import uuid
from typing import Optional

from .db import get_db
from .models import utcnow_iso
from .ulid import new_ulid


async def record(
    *,
    actor_id: Optional[str],
    actor_role: Optional[str],
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    device_id: Optional[str] = None,
    event_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    decision: Optional[str] = None,
    outcome: str = "success",
    detail: Optional[dict] = None,
    simulation: bool = False,
    immutable: bool = False,
):
    entry = {
        "entry_id": new_ulid(),
        "actor_id": actor_id,
        "actor_role": actor_role,
        "device_id": device_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "event_id": event_id,
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "decision": decision,
        "outcome": outcome,
        "detail": detail or {},
        "immutable": immutable,
        "created_at": utcnow_iso(),
    }
    await get_db(simulation).audit_log.insert_one(dict(entry))
    entry.pop("_id", None)
    return entry


async def record_unauthorized_attempt(user: dict, permission: str):
    from .events import bus
    await record(
        actor_id=user.get("user_id"), actor_role=user.get("role"),
        action="UNAUTHORIZED_ACTION_ATTEMPT", entity_type="permission",
        entity_id=permission, outcome="denied", immutable=True)
    await bus.emit_system(
        "UNAUTHORIZED_ACTION_ATTEMPT",
        {"actor_id": user.get("user_id"), "role": user.get("role"), "permission": permission},
        priority="high")
