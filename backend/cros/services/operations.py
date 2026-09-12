"""Domain orchestration: the real observe -> ... -> execute pipeline.

Every state change goes through validated commands that create events; the
projections update state. Nothing here mutates another aggregate directly
without an event.
"""
from __future__ import annotations

import uuid

from ..audit import record
from ..constants import (MISSION_TRANSITIONS, MISSION_TRANSITION_EVENT, EventType,
                         MissionStatus, Priority)
from ..errors import ApiError, already_decided, not_found, transition_invalid
from ..events import bus
from ..models import utcnow_iso
from ..observability import span
from ..ulid import new_ulid
from . import agents, allocation, communication, prioritization, routing, verification
from .hazard import get_module

PRIORITY_TO_EVENT_PRIORITY = {"critical": Priority.CRITICAL.value, "high": Priority.HIGH.value,
                              "normal": Priority.NORMAL.value, "low": Priority.LOW.value}


async def get_graph(db) -> dict:
    doc = await db.road_graph.find_one({"graph_id": "base-road-graph-v1"}, {"_id": 0})
    if not doc:
        doc = routing.build_base_graph()
        await db.road_graph.insert_one(dict(doc))
        doc.pop("_id", None)
    return doc


async def active_hazards(db) -> list[dict]:
    return await db.hazards.find({"active": True}, {"_id": 0}).to_list(500)


# --------------------------------------------------------------- pipeline
async def run_pipeline(db, request_id: str, *, actor_id: str | None = None,
                       simulation: bool = False, correlation_id: str | None = None) -> dict:
    """Verification -> dedupe -> incident association -> prioritization ->
    allocation -> AI recommendation -> critic -> pending approval."""
    steps = {}
    correlation_id = correlation_id or str(uuid.uuid4())
    req = await db.emergency_requests.find_one({"request_id": request_id}, {"_id": 0})
    if not req:
        raise not_found("Emergency request not found")

    with span("pipeline.verification", request_id=request_id):
        primary = await db.reports.find_one({"request_id": request_id}, {"_id": 0})
        if not primary:
            primary = {"report_id": f"R-{request_id}", "source_type": "citizen",
                       "text": req.get("description", ""), "location": req.get("location"),
                       "created_at": req.get("created_at")}
        related = await db.reports.find(
            {"request_id": {"$ne": request_id}}, {"_id": 0}).sort("created_at", -1).to_list(200)
        det = verification.evaluate(primary, related)

        recent = await db.emergency_requests.find(
            {"request_id": {"$ne": request_id}, "status": {"$nin": ["cancelled"]}},
            {"_id": 0}).sort("created_at", -1).to_list(200)
        dup = verification.find_duplicate(req, recent)

        reasoning, reasoning_meta = await agents.verification_reasoning(
            primary, related, det, session_id=f"verify-{request_id}")
        if reasoning.assessment == "contradicts" and det["status"] != "CONTRADICTORY":
            det["status"] = "CONTRADICTORY"
        det["confidence"] = round(min(det["confidence"], reasoning.confidence), 3)
        det["llm_reasoning"] = {"assessment": reasoning.assessment,
                               "rationale": reasoning.rationale,
                               "suggested_status": reasoning.suggested_status,
                               **reasoning_meta}
        if reasoning_meta.get("fallback_used"):
            await bus.emit_system(EventType.AI_FALLBACK_ACTIVATED.value,
                                  {"agent": "verification_reasoning",
                                   "reason": reasoning_meta.get("error", "unavailable"),
                                   "request_id": request_id},
                                  correlation_id=correlation_id, simulation=simulation)
        await bus.emit_system(
            EventType.REQUEST_VERIFIED.value,
            {"request_id": request_id, "verification": det,
             "duplicate_of": dup["request_id"] if dup else None,
             "contributing_report_ids": det["corroborating_report_ids"]},
            correlation_id=correlation_id, causal_parent_ids=[req.get("last_event_id")]
            if req.get("last_event_id") else [], simulation=simulation)
        steps["verification"] = det
        steps["duplicate_of"] = dup["request_id"] if dup else None

    if dup:
        req["priority"] = prioritization.compute_priority(req, 0.0)
        steps["priority"] = req["priority"]
        steps["outcome"] = "MERGED_AS_DUPLICATE"
        return steps

    with span("pipeline.incident_association", request_id=request_id):
        incident_id = req.get("incident_id")
        if not incident_id:
            incident = await db.incidents.find_one({"status": "active"}, {"_id": 0})
            incident_id = incident["incident_id"] if incident else None
            if incident_id:
                await db.emergency_requests.update_one({"request_id": request_id},
                                                       {"$set": {"incident_id": incident_id}})
        steps["incident_id"] = incident_id

    hazards = await active_hazards(db)
    coords = (req.get("location") or {}).get("coordinates", [0, 0])
    hazard_severity = routing.point_in_hazards(coords, hazards)

    with span("pipeline.prioritization", request_id=request_id):
        req["verification"] = det
        pr = prioritization.compute_priority(req, hazard_severity)
        await bus.emit_system(EventType.REQUEST_PRIORITIZED.value,
                              {"request_id": request_id, "priority": pr, "status": "triaged"},
                              priority=PRIORITY_TO_EVENT_PRIORITY.get(pr["tier"], "normal"),
                              correlation_id=correlation_id, simulation=simulation)
        steps["priority"] = pr
        req["priority"] = pr

    with span("pipeline.allocation", request_id=request_id):
        graph = await get_graph(db)
        resources = await db.resources.find({"status": "available"}, {"_id": 0}).to_list(200)
        proposal = allocation.propose([req], resources, graph, hazards)
        proposal["proposal_id"] = new_ulid()
        proposal["request_ids"] = [request_id]
        await bus.emit_system(EventType.ALLOCATION_PROPOSED.value, proposal,
                              correlation_id=correlation_id, simulation=simulation)
        steps["allocation"] = proposal

    assignment = proposal["assignments"][0] if proposal["assignments"] else None
    route = None
    if assignment:
        resource = next(r for r in resources if r["resource_id"] == assignment["resource_id"])
        route = routing.compute_route(graph, hazards,
                                     resource["location"]["coordinates"], coords,
                                     "boat" if resource.get("kind") == "boat" else "road")
        steps["route"] = route

    with span("pipeline.recommendation", request_id=request_id):
        rec = await draft_recommendation(
            db, req=req, assignment=assignment, route=route, hazard_severity=hazard_severity,
            incident_id=incident_id, correlation_id=correlation_id, simulation=simulation)
        steps["recommendation"] = rec
    steps["outcome"] = "PENDING_APPROVAL" if rec["approval_status"] == "pending_approval" \
        else rec["approval_status"].upper()
    return steps


async def draft_recommendation(db, *, req, assignment, route, hazard_severity, incident_id,
                               correlation_id, simulation=False) -> dict:
    rec_id = new_ulid()
    evidence_refs = [f"request:{req['request_id']}"]
    for rid in (req.get("verification") or {}).get("corroborating_report_ids", []):
        evidence_refs.append(f"report:{rid}")
    if route:
        evidence_refs.append(f"route:{route['graph_id']}")
    evidence_summary = {
        "verification_status": (req.get("verification") or {}).get("status"),
        "verification_confidence": (req.get("verification") or {}).get("confidence"),
        "uncertainty": (req.get("verification") or {}).get("uncertainty", []),
        "provenance": (req.get("verification") or {}).get("evidence", {}),
        "priority_tier": (req.get("priority") or {}).get("tier"),
        "priority_score": (req.get("priority") or {}).get("score"),
        "hazard_severity": hazard_severity,
        "route_status": route["status"] if route else "NO_ROUTE",
        "eta_seconds": route["eta_seconds"] if route else None,
        "hazard_exposure_edges": len(route["hazard_exposure"]) if route else 0,
    }
    proposed_action = {
        "type": "dispatch_mission",
        "emergency_request_id": req["request_id"],
        "incident_id": incident_id,
        "resource_id": assignment["resource_id"] if assignment else None,
        "resource_kind": assignment["resource_kind"] if assignment else None,
        "resource_label": assignment["resource_label"] if assignment else None,
        "responder_user_id": assignment["responder_user_id"] if assignment else None,
        "eta_seconds": assignment["eta_seconds"] if assignment else None,
    }
    context = {
        "structured": {
            "category": req.get("category"),
            "people_count": req.get("people_count"),
            "location_label": f"{round((req.get('location') or {}).get('coordinates',[0,0])[1],4)},"
                              f"{round((req.get('location') or {}).get('coordinates',[0,0])[0],4)}",
            **evidence_summary,
            "resource_label": proposed_action["resource_label"],
        },
        "report_text": req.get("description", ""),
    }
    synthesis, syn_meta = await agents.situation_synthesis(context, session_id=f"rec-{rec_id}")
    if syn_meta.get("fallback_used"):
        await bus.emit_system(EventType.AI_FALLBACK_ACTIVATED.value,
                              {"agent": "situation_synthesis",
                               "reason": syn_meta.get("error", "unavailable"),
                               "recommendation_id": rec_id},
                              correlation_id=correlation_id, simulation=simulation)

    alternatives = []
    graph = await get_graph(db)
    hazards = await active_hazards(db)
    others = await db.resources.find({"status": "available"}, {"_id": 0}).to_list(50)
    for rs in others:
        if assignment and rs["resource_id"] == assignment["resource_id"]:
            continue
        c = allocation.cost_for(req, rs, graph, hazards)
        alternatives.append({"alternative_id": new_ulid(), "resource_id": rs["resource_id"],
                             "resource_label": rs.get("label"), "resource_kind": rs.get("kind"),
                             "eta_seconds": c["eta_seconds"], "cost": c["cost"],
                             "feasible": c["feasible"], "reasons": c["reasons"]})
    alternatives = sorted(alternatives, key=lambda a: a["cost"])[:3]

    rec = {
        "recommendation_id": rec_id,
        "agent_name": "bounded_orchestrator",
        "kind": "mission_dispatch",
        "emergency_request_id": req["request_id"],
        "incident_id": incident_id,
        "summary": synthesis.summary,
        "key_risks": synthesis.key_risks,
        "information_gaps": synthesis.information_gaps,
        "confidence": round(min(synthesis.confidence,
                                (req.get("verification") or {}).get("confidence", 0.5) + 0.25), 3),
        "evidence_refs": evidence_refs,
        "evidence_summary": evidence_summary,
        "proposed_action": proposed_action,
        "alternatives": alternatives,
        "route": route,
        "deterministic_inputs": {
            "prioritization": req.get("priority"),
            "allocation_solver": (assignment or {}).get("route_status"),
        },
        "model": syn_meta,
        "fallback_used": bool(syn_meta.get("fallback_used")),
        "correlation_id": correlation_id,
        "trace_id": correlation_id,
        "created_at": utcnow_iso(),
    }
    rec["risk_tier"] = agents.risk_tier(rec)

    verdict, critic_meta = await agents.safety_critic(rec, session_id=f"critic-{rec_id}")
    rec["critic"] = {"result": verdict.result, "reason_codes": verdict.reason_codes,
                     "rationale": verdict.rationale, **critic_meta}

    await bus.emit_system(EventType.AI_RECOMMENDATION_DRAFTED.value,
                          {"recommendation_id": rec_id, "agent_name": rec["agent_name"],
                           "risk_tier": rec["risk_tier"], "confidence": rec["confidence"],
                           "evidence_refs": evidence_refs,
                           "alternative_ids": [a["alternative_id"] for a in alternatives],
                           "proposed_action": proposed_action, "recommendation": rec},
                          correlation_id=correlation_id, simulation=simulation)

    if verdict.result == "block":
        await bus.emit_system(EventType.AI_RECOMMENDATION_BLOCKED.value,
                              {"recommendation_id": rec_id,
                               "reason_codes": verdict.reason_codes,
                               "critic_mode": critic_meta.get("mode"),
                               "recommendation": rec},
                              priority=Priority.HIGH.value,
                              correlation_id=correlation_id, simulation=simulation)
        rec["approval_status"] = "blocked"
    else:
        await bus.emit_system(EventType.AI_RECOMMENDATION_APPROVED_FOR_HUMAN_REVIEW.value,
                              {"recommendation_id": rec_id, "risk_tier": rec["risk_tier"],
                               "requires_human_approval": rec["risk_tier"] in ("high", "medium"),
                               "recommendation": rec},
                              priority=Priority.HIGH.value,
                              correlation_id=correlation_id, simulation=simulation)
        rec["approval_status"] = "pending_approval"
    return rec


# ------------------------------------------------------------------ HITL
async def decide_recommendation(db, *, recommendation_id: str, approver: dict, approve: bool,
                                reason: str | None, emergency_override: bool = False,
                                simulation: bool = False) -> dict:
    rec = await db.recommendations.find_one({"recommendation_id": recommendation_id}, {"_id": 0})
    if not rec:
        raise not_found("Recommendation not found")
    existing = await db.approvals.find_one({"recommendation_id": recommendation_id}, {"_id": 0})
    if existing:
        raise already_decided(
            f"Recommendation already {existing['decision']} by {existing['approver_user_id']}")
    if rec.get("approval_status") == "blocked" and not emergency_override:
        raise ApiError(409, "CRITIC_BLOCKED",
                       "Safety critic blocked this recommendation; emergency override required")

    approval_id = new_ulid()
    correlation_id = rec.get("correlation_id")
    payload = {"approval_id": approval_id, "recommendation_id": recommendation_id,
               "approver_user_id": approver["user_id"], "approver_role": approver["role"],
               "decision": "approved" if approve else "rejected", "reason": reason,
               "was_emergency_override": bool(emergency_override)}
    await bus.emit_system(
        EventType.APPROVAL_GRANTED.value if approve else EventType.APPROVAL_REJECTED.value,
        payload, priority=Priority.HIGH.value, origin_actor_id=approver["user_id"],
        correlation_id=correlation_id, simulation=simulation)
    await record(actor_id=approver["user_id"], actor_role=approver["role"],
                 action="RECOMMENDATION_DECISION", entity_type="recommendation",
                 entity_id=recommendation_id, decision=payload["decision"],
                 correlation_id=correlation_id, detail={"reason": reason,
                                                        "override": emergency_override},
                 simulation=simulation, immutable=True)

    result = {"approval_id": approval_id, "decision": payload["decision"], "mission_id": None}
    if approve:
        mission = await execute_recommendation(db, rec, approver, correlation_id, simulation)
        result["mission_id"] = mission["mission_id"] if mission else None
    return result


async def execute_recommendation(db, rec: dict, actor: dict, correlation_id, simulation=False):
    """Deterministic execution of an approved recommendation. LLM output never runs directly."""
    action = rec.get("proposed_action") or {}
    if action.get("type") != "dispatch_mission" or not action.get("resource_id"):
        return None
    mission_id = new_ulid()
    priority = (rec.get("evidence_summary") or {}).get("priority_tier", "normal")
    await bus.emit_system(
        EventType.MISSION_PROPOSED.value,
        {"mission_id": mission_id,
         "emergency_request_id": action["emergency_request_id"],
         "incident_id": action.get("incident_id"),
         "resource_id": action["resource_id"],
         "responder_user_id": action.get("responder_user_id"),
         "recommendation_id": rec["recommendation_id"],
         "priority": priority,
         "objective": rec.get("summary", "")[:400],
         "route": rec.get("route")},
        priority=PRIORITY_TO_EVENT_PRIORITY.get(priority, "normal"),
        origin_actor_id=actor["user_id"], correlation_id=correlation_id, simulation=simulation)
    await bus.emit_system(
        EventType.MISSION_APPROVED.value,
        {"mission_id": mission_id, "emergency_request_id": action["emergency_request_id"],
         "responder_user_id": action.get("responder_user_id"),
         "previous_status": "proposed", "new_status": "approved",
         "approval_recommendation_id": rec["recommendation_id"], "reason": None},
        priority=Priority.HIGH.value, origin_actor_id=actor["user_id"],
        correlation_id=correlation_id, simulation=simulation)
    await bus.emit_system(
        EventType.RESOURCE_STATUS_CHANGED.value,
        {"resource_id": action["resource_id"], "previous_status": "available",
         "new_status": "assigned", "mission_id": mission_id},
        origin_actor_id=actor["user_id"], correlation_id=correlation_id, simulation=simulation)
    return {"mission_id": mission_id}


# --------------------------------------------------------------- missions
async def transition_mission(db, *, mission_id: str, target: MissionStatus, actor: dict,
                             reason: str | None = None, outcome: dict | None = None,
                             event_id: str | None = None, simulation: bool = False) -> dict:
    mission = await db.missions.find_one({"mission_id": mission_id}, {"_id": 0})
    if not mission:
        raise not_found("Mission not found")
    current = MissionStatus(mission["status"])
    if current == target:
        return {"mission_id": mission_id, "status": target.value, "idempotent": True}
    if target not in MISSION_TRANSITIONS[current]:
        raise transition_invalid(
            f"Invalid transition {current.value} -> {target.value}")
    if target in (MissionStatus.ACCEPTED_BY_RESPONDER, MissionStatus.DECLINED,
                  MissionStatus.EN_ROUTE, MissionStatus.ON_SCENE,
                  MissionStatus.COMPLETED, MissionStatus.FAILED):
        if actor["role"] == "field_responder" and \
                mission.get("responder_user_id") not in (None, actor["user_id"]):
            raise ApiError(403, "NOT_MISSION_OWNER", "Mission assigned to another responder")

    payload = {"mission_id": mission_id,
               "emergency_request_id": mission.get("emergency_request_id"),
               "responder_user_id": mission.get("responder_user_id") or actor["user_id"],
               "previous_status": current.value, "new_status": target.value,
               "reason": reason}
    if outcome:
        payload["outcome"] = outcome
    result = await bus.emit_system(
        MISSION_TRANSITION_EVENT[target], payload,
        priority=PRIORITY_TO_EVENT_PRIORITY.get(mission.get("priority", "normal"), "normal"),
        origin_actor_id=actor["user_id"], correlation_id=mission.get("correlation_id"),
        causal_parent_ids=[mission.get("last_event_id")] if mission.get("last_event_id") else [],
        event_id=event_id, simulation=simulation)

    if target in (MissionStatus.COMPLETED, MissionStatus.FAILED) and mission.get("resource_id"):
        await bus.emit_system(EventType.RESOURCE_STATUS_CHANGED.value,
                              {"resource_id": mission["resource_id"],
                               "previous_status": "assigned", "new_status": "available",
                               "mission_id": None},
                              origin_actor_id=actor["user_id"], simulation=simulation)
    await record(actor_id=actor["user_id"], actor_role=actor["role"],
                 action=MISSION_TRANSITION_EVENT[target], entity_type="mission",
                 entity_id=mission_id, event_id=result.get("event_id"),
                 detail={"from": current.value, "to": target.value, "reason": reason},
                 simulation=simulation, immutable=True)
    return {"mission_id": mission_id, "status": target.value,
            "event_id": result.get("event_id"), "idempotent": False}
