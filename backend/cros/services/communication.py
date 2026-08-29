"""Priority-aware outbound message queue + delivery over the transport abstraction."""
import json

from ..constants import EventType, Priority
from ..events import bus
from ..models import utcnow_iso
from ..observability import incr
from ..ulid import new_ulid
from .transport import PRIORITY_RANK, registry, select_transport


def size_kb(payload: dict) -> float:
    return round(len(json.dumps(payload, default=str).encode()) / 1024.0, 4)


async def enqueue(db, *, envelope: dict, gateway_id: str | None = None,
                  require_ack: bool = True, simulation: bool = False) -> dict:
    msg = {
        "message_id": new_ulid(),
        "event_id": envelope.get("event_id"),
        "event_type": envelope.get("event_type"),
        "priority": str(envelope.get("priority", Priority.NORMAL.value)),
        "size_kb": size_kb(envelope),
        "state": "QUEUED",
        "attempts": 0,
        "transport": None,
        "gateway_id": gateway_id,
        "require_ack": require_ack,
        "ttl_seconds": envelope.get("ttl_seconds", 86400),
        "queued_at": utcnow_iso(),
        "sent_at": None,
        "delivered_at": None,
        "last_error": None,
        "hops": [],
        "envelope": envelope,
    }
    await db.comm_messages.insert_one(dict(msg))
    msg.pop("_id", None)
    await bus.emit_system(EventType.MESSAGE_QUEUED.value,
                          {"message_id": msg["message_id"], "event_id": msg["event_id"],
                           "priority": msg["priority"], "size_kb": msg["size_kb"],
                           "gateway_id": gateway_id},
                          priority=msg["priority"], simulation=simulation)
    incr("comm.queued")
    return msg


async def flush(db, limit: int = 25, simulation: bool = False) -> dict:
    """Attempt delivery of queued messages, highest priority first."""
    pending = await db.comm_messages.find(
        {"state": {"$in": ["QUEUED", "FAILED"]}}, {"_id": 0}).to_list(500)
    pending.sort(key=lambda m: (-PRIORITY_RANK.get(m["priority"], 1), m["queued_at"]))
    states = registry.states()
    results = []
    for msg in pending[:limit]:
        chosen, scoring = select_transport(states, msg["priority"], msg["size_kb"],
                                          msg.get("require_ack", True))
        if not chosen:
            await db.comm_messages.update_one(
                {"message_id": msg["message_id"]},
                {"$set": {"state": "QUEUED", "last_error": "NO_ELIGIBLE_TRANSPORT",
                          "transport_scoring": scoring},
                 "$inc": {"attempts": 1}})
            results.append({"message_id": msg["message_id"], "delivered": False,
                            "error": "NO_ELIGIBLE_TRANSPORT"})
            continue
        transport = registry.get(chosen)
        outcome = await transport.send({"size_kb": msg["size_kb"], "payload": msg["envelope"]})
        hop = {"transport": chosen, "at": utcnow_iso(), "delivered": outcome["delivered"],
               "latency_ms": outcome.get("latency_ms"), "simulated": outcome.get("simulated"),
               "error": outcome.get("error")}
        if outcome["delivered"]:
            await db.comm_messages.update_one(
                {"message_id": msg["message_id"]},
                {"$set": {"state": "DELIVERED", "transport": chosen,
                          "sent_at": utcnow_iso(), "delivered_at": utcnow_iso(),
                          "last_error": None, "transport_scoring": scoring},
                 "$inc": {"attempts": 1}, "$push": {"hops": hop}})
            await bus.emit_system(EventType.MESSAGE_SENT.value,
                                  {"message_id": msg["message_id"], "transport": chosen,
                                   "event_id": msg["event_id"], "simulated": transport.simulated},
                                  priority=msg["priority"], simulation=simulation)
            if outcome.get("acknowledged"):
                await bus.emit_system(EventType.MESSAGE_DELIVERY_CONFIRMED.value,
                                      {"message_id": msg["message_id"], "transport": chosen,
                                       "latency_ms": outcome.get("latency_ms")},
                                      priority=msg["priority"], simulation=simulation)
            incr("comm.delivered")
        else:
            await db.comm_messages.update_one(
                {"message_id": msg["message_id"]},
                {"$set": {"state": "FAILED", "transport": chosen,
                          "last_error": outcome.get("error"), "transport_scoring": scoring},
                 "$inc": {"attempts": 1}, "$push": {"hops": hop}})
            await bus.emit_system(EventType.MESSAGE_DELIVERY_FAILED.value,
                                  {"message_id": msg["message_id"], "transport": chosen,
                                   "error": outcome.get("error")},
                                  priority=msg["priority"], simulation=simulation)
            incr("comm.failed")
        results.append({"message_id": msg["message_id"], "transport": chosen,
                        "delivered": outcome["delivered"], "error": outcome.get("error")})
    return {"attempted": len(results), "results": results,
            "connectivity_state": registry.connectivity_state()}


async def queue_depth(db) -> dict:
    out = {}
    for state in ("QUEUED", "SENT", "DELIVERED", "FAILED", "EXPIRED"):
        out[state] = await db.comm_messages.count_documents({"state": state})
    by_priority = {}
    for p in ("critical", "high", "normal", "low"):
        by_priority[p] = await db.comm_messages.count_documents(
            {"state": {"$in": ["QUEUED", "FAILED"]}, "priority": p})
    out["pending_by_priority"] = by_priority
    return out
