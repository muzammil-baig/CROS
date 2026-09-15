"""Shared router helpers: event ingestion, idempotency, privacy scoping."""
from __future__ import annotations

import copy
import uuid
from typing import Optional

from fastapi import Header

from .. import edge_keystore
from ..auth import has_permission
from ..config import PERSISTENCE_BACKEND
from ..db import db as prod_db
from ..errors import ApiError
from ..events import bus
from ..models import utcnow_iso
from ..crypto import verify
from ..ulid import new_ulid


async def default_device(user: dict) -> str:
    """Every actor has a registered signing device; provision on first use."""
    device_id = f"DEV-{user['user_id']}"
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        doc = await postgres.find_device(device_id)
    else:
        doc = await prod_db.devices.find_one({"device_id": device_id})
    pub = edge_keystore.provision(device_id)
    if not doc or doc.get("public_key") != pub:
        from ..constants import EventType
        if PERSISTENCE_BACKEND == "postgres":
            from .. import postgres
            await postgres.upsert_device(
                device_id=device_id, owner_user_id=user["user_id"], public_key=pub,
                signing_mode="server_keystore", trust_level="provisional")
        else:
            await bus.emit_system(EventType.DEVICE_REGISTERED.value,
                                  {"device_id": device_id, "public_key": pub,
                                   "owner_user_id": user["user_id"],
                                   "device_type": "web_client",
                                   "signing_mode": "server_keystore",
                                   "trust_level": "provisional"})
    elif doc.get("revoked"):
        raise ApiError(403, "DEVICE_REVOKED", "Signing device credential revoked")
    return device_id


async def emit_client_event(*, event_type: str, payload: dict, user: dict,
                            device_id: Optional[str] = None, priority: str = "normal",
                            event_id: Optional[str] = None,
                            correlation_id: Optional[str] = None,
                            causal_parent_ids: Optional[list[str]] = None,
                            simulation: bool = False) -> dict:
    """Create a signed, idempotent event on behalf of an authenticated actor."""
    device_id = device_id or await default_device(user)
    if not edge_keystore.get_private_key(device_id):
        raise ApiError(409, "DEVICE_NOT_PROVISIONED",
                       "Device has no server-side signing material; sign client-side instead")
    env = bus.build_envelope(event_type, payload, origin_device_id=device_id,
                             origin_actor_id=user["user_id"], priority=priority,
                             correlation_id=correlation_id or str(uuid.uuid4()),
                             causal_parent_ids=causal_parent_ids or [],
                             event_id=event_id or new_ulid())
    result = await bus.publish(env, simulation=simulation, trusted=False)
    if result["status"] == "rejected":
        raise ApiError(422, "EVENT_REJECTED", f"Event rejected: {result.get('reason')}",
                       [{"field": "event", "issue": str(result.get("reason"))}])
    return result


ENVELOPE_FIELDS = ("event_id", "event_type", "schema_version", "origin_device_id",
                   "origin_actor_id", "logical_timestamp", "wall_clock_timestamp",
                   "causal_parent_ids", "priority", "ttl_seconds", "correlation_id",
                   "signature", "payload")


async def sign_if_delegated(envelope: dict) -> dict:
    """Normalise an envelope coming back from an edge node and sign it if delegated.

    Edge nodes may have cached cloud-side annotations (received_at,
    signature_verified, ...). Those are stripped so the strict event contract
    still validates; the logical identity is untouched.
    """
    envelope = {k: v for k, v in envelope.items() if k in ENVELOPE_FIELDS}
    if envelope.get("signature"):
        return envelope
    device_id = envelope.get("origin_device_id")
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        device = await postgres.find_device(device_id)
    else:
        device = await prod_db.devices.find_one({"device_id": device_id}, {"_id": 0})
    if device and not device.get("revoked") and \
            device.get("signing_mode") == "server_keystore":
        signature = edge_keystore.sign_envelope(device_id, envelope)
        if signature and device.get("public_key") and verify(device["public_key"], envelope, signature):
            envelope["signature"] = signature
        else:
            public_key = edge_keystore.provision(device_id)
            envelope["signature"] = edge_keystore.sign_envelope(device_id, envelope)
            if PERSISTENCE_BACKEND == "postgres":
                from .. import postgres
                await postgres.upsert_device(
                    device_id=device_id, owner_user_id=device.get("owner_user_id"),
                    public_key=public_key, signing_mode="server_keystore",
                    trust_level=device.get("trust_level", "provisional"))
    return envelope


async def publish_offline_envelope(envelope: dict, *, simulation: bool = False) -> dict:
    """Ingest an envelope created while the client was offline.

    The logical event identity (event_id, HLC, causal parents) is preserved
    exactly; no new event id is minted. If the device uses server-delegated
    signing (SIMULATED_SIGNER), the signature is applied here.
    """
    envelope = {k: v for k, v in envelope.items() if k in ENVELOPE_FIELDS}
    device_id = envelope.get("origin_device_id")
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        device = await postgres.find_device(device_id)
    else:
        device = await prod_db.devices.find_one({"device_id": device_id}, {"_id": 0})
    if device and device.get("revoked"):
        raise ApiError(403, "DEVICE_REVOKED", "Signing device credential revoked")
    if not envelope.get("signature") and device and \
            device.get("signing_mode") == "server_keystore":
        envelope["signature"] = edge_keystore.sign_envelope(device_id, envelope)
    return await bus.publish(envelope, simulation=simulation)


# ---------------------------------------------------------------- privacy
def scope_request(doc: dict, user: dict) -> dict:
    """Backend-enforced data minimisation. Never rely on frontend filtering."""
    out = copy.deepcopy(doc)
    if not has_permission(user["role"], "pii:read"):
        out.pop("reporter_phone", None)
        out.pop("reporter_name", None)
        out["reporter_redacted"] = True
    if not has_permission(user["role"], "location:precise"):
        loc = out.get("location") or {}
        if loc.get("coordinates"):
            out["location"] = {"type": "Point",
                               "coordinates": [round(loc["coordinates"][0], 3),
                                               round(loc["coordinates"][1], 3)]}
            out["location_precision"] = "coarse_100m"
        out.pop("geo", None)
    else:
        out["location_precision"] = "precise"
    if user["role"] == "citizen" and out.get("reporter_user_id") != user["user_id"]:
        raise ApiError(404, "NOT_FOUND", "Resource not found")
    return out


def scope_responder(doc: dict, user: dict) -> dict:
    out = copy.deepcopy(doc)
    if not has_permission(user["role"], "location:precise"):
        loc = out.get("location") or {}
        if loc.get("coordinates"):
            out["location"] = {"type": "Point",
                               "coordinates": [round(loc["coordinates"][0], 2),
                                               round(loc["coordinates"][1], 2)]}
            out["location_precision"] = "coarse_1km"
    return out


def stale_flag(iso: str | None, threshold_seconds: int = 300) -> bool:
    from datetime import datetime, timezone
    if not iso:
        return True
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() > threshold_seconds
    except Exception:
        return True
