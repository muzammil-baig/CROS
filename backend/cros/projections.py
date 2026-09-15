"""Event consumers / projections. All handlers must be idempotent."""
from .config import PERSISTENCE_BACKEND
from .constants import MISSION_TRANSITIONS, EventType, MissionStatus
from .events import bus
from .models import utcnow_iso
from .services import capacity, sync as sync_svc
from .services.transport import registry


def _p(event: dict) -> dict:
    return event.get("payload") or {}


def _meta(event: dict) -> dict:
    return {"last_event_id": event["event_id"],
            "hlc": event["logical_timestamp"],
            "updated_at": event["wall_clock_timestamp"],
            "origin_device_id": event["origin_device_id"]}


# --------------------------------------------------------------- requests
@bus.on(EventType.RESCUE_REQUEST_CREATED.value)
async def on_request_created(db, event):
    p = _p(event)
    doc = {
        "request_id": p["request_id"],
        "incident_id": p.get("incident_id"),
        "category": p.get("category", "rescue"),
        "description": p.get("description", ""),
        "location": p.get("location"),
        "geo": p.get("location"),
        "people_count": p.get("people_count", 1),
        "vulnerabilities": p.get("vulnerabilities", []),
        "water_rising": p.get("water_rising", False),
        "reporter_user_id": event.get("origin_actor_id"),
        "reporter_name": p.get("reporter_name"),
        "reporter_phone": p.get("reporter_phone"),
        "created_at": event["wall_clock_timestamp"],
        "status": "new",
        "verification": {"status": "UNVERIFIED", "confidence": 0.0},
        "priority": {"score": 0.0, "tier": "normal"},
        "duplicate_of": None,
        "contributing_report_ids": [],
        "sync_state": "SYNCED",
    }
    await db.emergency_requests.update_one(
        {"request_id": doc["request_id"]},
        {"$setOnInsert": doc, "$set": _meta(event)}, upsert=True)
    # provenance: append-only report record, never deleted
    report_id = p.get("report_id") or f"R-{event['event_id']}"
    await db.reports.update_one(
        {"report_id": report_id},
        {"$setOnInsert": {
            "report_id": report_id,
            "request_id": p["request_id"],
            "kind": "rescue_request",
            "source_type": p.get("source_type", "citizen"),
            "source_user_id": event.get("origin_actor_id"),
            "device_id": event["origin_device_id"],
            "text": p.get("description", ""),
            "location": p.get("location"),
            "created_at": event["wall_clock_timestamp"],
            "event_id": event["event_id"],
        }}, upsert=True)


@bus.on(EventType.REQUEST_VERIFIED.value)
async def on_request_verified(db, event):
    p = _p(event)
    update = {"verification": p["verification"], **_meta(event)}
    if p.get("duplicate_of"):
        update["duplicate_of"] = p["duplicate_of"]
        update["status"] = "duplicate_merged"
    await db.emergency_requests.update_one({"request_id": p["request_id"]}, {"$set": update})
    if p.get("contributing_report_ids"):
        await db.emergency_requests.update_one(
            {"request_id": p["request_id"]},
            {"$addToSet": {"contributing_report_ids": {"$each": p["contributing_report_ids"]}}})
    if p.get("duplicate_of"):
        await db.emergency_requests.update_one(
            {"request_id": p["duplicate_of"]},
            {"$addToSet": {"duplicate_report_ids": p["request_id"]}})


@bus.on(EventType.REQUEST_PRIORITIZED.value)
async def on_request_prioritized(db, event):
    p = _p(event)
    await db.emergency_requests.update_one(
        {"request_id": p["request_id"]},
        {"$set": {"priority": p["priority"], "status": p.get("status", "triaged"), **_meta(event)}})


@bus.on(EventType.REQUEST_CANCELLED.value)
async def on_request_cancelled(db, event):
    p = _p(event)
    await db.emergency_requests.update_one(
        {"request_id": p["request_id"]},
        {"$set": {"status": "cancelled", "cancel_reason": p.get("reason"), **_meta(event)}})


# --------------------------------------------------------------- incidents
@bus.on(EventType.INCIDENT_CREATED.value)
async def on_incident_created(db, event):
    p = _p(event)
    await db.incidents.update_one(
        {"incident_id": p["incident_id"]},
        {"$setOnInsert": {
            "incident_id": p["incident_id"], "name": p.get("name"),
            "hazard_type": p.get("hazard_type", "flood"),
            "status": p.get("status", "active"), "severity": p.get("severity", "moderate"),
            "commander_user_id": p.get("commander_user_id"),
            "location": p.get("location"), "area": p.get("area"),
            "created_at": event["wall_clock_timestamp"]},
         "$set": _meta(event)}, upsert=True)


@bus.on(EventType.INCIDENT_UPDATED.value, EventType.INCIDENT_STATUS_CHANGED.value)
async def on_incident_updated(db, event):
    p = _p(event)
    changes = {k: v for k, v in (p.get("changes") or {}).items()}
    if p.get("new_status"):
        changes["status"] = p["new_status"]
    if p.get("commander_user_id"):
        changes["commander_user_id"] = p["commander_user_id"]
    if changes:
        await db.incidents.update_one({"incident_id": p["incident_id"]},
                                      {"$set": {**changes, **_meta(event)}})


# ----------------------------------------------------------------- hazards
@bus.on(EventType.HAZARD_REPORTED.value)
async def on_hazard_reported(db, event):
    p = _p(event)
    # Module-specific attributes (water level, wind speed, surge, ...) persist
    # generically so the core domain stays hazard-agnostic.
    extra = {k: v for k, v in p.items() if k != "hazard_id"}
    await db.hazards.update_one(
        {"hazard_id": p["hazard_id"]},
        {"$setOnInsert": {"hazard_id": p["hazard_id"], "created_at":
                          event["wall_clock_timestamp"]},
         "$set": {
             "incident_id": p.get("incident_id"),
             "hazard_type": p.get("hazard_type", "flood"),
             "geometry": p.get("geometry"),
             "description": p.get("description", ""),
             "severity": p.get("severity", 0.5),
             "road_blocked": p.get("road_blocked", False),
             "source_provenance": p.get("source_provenance", "citizen_report"),
             "verification": p.get("verification", {"status": "UNVERIFIED", "confidence": 0.4}),
             "active": p.get("active", True),
             "version": p.get("version", 1),
             **extra,
             **_meta(event)}}, upsert=True)
    if PERSISTENCE_BACKEND == "postgres" and p.get("geometry"):
        from . import postgres
        try:
            await postgres.upsert_spatial_hazard({**p, "geometry": p["geometry"], "updated_at": event["wall_clock_timestamp"]})
            await postgres.invalidate_routes_for_hazard(p["hazard_id"], "hazard_overlay_updated")
        except Exception:
            # Event application must remain available when a legacy hazard payload
            # cannot satisfy the spatial schema; the canonical event is preserved.
            pass


# --------------------------------------------------------------- resources
@bus.on(EventType.RESOURCE_STATUS_CHANGED.value)
async def on_resource_status(db, event):
    p = _p(event)
    update = {"status": p["new_status"], "assigned_mission_id": p.get("mission_id"),
              **_meta(event)}
    if p.get("location"):
        update["location"] = p["location"]
    await db.resources.update_one({"resource_id": p["resource_id"]}, {"$set": update})


@bus.on(EventType.ALLOCATION_PROPOSED.value)
async def on_allocation_proposed(db, event):
    p = _p(event)
    await db.allocation_proposals.update_one(
        {"proposal_id": p["proposal_id"]},
        {"$setOnInsert": {**p, "created_at": event["wall_clock_timestamp"],
                          "event_id": event["event_id"]}}, upsert=True)


# --------------------------------------------------------------- missions
@bus.on(EventType.MISSION_PROPOSED.value)
async def on_mission_proposed(db, event):
    p = _p(event)
    await db.missions.update_one(
        {"mission_id": p["mission_id"]},
        {"$setOnInsert": {
            "mission_id": p["mission_id"],
            "emergency_request_id": p.get("emergency_request_id"),
            "incident_id": p.get("incident_id"),
            "responder_user_id": p.get("responder_user_id"),
            "resource_id": p.get("resource_id"),
            "recommendation_id": p.get("recommendation_id"),
            "priority": p.get("priority", "normal"),
            "objective": p.get("objective", ""),
            "route": p.get("route"),
            "status": MissionStatus.PROPOSED.value,
            "created_at": event["wall_clock_timestamp"],
            "history": []},
         "$set": _meta(event)}, upsert=True)
    await db.missions.update_one(
        {"mission_id": p["mission_id"], "history.event_id": {"$ne": event["event_id"]}},
        {"$push": {"history": {"status": MissionStatus.PROPOSED.value,
                               "at": event["wall_clock_timestamp"],
                               "actor_id": event.get("origin_actor_id"),
                               "event_id": event["event_id"]}}})


_TRANSITION_EVENTS = {
    EventType.MISSION_APPROVED.value: MissionStatus.APPROVED.value,
    EventType.MISSION_REJECTED.value: MissionStatus.REJECTED.value,
    EventType.MISSION_DISPATCHED.value: MissionStatus.DISPATCHED.value,
    EventType.MISSION_ACCEPTED.value: MissionStatus.ACCEPTED_BY_RESPONDER.value,
    EventType.MISSION_DECLINED.value: MissionStatus.DECLINED.value,
    EventType.MISSION_EN_ROUTE.value: MissionStatus.EN_ROUTE.value,
    EventType.MISSION_ON_SCENE.value: MissionStatus.ON_SCENE.value,
    EventType.MISSION_COMPLETED.value: MissionStatus.COMPLETED.value,
    EventType.MISSION_FAILED.value: MissionStatus.FAILED.value,
}


@bus.on(*_TRANSITION_EVENTS.keys())
async def on_mission_transition(db, event):
    p = _p(event)
    new_status = _TRANSITION_EVENTS[event["event_type"]]
    mission = await db.missions.find_one({"mission_id": p["mission_id"]}, {"_id": 0})
    if not mission:
        return
    if mission["status"] == new_status:
        return  # idempotent
    allowed = [s.value for s in MISSION_TRANSITIONS.get(MissionStatus(mission["status"]), [])]
    if new_status not in allowed:
        resolution = sync_svc.resolve("mission.status",
                                      {"value": mission["status"], "hlc": mission.get("hlc", "")},
                                      {"value": new_status,
                                       "hlc": event["logical_timestamp"]})
        if resolution["winner"] != "remote":
            await sync_svc.record_conflict(
                db, entity_type="mission", entity_id=p["mission_id"], path="mission.status",
                local={"value": mission["status"], "hlc": mission.get("hlc")},
                remote={"value": new_status, "hlc": event["logical_timestamp"]},
                resolution=resolution, event_id=event["event_id"])
            return
    update = {"status": new_status, **_meta(event)}
    if p.get("responder_user_id"):
        update["responder_user_id"] = p["responder_user_id"]
    if p.get("route"):
        update["route"] = p["route"]
    if p.get("reason"):
        update["last_reason"] = p["reason"]
    if p.get("outcome"):
        update["outcome"] = p["outcome"]
    await db.missions.update_one({"mission_id": p["mission_id"]}, {"$set": update})
    await db.missions.update_one(
        {"mission_id": p["mission_id"], "history.event_id": {"$ne": event["event_id"]}},
        {"$push": {"history": {"status": new_status, "at": event["wall_clock_timestamp"],
                               "actor_id": event.get("origin_actor_id"),
                               "reason": p.get("reason"), "event_id": event["event_id"]}}})
    if mission.get("emergency_request_id"):
        req_status = {"approved": "assigned", "dispatched": "assigned",
                      "en_route": "responding", "on_scene": "on_scene",
                      "completed": "resolved", "failed": "needs_reassignment"}.get(new_status)
        if req_status:
            await db.emergency_requests.update_one(
                {"request_id": mission["emergency_request_id"]},
                {"$set": {"status": req_status, "mission_id": mission["mission_id"]}})


# --------------------------------------------------------------- capacity
@bus.on(EventType.HOSPITAL_CAPACITY_CHANGED.value, EventType.SHELTER_CAPACITY_CHANGED.value)
async def on_capacity_changed(db, event):
    p = _p(event)
    await capacity.apply_operation(
        db, facility_id=p["facility_id"], counter_kind=p["counter_kind"],
        operation=p["operation"], amount=int(p["amount"]),
        operation_id=p["operation_id"], actor_id=event.get("origin_actor_id"),
        origin_device_id=event["origin_device_id"])


# ---------------------------------------------------------- communication
@bus.on(EventType.GATEWAY_ONLINE.value, EventType.GATEWAY_OFFLINE.value)
async def on_gateway_state(db, event):
    p = _p(event)
    online = event["event_type"] == EventType.GATEWAY_ONLINE.value
    await db.gateways.update_one(
        {"gateway_id": p["gateway_id"]},
        {"$set": {"status": "online" if online else "offline",
                  "connectivity_state": p.get("connectivity_state",
                                              "CONNECTED" if online else "ISOLATED"),
                  "last_seen": event["wall_clock_timestamp"], **_meta(event)}})


@bus.on(EventType.SATELLITE_CONNECTED.value, EventType.SATELLITE_DISCONNECTED.value)
async def on_satellite_state(db, event):
    p = _p(event)
    connected = event["event_type"] == EventType.SATELLITE_CONNECTED.value
    sat = registry.get("satellite")
    if sat:
        sat.adapter.window_open = connected
        sat.available = connected
        sat.degradation = 0.0 if connected else 1.0
    await db.link_state.update_one(
        {"link": "satellite"},
        {"$set": {"link": "satellite", "connected": connected, "simulated": True,
                  "provider": p.get("provider", "simulator"),
                  "updated_at": event["wall_clock_timestamp"]}}, upsert=True)


@bus.on(EventType.MESSAGE_EXPIRED.value)
async def on_message_expired(db, event):
    p = _p(event)
    if p.get("message_id"):
        await db.comm_messages.update_one({"message_id": p["message_id"]},
                                          {"$set": {"state": "EXPIRED"}})


# ------------------------------------------------------ AI / HITL / audit
@bus.on(EventType.AI_RECOMMENDATION_DRAFTED.value,
        EventType.AI_RECOMMENDATION_BLOCKED.value,
        EventType.AI_RECOMMENDATION_APPROVED_FOR_HUMAN_REVIEW.value)
async def on_recommendation(db, event):
    p = _p(event)
    status = {
        EventType.AI_RECOMMENDATION_DRAFTED.value: "drafted",
        EventType.AI_RECOMMENDATION_BLOCKED.value: "blocked",
        EventType.AI_RECOMMENDATION_APPROVED_FOR_HUMAN_REVIEW.value: "pending_approval",
    }[event["event_type"]]
    rec = p.get("recommendation") or {}
    await db.recommendations.update_one(
        {"recommendation_id": p["recommendation_id"]},
        {"$set": {**rec, "recommendation_id": p["recommendation_id"],
                  "approval_status": status, **_meta(event)}}, upsert=True)


@bus.on(EventType.APPROVAL_GRANTED.value, EventType.APPROVAL_REJECTED.value)
async def on_approval(db, event):
    p = _p(event)
    granted = event["event_type"] == EventType.APPROVAL_GRANTED.value
    approval = {
        "approval_id": p["approval_id"],
        "recommendation_id": p["recommendation_id"],
        "approver_user_id": p.get("approver_user_id"),
        "decision": "approved" if granted else "rejected",
        "reason": p.get("reason"),
        "was_emergency_override": bool(p.get("was_emergency_override")),
        "created_at": event["wall_clock_timestamp"],
        "event_id": event["event_id"],
        "immutable": True,
    }
    # append-only + immutable: only ever inserted, never updated
    await db.approvals.update_one({"approval_id": p["approval_id"]},
                                  {"$setOnInsert": approval}, upsert=True)
    await db.recommendations.update_one(
        {"recommendation_id": p["recommendation_id"]},
        {"$set": {"approval_status": "approved" if granted else "rejected",
                  "human_decision": {"approver_user_id": p.get("approver_user_id"),
                                     "decision": approval["decision"],
                                     "reason": p.get("reason"),
                                     "was_emergency_override":
                                         approval["was_emergency_override"],
                                     "at": event["wall_clock_timestamp"]},
                  "decided_at": event["wall_clock_timestamp"], **_meta(event)}})


@bus.on(EventType.DEVICE_REGISTERED.value, EventType.DEVICE_KEY_ROTATED.value)
async def on_device_registered(db, event):
    p = _p(event)
    if PERSISTENCE_BACKEND == "postgres":
        from . import postgres
        await postgres.upsert_device(device_id=p["device_id"], owner_user_id=p.get("owner_user_id"),
                                     public_key=p.get("public_key", ""),
                                     signing_mode=p.get("signing_mode", "client_webcrypto"),
                                     trust_level=p.get("trust_level", "provisional"))
        return
    await db.devices.update_one(
        {"device_id": p["device_id"]},
        {"$set": {"public_key": p.get("public_key"), "revoked": False,
                  "trust_level": p.get("trust_level", "provisional"),
                  "owner_user_id": p.get("owner_user_id"),
                  "device_type": p.get("device_type", "mobile"),
                  "signing_mode": p.get("signing_mode", "client_webcrypto"),
                  "registered_at": event["wall_clock_timestamp"]}}, upsert=True)


@bus.on(EventType.DEVICE_CREDENTIAL_REVOKED.value)
async def on_device_revoked(db, event):
    p = _p(event)
    if PERSISTENCE_BACKEND == "postgres":
        from . import postgres
        await postgres.revoke_device(p["device_id"], p.get("reason"))
        return
    await db.devices.update_one(
        {"device_id": p["device_id"]},
        {"$set": {"revoked": True, "revoked_reason": p.get("reason"),
                  "revoked_at": p.get("revoked_at", event["wall_clock_timestamp"]),
                  "trust_level": "revoked"}})


# ------------------------------------------------------------------ audit
AUDITED = {e.value for e in EventType}


@bus.on(*sorted(AUDITED))
async def on_any_event_audit(db, event):
    from .audit import record
    p = _p(event)
    entity_id = (p.get("request_id") or p.get("mission_id") or p.get("incident_id")
                 or p.get("hazard_id") or p.get("recommendation_id") or p.get("facility_id")
                 or p.get("device_id") or p.get("gateway_id") or p.get("message_id"))
    await record(actor_id=event.get("origin_actor_id"), actor_role=None,
                 action=event["event_type"], entity_type="event", entity_id=entity_id,
                 device_id=event["origin_device_id"], event_id=event["event_id"],
                 correlation_id=event.get("correlation_id"),
                 detail={"priority": str(event.get("priority")),
                         "signature_verified": event.get("signature_verified"),
                         "hlc": event["logical_timestamp"]},
                 simulation=bool(event.get("simulation")), immutable=True)
