from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import require, role_permissions, has_permission
from ..constants import EventType, Priority, Role
from ..db import db
from ..errors import ApiError, not_found
from ..events import bus
from ..models import utcnow_iso
from ..observability import agent_invocations, counters, spans
from ..config import PERSISTENCE_BACKEND
from ..realtime import manager
from ..services.transport import registry

router = APIRouter(tags=["admin-audit-observability"])


class RoleBody(BaseModel):
    role: str


class RevokeBody(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class DisableBody(BaseModel):
    disabled: bool


# ----------------------------------------------------------------------- users
@router.get("/users")
async def list_users(role: Optional[str] = None,
                     user: dict = Depends(require("user:admin"))):
    q = {"role": role} if role else {}
    docs = await db.users.find(q, {"_id": 0, "password_hash": 0}).to_list(500)
    for d in docs:
        d["permissions"] = role_permissions(d["role"])
    return {"items": docs, "count": len(docs), "roles": [r.value for r in Role]}


@router.get("/users/{user_id}")
async def get_user(user_id: str, user: dict = Depends(require("user:admin"))):
    doc = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    if not doc:
        raise not_found("User not found")
    doc["permissions"] = role_permissions(doc["role"])
    return doc


@router.post("/users/{user_id}/role")
async def set_role(user_id: str, body: RoleBody, user: dict = Depends(require("user:admin"))):
    if body.role not in [r.value for r in Role]:
        raise ApiError(422, "VALIDATION_FAILED", "Unknown role")
    res = await db.users.update_one({"user_id": user_id}, {"$set": {"role": body.role}})
    if res.matched_count == 0:
        raise not_found("User not found")
    from ..audit import record
    await record(actor_id=user["user_id"], actor_role=user["role"], action="ROLE_CHANGED",
                 entity_type="user", entity_id=user_id, detail={"new_role": body.role},
                 immutable=True)
    return {"user_id": user_id, "role": body.role,
            "permissions": role_permissions(body.role),
            "note": "Platform administration does not grant operational authority"}


@router.post("/users/{user_id}/disable")
async def disable_user(user_id: str, body: DisableBody,
                       user: dict = Depends(require("user:admin"))):
    res = await db.users.update_one({"user_id": user_id},
                                     {"$set": {"disabled": body.disabled}})
    if res.matched_count == 0:
        raise not_found("User not found")
    return {"user_id": user_id, "disabled": body.disabled}


@router.get("/organizations")
async def list_orgs(user: dict = Depends(require("org:read"))):
    docs = await db.organizations.find({}, {"_id": 0}).to_list(100)
    return {"items": docs, "count": len(docs)}


@router.get("/organizations/{org_id}")
async def get_org(org_id: str, user: dict = Depends(require("org:read"))):
    doc = await db.organizations.find_one({"org_id": org_id}, {"_id": 0})
    if not doc:
        raise not_found("Organization not found")
    return doc


# --------------------------------------------------------------------- devices
@router.get("/devices")
async def list_devices(user: dict = Depends(require("device:admin"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        docs = await postgres.list_devices()
    else:
        docs = await db.devices.find({}, {"_id": 0}).to_list(500)
    for d in docs:
        d["private_key_stored_in_database"] = False
        d["simulated_signer"] = d.get("signing_mode") == "server_keystore"
    return {"items": docs, "count": len(docs)}


@router.get("/devices/{device_id}")
async def get_device(device_id: str, user: dict = Depends(require("device:admin"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        doc = await postgres.find_device(device_id)
    else:
        doc = await db.devices.find_one({"device_id": device_id}, {"_id": 0})
    if not doc:
        raise not_found("Device not found")
    return doc


@router.get("/devices/{device_id}/trust-status")
async def trust_status(device_id: str, user: dict = Depends(require("device:admin"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        doc = await postgres.find_device(device_id)
    else:
        doc = await db.devices.find_one({"device_id": device_id}, {"_id": 0})
    if not doc:
        raise not_found("Device not found")
    failures = await db.quarantine_events.count_documents(
        {"envelope.origin_device_id": device_id})
    accepted = await db.events.count_documents({"origin_device_id": device_id})
    return {"device_id": device_id, "trust_level": doc.get("trust_level"),
            "revoked": bool(doc.get("revoked")), "accepted_events": accepted,
            "rejected_events": failures,
            "signing_mode": doc.get("signing_mode")}


@router.post("/devices/{device_id}/revoke")
async def revoke_device(device_id: str, body: RevokeBody,
                        user: dict = Depends(require("device:admin"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        doc = await postgres.find_device(device_id)
    else:
        doc = await db.devices.find_one({"device_id": device_id}, {"_id": 0})
    if not doc:
        raise not_found("Device not found")
    if doc.get("revoked"):
        raise ApiError(409, "ALREADY_DECIDED", "Device credential already revoked")
    result = await bus.emit_system(EventType.DEVICE_CREDENTIAL_REVOKED.value,
                                   {"device_id": device_id, "reason": body.reason,
                                    "revoked_at": utcnow_iso()},
                                   priority=Priority.HIGH.value,
                                   origin_actor_id=user["user_id"])
    return {"device_id": device_id, "revoked": True, "event_id": result.get("event_id")}


@router.get("/devices/{device_id}/revocation-status")
async def revocation_status(device_id: str, user: dict = Depends(require("device:admin"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        doc = await postgres.find_device(device_id)
    else:
        doc = await db.devices.find_one({"device_id": device_id}, {"_id": 0})
    if not doc:
        raise not_found("Device not found")
    return {"device_id": device_id, "revoked": bool(doc.get("revoked")),
            "reason": doc.get("revoked_reason"), "revoked_at": doc.get("revoked_at")}


# ----------------------------------------------------------------------- audit
@router.get("/audit")
async def audit(action: Optional[str] = None, entity_id: Optional[str] = None,
                actor_id: Optional[str] = None, limit: int = Query(200, le=1000),
                user: dict = Depends(require("audit:read"))):
    q = {}
    if action:
        q["action"] = action
    if entity_id:
        q["entity_id"] = entity_id
    if actor_id:
        q["actor_id"] = actor_id
    docs = await db.audit_log.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return {"items": docs, "count": len(docs), "immutable": True,
            "note": "Audit records are append-only; no API can edit them"}


@router.get("/audit/{entry_id}")
async def audit_entry(entry_id: str, user: dict = Depends(require("audit:read"))):
    doc = await db.audit_log.find_one({"entry_id": entry_id}, {"_id": 0})
    if not doc:
        raise not_found("Audit entry not found")
    return doc


@router.get("/events")
async def list_events(event_type: Optional[str] = None, limit: int = Query(100, le=1000),
                      user: dict = Depends(require("audit:read"))):
    q = {"event_type": event_type} if event_type else {}
    docs = await db.events.find(q, {"_id": 0}).sort("logical_timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


# --------------------------------------------------------------- observability
@router.get("/observability/agent-invocations")
async def agent_invocation_log(user: dict = Depends(require("observability:read"))):
    return {"items": agent_invocations(200)}


@router.get("/observability/connectivity-state")
async def connectivity_state(user: dict = Depends(require("observability:read"))):
    from ..services.communication import queue_depth
    return {"connectivity_state": registry.connectivity_state(),
            "transports": registry.states(),
            "queue": await queue_depth(db),
            "realtime_clients": manager.client_count(),
            "checked_at": utcnow_iso()}


@router.get("/observability/metrics")
async def metrics(user: dict = Depends(require("observability:read"))):
    return {"counters": counters(), "spans": spans(60),
            "handlers_registered": bus.handler_count()}


@router.get("/system/health")
async def system_health(user: dict = Depends(require("observability:read"))):
    checks = {}
    try:
        await db.command("ping")
        checks["database"] = {"status": "up", "engine": "mongodb", "name": db.name}
    except Exception as exc:
        checks["database"] = {"status": "down", "error": str(exc)[:200]}
    from ..services.agents import provider
    checks["llm_provider"] = {"status": "configured" if provider.available else "unconfigured",
                              "provider": provider.provider, "model": provider.model,
                              "fallback": "deterministic_rules"}
    checks["event_bus"] = {"status": "up", "kind": "mongodb_append_only_log",
                           "handlers": bus.handler_count(),
                           "persisted_events": await db.events.count_documents({}),
                           "unapplied": await db.events.count_documents({"applied": False}),
                           "quarantined": await db.quarantine_events.count_documents({}),
                           "projection_errors":
                               await db.projection_errors.count_documents({})}
    checks["communication"] = {"connectivity_state": registry.connectivity_state(),
                               "simulated_transports":
                                   [s["name"] for s in registry.states() if s["simulated"]]}
    checks["realtime"] = {"clients": manager.client_count(),
                          "replay_buffer_seq": manager.latest_seq}
    return {"checks": checks, "checked_at": utcnow_iso()}
