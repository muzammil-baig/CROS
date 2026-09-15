from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..audit import record
from ..auth import require
from ..constants import EventType, Priority
from ..db import db
from ..errors import ApiError, not_found
from ..events import bus
from ..models import utcnow_iso
from ..services import communication, sync as sync_svc
from ..services.transport import registry, score_transport, select_transport
from ..edge import gateway as edge
from .deps import publish_offline_envelope, sign_if_delegated

router = APIRouter(tags=["communication-sync"])


class DegradeBody(BaseModel):
    transport: str
    degradation: float = Field(ge=0.0, le=1.0)


class ReprioritizeBody(BaseModel):
    priority: str


class SyncBody(BaseModel):
    device_id: str
    known_event_ids: list[str] = Field(default_factory=list, max_length=5000)
    events: list[dict] = Field(default_factory=list, max_length=200)
    max_events: int = Field(default=50, ge=1, le=500)
    priority_floor: str = "low"
    latest_hlc: Optional[str] = None


class ResolveConflictBody(BaseModel):
    resolution: str
    reason: str = Field(min_length=3, max_length=500)


class MeshRelayBody(BaseModel):
    peer_gateway_id: str
    max_events: int = Field(default=25, ge=1, le=200)
    push_upstream: bool = True


class GatewayConfigBody(BaseModel):
    connectivity_state: Optional[str] = None
    battery_pct: Optional[int] = None
    mesh_roster: Optional[list[str]] = None


class MissionCommunicationBody(BaseModel):
    recipient: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    priority: str = Field(default="high", pattern="^(critical|high|normal|low)$")
    incident_id: Optional[str] = None
    require_ack: bool = True
    correlation_id: Optional[str] = Field(default=None, max_length=120)


# ------------------------------------------------------------------ transports
@router.get("/comm/transports")
async def transports(user: dict = Depends(require("comm:read"))):
    states = registry.states()
    chosen, scoring = select_transport(states, "critical", 2.0, True)
    return {"items": states, "connectivity_state": registry.connectivity_state(),
            "selection_for_critical_2kb": {"chosen": chosen, "scoring": scoring},
            "policy": "deterministic_capability_scoring_v1",
            "simulated_transports": [s["name"] for s in states if s["simulated"]]}


@router.post("/comm/transports/degrade")
async def degrade_transport(body: DegradeBody, user: dict = Depends(require("comm:write"))):
    if not registry.get(body.transport):
        raise not_found("Unknown transport")
    registry.set_degradation(body.transport, body.degradation)
    state = registry.connectivity_state()
    if body.transport == "satellite":
        await bus.emit_system(
            EventType.SATELLITE_DISCONNECTED.value if body.degradation >= 1.0
            else EventType.SATELLITE_CONNECTED.value,
            {"provider": "simulator", "simulated": True,
             "degradation": body.degradation}, priority=Priority.HIGH.value)
    return {"transport": body.transport, "degradation": body.degradation,
            "connectivity_state": state, "transports": registry.states()}


@router.get("/comm/queue")
async def comm_queue(state: Optional[str] = None, limit: int = Query(100, le=500),
                     user: dict = Depends(require("comm:read"))):
    q = {"state": state} if state else {}
    docs = await db.comm_messages.find(q, {"_id": 0, "envelope": 0}).sort(
        "queued_at", -1).to_list(limit)
    return {"items": docs, "depth": await communication.queue_depth(db),
            "connectivity_state": registry.connectivity_state()}


@router.post("/comm/queue/{message_id}/reprioritize")
async def reprioritize(message_id: str, body: ReprioritizeBody,
                       user: dict = Depends(require("comm:write"))):
    if body.priority not in ("critical", "high", "normal", "low"):
        raise ApiError(422, "VALIDATION_FAILED", "Invalid priority")
    res = await db.comm_messages.update_one({"message_id": message_id},
                                             {"$set": {"priority": body.priority}})
    if res.matched_count == 0:
        raise not_found("Message not found")
    return {"message_id": message_id, "priority": body.priority}


@router.post("/comm/queue/flush")
async def flush_queue(user: dict = Depends(require("comm:write"))):
    return await communication.flush(db)


@router.post("/comm/enqueue-test")
async def enqueue_test(user: dict = Depends(require("comm:write"))):
    """Queue a real signed heartbeat event through the transport abstraction."""
    env = bus.build_envelope("MESSAGE_QUEUED", {"probe": "operator_link_test",
                                                "at": utcnow_iso()},
                             priority=Priority.NORMAL.value)
    msg = await communication.enqueue(db, envelope=env)
    return {"message_id": msg["message_id"], "state": msg["state"]}


@router.post("/missions/{mission_id}/communications")
async def send_mission_communication(mission_id: str, body: MissionCommunicationBody,
                                      user: dict = Depends(require("comm:write"))):
    """Queue a communication only for an approved and dispatched mission."""
    mission = await db.missions.find_one({"mission_id": mission_id}, {"_id": 0})
    if not mission:
        raise not_found("Mission not found")
    if mission.get("status") != "dispatched":
        raise ApiError(409, "MISSION_NOT_DISPATCHED",
                       "Communication requires a dispatched mission")
    if body.incident_id and body.incident_id != mission.get("incident_id"):
        raise ApiError(403, "MISSION_SCOPE_MISMATCH",
                       "Communication incident does not match mission scope")
    correlation_id = body.correlation_id or mission.get("correlation_id") or mission_id
    envelope = bus.build_envelope(
        EventType.MESSAGE_QUEUED.value,
        {"mission_id": mission_id, "incident_id": mission.get("incident_id"),
         "emergency_request_id": mission.get("emergency_request_id"),
         "recipient": body.recipient, "message": body.message,
         "sender_user_id": user["user_id"], "correlation_id": correlation_id},
        priority=body.priority, correlation_id=correlation_id,
        origin_actor_id=user["user_id"])
    msg = await communication.enqueue(db, envelope=envelope,
                                      require_ack=body.require_ack)
    await record(actor_id=user["user_id"], actor_role=user["role"],
                 action="MISSION_COMMUNICATION_QUEUED", entity_type="mission",
                 entity_id=mission_id,
                 detail={"message_id": msg["message_id"], "recipient": body.recipient,
                         "priority": body.priority, "simulated_transport": True},
                 immutable=True)
    return {"mission_id": mission_id, "message_id": msg["message_id"],
            "state": msg["state"], "correlation_id": correlation_id,
            "require_ack": body.require_ack}


# -------------------------------------------------------------------- gateways
@router.get("/gateways")
async def list_gateways(user: dict = Depends(require("comm:read"))):
    docs = await db.gateways.find({}, {"_id": 0}).to_list(100)
    for g in docs:
        node = edge.node(g["gateway_id"])
        g["edge_runtime"] = node.health()
    return {"items": docs, "count": len(docs)}


@router.get("/gateways/{gateway_id}/health")
async def gateway_health(gateway_id: str, user: dict = Depends(require("comm:read"))):
    g = await db.gateways.find_one({"gateway_id": gateway_id}, {"_id": 0})
    if not g:
        raise not_found("Gateway not found")
    node = edge.node(gateway_id)
    return {**g, "edge_runtime": node.health(),
            "transports": registry.states(),
            "connectivity_state": g.get("connectivity_state")}


@router.post("/gateways/{gateway_id}/restart")
async def gateway_restart(gateway_id: str, user: dict = Depends(require("gateway:admin"))):
    g = await db.gateways.find_one({"gateway_id": gateway_id}, {"_id": 0})
    if not g:
        raise not_found("Gateway not found")
    await bus.emit_system(EventType.GATEWAY_OFFLINE.value,
                          {"gateway_id": gateway_id, "connectivity_state": "ISOLATED",
                           "reason": "operator_restart"}, priority=Priority.HIGH.value)
    await bus.emit_system(EventType.GATEWAY_ONLINE.value,
                          {"gateway_id": gateway_id, "connectivity_state": "CONNECTED"})
    return {"gateway_id": gateway_id, "status": "online", "restarted_at": utcnow_iso()}


@router.post("/gateways/{gateway_id}/reconfigure")
async def gateway_reconfigure(gateway_id: str, body: GatewayConfigBody,
                              user: dict = Depends(require("gateway:admin"))):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise ApiError(422, "VALIDATION_FAILED", "No configuration supplied")
    res = await db.gateways.update_one({"gateway_id": gateway_id}, {"$set": changes})
    if res.matched_count == 0:
        raise not_found("Gateway not found")
    edge.node(gateway_id).set_meta("config", changes)
    return {"gateway_id": gateway_id, "changes": changes}


@router.get("/gateways/{gateway_id}/mesh-roster")
async def mesh_roster(gateway_id: str, user: dict = Depends(require("comm:read"))):
    g = await db.gateways.find_one({"gateway_id": gateway_id}, {"_id": 0})
    if not g:
        raise not_found("Gateway not found")
    return {"gateway_id": gateway_id, "mesh_roster": g.get("mesh_roster", []),
            "transport": registry.get("mesh").state()}


@router.get("/gateways/{gateway_id}/audit")
async def gateway_audit(gateway_id: str, limit: int = Query(100, le=500),
                        user: dict = Depends(require("comm:read"))):
    docs = await db.audit_log.find({"entity_id": gateway_id}, {"_id": 0}).sort(
        "created_at", -1).to_list(limit)
    return {"gateway_id": gateway_id, "entries": docs, "count": len(docs)}


async def _run_pipelines_for(accepted_event_ids: list[str], actor_id: str):
    """Newly accepted rescue requests must go through the real domain pipeline."""
    if not accepted_event_ids:
        return []
    from ..services import operations
    events = await db.events.find(
        {"event_id": {"$in": accepted_event_ids},
         "event_type": EventType.RESCUE_REQUEST_CREATED.value},
        {"_id": 0, "payload": 1}).to_list(200)
    processed = []
    for e in events:
        rid = (e.get("payload") or {}).get("request_id")
        if not rid:
            continue
        try:
            await operations.run_pipeline(db, rid, actor_id=actor_id)
            processed.append(rid)
        except Exception:
            continue
    return processed


@router.post("/gateways/{gateway_id}/mesh-relay")
async def mesh_relay(gateway_id: str, body: MeshRelayBody,
                     user: dict = Depends(require("comm:write"))):
    """Gateway-to-gateway relay over the mesh transport.

    An isolated gateway hands its locally queued events to a peer that still has
    backhaul. Logical event identity (event_id / HLC / causal parents) is preserved:
    the relay is a transport hop, never a new domain event.
    """
    if gateway_id == body.peer_gateway_id:
        raise ApiError(422, "VALIDATION_FAILED", "peer_gateway_id must differ from source")
    source_doc = await db.gateways.find_one({"gateway_id": gateway_id}, {"_id": 0})
    peer_doc = await db.gateways.find_one({"gateway_id": body.peer_gateway_id}, {"_id": 0})
    if not source_doc or not peer_doc:
        raise not_found("Gateway not found")

    mesh = registry.get("mesh")
    mesh_state = mesh.state()
    if not mesh_state["available"]:
        raise ApiError(503, "MESH_UNAVAILABLE",
                       "Mesh transport is unavailable; relay cannot be attempted")

    source = edge.node(gateway_id)
    peer = edge.node(body.peer_gateway_id)
    pending = source.outbound(limit=body.max_events)
    hops, relayed, failed = [], [], []
    for item in pending:
        env = item["envelope"]
        size_kb = communication.size_kb(env)
        outcome = await mesh.send({"size_kb": size_kb, "payload": env})
        hop = {"event_id": env["event_id"], "transport": "mesh",
               "delivered": outcome["delivered"], "latency_ms": outcome.get("latency_ms"),
               "error": outcome.get("error"), "at": utcnow_iso()}
        hops.append(hop)
        if outcome["delivered"]:
            peer.ingest_inbound([env])
            source.mark_synced([env["event_id"]], state="RELAYED")
            relayed.append(env["event_id"])
            await bus.emit_system(EventType.MESSAGE_SENT.value,
                                  {"event_id": env["event_id"], "transport": "mesh",
                                   "from_gateway_id": gateway_id,
                                   "to_gateway_id": body.peer_gateway_id,
                                   "hop": "mesh_relay", "simulated": mesh.simulated,
                                   "latency_ms": outcome.get("latency_ms")},
                                  priority=str(env.get("priority", "normal")))
        else:
            source.mark_synced([env["event_id"]], state="FAILED",
                               error=outcome.get("error"))
            failed.append({"event_id": env["event_id"], "error": outcome.get("error")})

    upstream = None
    if body.push_upstream and relayed:
        relayed_set = set(relayed)
        relayed_envs = [item["envelope"] for item in pending
                        if item["envelope"]["event_id"] in relayed_set]
        envelopes, seen = [], set()
        for env in relayed_envs:
            eid = env["event_id"]
            if eid in seen:
                continue
            seen.add(eid)
            envelopes.append(await sign_if_delegated(env))
        upstream = await sync_svc.exchange(
            db, device_id=body.peer_gateway_id, actor_id=user["user_id"],
            known_event_ids=peer.known_event_ids(), incoming_events=envelopes,
            max_events=body.max_events)
        peer.mark_synced(upstream["accepted_event_ids"] + upstream["duplicate_event_ids"])
        peer.set_meta("last_sync", utcnow_iso())
        pipelines = await _run_pipelines_for(upstream["accepted_event_ids"],
                                            user["user_id"])
        upstream = {k: upstream[k] for k in
                    ("sync_state", "accepted_event_ids", "duplicate_event_ids",
                     "rejected_events", "more_available")}
        upstream["pipelines_run_for_requests"] = pipelines

    await record(actor_id=user["user_id"], actor_role=user["role"],
                 action="MESH_RELAY", entity_type="gateway", entity_id=gateway_id,
                 detail={"peer": body.peer_gateway_id, "relayed": len(relayed),
                         "failed": len(failed)}, immutable=True)
    return {
        "source_gateway_id": gateway_id,
        "peer_gateway_id": body.peer_gateway_id,
        "transport": "mesh",
        "simulated": mesh.simulated,
        "mesh_state": mesh_state,
        "attempted": len(pending),
        "relayed_event_ids": relayed,
        "failed": failed,
        "hops": hops,
        "identity_preserved": True,
        "upstream": upstream,
        "source_runtime": source.health(),
        "peer_runtime": peer.health(),
    }


@router.post("/gateways/{gateway_id}/queue-local")
async def queue_local_event(gateway_id: str, envelope: dict,
                            user: dict = Depends(require("comm:write"))):
    """Persist an event into a gateway's local SQLite log + outbound queue (offline)."""
    if not await db.gateways.find_one({"gateway_id": gateway_id}, {"_id": 0}):
        raise not_found("Gateway not found")
    node = edge.node(gateway_id)
    result = node.append_event(envelope)
    return {"gateway_id": gateway_id, **result, "edge_runtime": node.health()}


@router.post("/gateways/{gateway_id}/sync")
async def gateway_sync(gateway_id: str, body: SyncBody,
                       user: dict = Depends(require("sync:exchange"))):
    """Gateway relays locally queued edge events upstream and pulls missing events."""
    g = await db.gateways.find_one({"gateway_id": gateway_id}, {"_id": 0})
    if not g:
        raise not_found("Gateway not found")
    node = edge.node(gateway_id)
    for env in body.events:
        node.append_event(env)
    outbound = node.outbound(limit=body.max_events)
    envelopes = [o["envelope"] for o in outbound] + body.events
    seen = set()
    unique = []
    for e in envelopes:
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        unique.append(await sign_if_delegated(e))
    result = await sync_svc.exchange(
        db, device_id=gateway_id, actor_id=user["user_id"],
        known_event_ids=body.known_event_ids or node.known_event_ids(),
        incoming_events=unique, max_events=body.max_events,
        priority_floor=body.priority_floor)
    node.mark_synced(result["accepted_event_ids"] + result["duplicate_event_ids"])
    node.ingest_inbound(result["events"])
    node.set_meta("last_sync", utcnow_iso())
    result["pipelines_run_for_requests"] = await _run_pipelines_for(
        result["accepted_event_ids"], user["user_id"])
    devices = await db.devices.find({}, {"_id": 0, "device_id": 1, "public_key": 1,
                                          "revoked": 1}).to_list(500)
    node.cache_devices(devices)
    return {**result, "gateway_id": gateway_id, "edge_runtime": node.health()}


# ------------------------------------------------------------------------ sync
@router.post("/sync/device")
async def sync_device(body: SyncBody, user: dict = Depends(require("sync:exchange"))):
    """Device-level sync: offline-created events are ingested with identity preserved."""
    ingested = []
    for env in body.events:
        try:
            r = await publish_offline_envelope(env)
        except ApiError as exc:
            r = {"status": "rejected", "reason": exc.code}
        ingested.append({"event_id": env.get("event_id"), "status": r["status"],
                         "reason": r.get("reason"),
                         "signature_verified": (r.get("event") or {}).get("signature_verified")})
    result = await sync_svc.exchange(
        db, device_id=body.device_id, actor_id=user["user_id"],
        known_event_ids=body.known_event_ids, incoming_events=[],
        max_events=body.max_events, priority_floor=body.priority_floor)
    result["ingested"] = ingested
    # run the domain pipeline for any newly accepted rescue requests
    from ..services import operations
    for item, env in zip(ingested, body.events):
        if item["status"] == "applied" and \
                env.get("event_type") == EventType.RESCUE_REQUEST_CREATED.value:
            try:
                await operations.run_pipeline(db, env["payload"]["request_id"],
                                             actor_id=user["user_id"])
            except Exception:
                pass
    return result


@router.get("/sync/status")
async def sync_status(user: dict = Depends(require("sync:exchange"))):
    states = await db.sync_state.find({}, {"_id": 0}).to_list(200)
    unapplied = await db.events.count_documents({"applied": False})
    quarantined = await db.quarantine_events.count_documents({})
    conflicts = await db.sync_conflicts.count_documents({"status": "pending_review"})
    return {"nodes": states, "server_digest": await sync_svc.digest(db),
            "unapplied_events": unapplied, "quarantined_events": quarantined,
            "pending_conflicts": conflicts,
            "connectivity_state": registry.connectivity_state(),
            "field_classes": sync_svc.FIELD_CLASSES}


@router.get("/sync/conflicts")
async def list_conflicts(user: dict = Depends(require("sync:exchange"))):
    docs = await db.sync_conflicts.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return {"items": docs, "count": len(docs)}


@router.post("/sync/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, body: ResolveConflictBody,
                           user: dict = Depends(require("sync:resolve"))):
    doc = await db.sync_conflicts.find_one({"conflict_id": conflict_id}, {"_id": 0})
    if not doc:
        raise not_found("Conflict not found")
    if doc["status"] == "resolved":
        raise ApiError(409, "ALREADY_DECIDED", "Conflict already resolved")
    if body.resolution not in ("keep_local", "accept_remote", "manual"):
        raise ApiError(422, "VALIDATION_FAILED", "Invalid resolution")
    await db.sync_conflicts.update_one(
        {"conflict_id": conflict_id},
        {"$set": {"status": "resolved", "resolved_by": user["user_id"],
                  "human_resolution": body.resolution, "resolution_reason": body.reason,
                  "resolved_at": utcnow_iso()}})
    from ..audit import record
    await record(actor_id=user["user_id"], actor_role=user["role"],
                 action="SYNC_CONFLICT_RESOLVED", entity_type="sync_conflict",
                 entity_id=conflict_id, decision=body.resolution, immutable=True)
    return {"conflict_id": conflict_id, "status": "resolved",
            "resolution": body.resolution}


@router.get("/sync/digest")
async def get_digest(since_hlc: Optional[str] = None,
                     user: dict = Depends(require("sync:exchange"))):
    return await sync_svc.digest(db, since_hlc)
