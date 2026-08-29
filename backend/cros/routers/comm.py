from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import require
from ..constants import EventType, Priority
from ..db import db
from ..errors import ApiError, not_found
from ..events import bus
from ..models import utcnow_iso
from ..services import communication, sync as sync_svc
from ..services.transport import registry, score_transport, select_transport
from ..edge import gateway as edge
from .deps import publish_offline_envelope

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


class GatewayConfigBody(BaseModel):
    connectivity_state: Optional[str] = None
    battery_pct: Optional[int] = None
    mesh_roster: Optional[list[str]] = None


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
        unique.append(e)
    result = await sync_svc.exchange(
        db, device_id=gateway_id, actor_id=user["user_id"],
        known_event_ids=body.known_event_ids or node.known_event_ids(),
        incoming_events=unique, max_events=body.max_events,
        priority_floor=body.priority_floor)
    node.mark_synced(result["accepted_event_ids"] + result["duplicate_event_ids"])
    node.ingest_inbound(result["events"])
    node.set_meta("last_sync", utcnow_iso())
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
                         "reason": r.get("reason")})
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
