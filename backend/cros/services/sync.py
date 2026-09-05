"""Synchronization: digest exchange, priority-aware partial transfer, conflict handling."""
from __future__ import annotations

from ..constants import (FIELD_CLASS_APPEND_ONLY, FIELD_CLASS_COUNTER, FIELD_CLASS_LWW,
                         FIELD_CLASS_STATE_MACHINE, MISSION_TRANSITIONS, EventType,
                         MissionStatus)
from ..events import NODE_ID, bus
from ..hlc import compare
from ..models import utcnow_iso
from ..ulid import new_ulid
from .transport import PRIORITY_RANK

FIELD_CLASSES = {
    "mission.status": FIELD_CLASS_STATE_MACHINE,
    "incident.status": FIELD_CLASS_STATE_MACHINE,
    "emergency_request.status": FIELD_CLASS_STATE_MACHINE,
    "facility.counters": FIELD_CLASS_COUNTER,
    "emergency_request.description": FIELD_CLASS_LWW,
    "resource.status": FIELD_CLASS_LWW,
    "responder.location": FIELD_CLASS_LWW,
    "reports": FIELD_CLASS_APPEND_ONLY,
    "events": FIELD_CLASS_APPEND_ONLY,
    "approvals": FIELD_CLASS_APPEND_ONLY,
}

MISSION_STAGE_ORDER = [
    MissionStatus.PROPOSED.value, MissionStatus.REJECTED.value, MissionStatus.APPROVED.value,
    MissionStatus.DISPATCHED.value, MissionStatus.DECLINED.value,
    MissionStatus.ACCEPTED_BY_RESPONDER.value, MissionStatus.EN_ROUTE.value,
    MissionStatus.ON_SCENE.value, MissionStatus.COMPLETED.value, MissionStatus.FAILED.value,
]


def field_class(path: str) -> str:
    return FIELD_CLASSES.get(path, FIELD_CLASS_LWW)


def resolve(path: str, local: dict, remote: dict) -> dict:
    """Deterministic conflict resolution by field class."""
    fc = field_class(path)
    if fc == FIELD_CLASS_APPEND_ONLY:
        return {"winner": "both", "field_class": fc, "reason": "append_only_union"}
    if fc == FIELD_CLASS_COUNTER:
        return {"winner": "merge", "field_class": fc, "reason": "crdt_counter_fold"}
    if fc == FIELD_CLASS_STATE_MACHINE:
        try:
            li = MISSION_STAGE_ORDER.index(local.get("value"))
            ri = MISSION_STAGE_ORDER.index(remote.get("value"))
        except ValueError:
            return {"winner": "local", "field_class": fc, "reason": "unknown_stage"}
        if ri > li:
            return {"winner": "remote", "field_class": fc, "reason": "monotonic_stage_advance"}
        if ri == li:
            return {"winner": "local", "field_class": fc, "reason": "same_stage"}
        return {"winner": "local", "field_class": fc,
                "reason": "regression_rejected_requires_review", "requires_review": True}
    winner = "remote" if compare(remote.get("hlc", "0.0.x"),
                                 local.get("hlc", "0.0.x")) > 0 else "local"
    return {"winner": winner, "field_class": fc, "reason": "hlc_last_write_wins"}


async def record_conflict(db, *, entity_type: str, entity_id: str, path: str,
                          local: dict, remote: dict, resolution: dict,
                          event_id: str | None = None) -> dict:
    conflict = {
        "conflict_id": new_ulid(),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "path": path,
        "field_class": resolution.get("field_class"),
        "local": local,
        "remote": remote,
        "resolution": resolution,
        "event_id": event_id,
        "status": "pending_review" if resolution.get("requires_review") else "auto_resolved",
        "created_at": utcnow_iso(),
    }
    await db.sync_conflicts.insert_one(dict(conflict))
    conflict.pop("_id", None)
    await bus.emit_system(EventType.SYNC_CONFLICT_DETECTED.value,
                          {"conflict_id": conflict["conflict_id"], "entity_type": entity_type,
                           "entity_id": entity_id, "path": path,
                           "resolution": resolution}, priority="high")
    return conflict


async def digest(db, since_hlc: str | None = None, limit: int = 2000) -> dict:
    q = {}
    events = await db.events.find(
        q, {"_id": 0, "event_id": 1, "logical_timestamp": 1, "event_type": 1,
            "priority": 1}).sort("logical_timestamp", -1).to_list(limit)
    if since_hlc:
        events = [e for e in events if compare(e["logical_timestamp"], since_hlc) > 0]
    return {
        "node_id": NODE_ID,
        "event_count": len(events),
        "latest_hlc": events[0]["logical_timestamp"] if events else "0.0." + NODE_ID,
        "event_ids": [e["event_id"] for e in events],
        "generated_at": utcnow_iso(),
    }


async def exchange(db, *, device_id: str, actor_id: str | None, known_event_ids: list[str],
                   incoming_events: list[dict], max_events: int = 50,
                   priority_floor: str = "low", simulation: bool = False) -> dict:
    """Full sync round-trip: ingest remote events, then return missing events (partial)."""
    correlation_id = new_ulid()
    await bus.emit_system(EventType.SYNC_STARTED.value,
                          {"device_id": device_id, "incoming": len(incoming_events),
                           "known": len(known_event_ids)}, simulation=simulation)

    accepted, duplicates, rejected = [], [], []
    for env in incoming_events:
        result = await bus.publish(env, simulation=simulation)
        if result["status"] == "applied":
            accepted.append(env.get("event_id"))
        elif result["status"] == "duplicate":
            duplicates.append(env.get("event_id"))
        else:
            rejected.append({"event_id": env.get("event_id"), "reason": result.get("reason")})

    await bus.emit_system(EventType.SYNC_DIGEST_EXCHANGED.value,
                          {"device_id": device_id, "known_count": len(known_event_ids)},
                          simulation=simulation)

    known = set(known_event_ids)
    floor = PRIORITY_RANK.get(priority_floor, 0)
    candidates = await db.events.find(
        {"event_id": {"$nin": list(known)[:5000]}},
        {"_id": 0}).sort("logical_timestamp", 1).to_list(4000)
    candidates = [c for c in candidates
                  if PRIORITY_RANK.get(str(c.get("priority", "normal")), 1) >= floor]
    candidates.sort(key=lambda c: (-PRIORITY_RANK.get(str(c.get("priority", "normal")), 1),
                                   c["logical_timestamp"]))
    batch = candidates[:max_events]
    # Apply in causal order within the batch.
    batch.sort(key=lambda c: c["logical_timestamp"])
    more = len(candidates) > len(batch)

    if batch:
        await bus.emit_system(EventType.SYNC_EVENTS_REQUESTED.value,
                              {"device_id": device_id, "count": len(batch)},
                              simulation=simulation)

    state = "SYNCED" if not more else "SYNCING"
    await db.sync_state.update_one(
        {"device_id": device_id},
        {"$set": {"device_id": device_id, "actor_id": actor_id, "state": state,
                  "last_sync_at": utcnow_iso(), "pending_remote": len(candidates) - len(batch),
                  "accepted_last_round": len(accepted)}},
        upsert=True)
    await bus.emit_system(EventType.SYNC_COMPLETED.value,
                          {"device_id": device_id, "accepted": len(accepted),
                           "duplicates": len(duplicates), "rejected": len(rejected),
                           "sent": len(batch), "more_available": more},
                          simulation=simulation)
    return {
        "correlation_id": correlation_id,
        "sync_state": state,
        "accepted_event_ids": accepted,
        "duplicate_event_ids": duplicates,
        "rejected_events": rejected,
        "events": batch,
        "more_available": more,
        "pending_remote": len(candidates) - len(batch),
        "server_digest": await digest(db),
    }
