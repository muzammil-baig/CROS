"""CROS event bus.

Append-only event log persisted in MongoDB (production DB and a physically
separate simulation DB), with idempotent consumers, signature verification,
HLC merge, TTL enforcement and realtime fan-out.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pydantic import ValidationError

from . import crypto, edge_keystore
from .constants import TRUSTED_ONLY_EVENTS, EventType, Priority
from .db import get_db, db as prod_db
from .hlc import HybridLogicalClock
from .models import EventEnvelope, utcnow_iso
from .observability import incr, span
from .realtime import manager
from .ulid import new_ulid
from .config import PERSISTENCE_BACKEND
from .postgres import insert_event as insert_postgres_event

logger = logging.getLogger("cros.events")

NODE_ID = f"cloud-{socket.gethostname()[:12]}"
CLOUD_DEVICE_ID = "cloud-service-node"
hlc = HybridLogicalClock(NODE_ID)

Handler = Callable[[object, dict], Awaitable[None]]


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Handler]] = {}

    def on(self, *event_types: str):
        def deco(fn: Handler):
            for et in event_types:
                self._handlers.setdefault(str(et), []).append(fn)
            return fn
        return deco

    def handler_count(self) -> int:
        return sum(len(v) for v in self._handlers.values())

    # ------------------------------------------------------------------ build
    def build_envelope(self, event_type: str, payload: dict, *,
                       origin_device_id: str = CLOUD_DEVICE_ID,
                       origin_actor_id: str | None = None,
                       priority: str = Priority.NORMAL.value,
                       ttl_seconds: int = 86400,
                       correlation_id: str | None = None,
                       causal_parent_ids: list[str] | None = None,
                       event_id: str | None = None) -> dict:
        env = {
            "event_id": event_id or new_ulid(),
            "event_type": str(event_type),
            "schema_version": 1,
            "origin_device_id": origin_device_id,
            "origin_actor_id": origin_actor_id,
            "logical_timestamp": hlc.now(),
            "wall_clock_timestamp": utcnow_iso(),
            "causal_parent_ids": causal_parent_ids or [],
            "priority": priority,
            "ttl_seconds": ttl_seconds,
            "correlation_id": correlation_id or str(uuid.uuid4()),
            "payload": payload,
        }
        env["signature"] = edge_keystore.sign_envelope(origin_device_id, env)
        return env

    async def emit_system(self, event_type, payload: dict, **kw) -> dict:
        """Create and publish an event originating from the trusted cloud node."""
        simulation = kw.pop("simulation", False)
        env = self.build_envelope(str(event_type), payload, **kw)
        return await self.publish(env, simulation=simulation, trusted=True)

    # ---------------------------------------------------------------- publish
    async def publish(self, envelope: dict, *, simulation: bool = False,
                      trusted: bool = False) -> dict:
        db = get_db(simulation)
        with span("event.publish", event_type=envelope.get("event_type"),
                  event_id=envelope.get("event_id")):
            try:
                env = EventEnvelope.model_validate(envelope).model_dump()
            except ValidationError as e:
                incr("events.malformed")
                if PERSISTENCE_BACKEND != "postgres":
                    await db.quarantine_events.insert_one(
                        {"envelope": envelope, "reason": "MALFORMED",
                         "errors": e.errors(include_url=False), "at": utcnow_iso()})
                return {"status": "rejected", "reason": "MALFORMED_ENVELOPE",
                        "errors": e.errors(include_url=False)}

            # PostgreSQL enforces event idempotency with a unique event_id constraint.
            if PERSISTENCE_BACKEND != "postgres":
                existing = await db.events.find_one({"event_id": env["event_id"]}, {"_id": 0})
                if existing:
                    incr("events.duplicate")
                    return {"status": "duplicate", "event_id": env["event_id"],
                            "event": _clean(existing)}

            # ---- TTL / expiry
            try:
                wall = datetime.fromisoformat(env["wall_clock_timestamp"].replace("Z", "+00:00"))
                if wall.tzinfo is None:
                    wall = wall.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - wall).total_seconds()
            except Exception:
                age = 0
            if env["ttl_seconds"] and age > env["ttl_seconds"]:
                incr("events.expired")
                await self.emit_system(EventType.MESSAGE_EXPIRED.value,
                                       {"event_id": env["event_id"], "age_seconds": age},
                                       simulation=simulation)
                return {"status": "rejected", "reason": "EVENT_EXPIRED"}

            # ---- signature verification (device trust boundary)
            verified = False
            if trusted and env["origin_device_id"] == CLOUD_DEVICE_ID:
                verified = PERSISTENCE_BACKEND == "postgres" or (
                    env.get("signature") is not None and crypto.verify(
                        await _cloud_public_key(), env, env["signature"]))
            else:
                verified, reason = await _verify_device_signature(env)
                if not verified:
                    incr("events.signature_failed")
                    await db.quarantine_events.insert_one(
                        {"envelope": env, "reason": reason, "at": utcnow_iso()})
                    if env["event_type"] != EventType.SIGNATURE_VERIFICATION_FAILED.value:
                        await self.emit_system(
                            EventType.SIGNATURE_VERIFICATION_FAILED.value,
                            {"event_id": env["event_id"], "event_type": env["event_type"],
                             "origin_device_id": env["origin_device_id"], "reason": reason},
                            priority=Priority.HIGH.value, simulation=simulation)
                    return {"status": "rejected", "reason": reason}

            if not verified and str(env["event_type"]) in {str(e) for e in TRUSTED_ONLY_EVENTS}:
                return {"status": "rejected", "reason": "UNTRUSTED_ORIGIN"}

            # ---- HLC merge for causal ordering
            try:
                hlc.update(env["logical_timestamp"])
            except ValueError:
                return {"status": "rejected", "reason": "INVALID_HLC"}

            env["received_at"] = utcnow_iso()
            env["signature_verified"] = verified
            env["simulation"] = simulation
            env["applied"] = False
            if PERSISTENCE_BACKEND == "postgres":
                postgres_envelope = dict(env)
                postgres_envelope["logical_timestamp"] = postgres_envelope.pop("logical_timestamp")
                postgres_envelope["wall_clock_timestamp"] = datetime.fromisoformat(
                    postgres_envelope["wall_clock_timestamp"].replace("Z", "+00:00")
                )
                postgres_envelope["received_at"] = datetime.fromisoformat(
                    postgres_envelope["received_at"].replace("Z", "+00:00")
                )
                try:
                    inserted = await insert_postgres_event(postgres_envelope)
                except Exception:
                    logger.exception("postgres event persistence failed: %s", env["event_id"])
                    raise
                if not inserted:
                    incr("events.duplicate")
                    return {"status": "duplicate", "event_id": env["event_id"]}
            else:
                try:
                    await db.events.insert_one(dict(env))
                except Exception as exc:  # duplicate key race
                    if "duplicate key" in str(exc).lower():
                        incr("events.duplicate")
                        return {"status": "duplicate", "event_id": env["event_id"]}
                    raise

            incr("events.persisted")
            incr(f"events.type.{env['event_type']}")
            if PERSISTENCE_BACKEND != "postgres":
                await self._apply(db, _clean(env), simulation)
                await db.events.update_one({"event_id": env["event_id"]},
                                           {"$set": {"applied": True}})
            await manager.broadcast(_topic(env["event_type"]), _clean(env))
            await manager.broadcast("events", _clean(env))
            return {"status": "applied", "event_id": env["event_id"], "event": _clean(env)}

    async def _apply(self, db, env: dict, simulation: bool):
        for h in self._handlers.get(env["event_type"], []):
            try:
                await h(db, env)
            except Exception:
                logger.exception("projection handler failed: %s %s",
                                 env["event_type"], h.__name__)
                incr("projection.errors")
                await db.projection_errors.insert_one(
                    {"event_id": env["event_id"], "handler": h.__name__,
                     "event_type": env["event_type"], "at": utcnow_iso()})


def _clean(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    return d


def _topic(event_type: str) -> str:
    et = str(event_type)
    if et.startswith("MISSION"):
        return "missions"
    if et.startswith("RESCUE") or et.startswith("REQUEST"):
        return "requests"
    if et.startswith("INCIDENT"):
        return "incidents"
    if et.startswith("AI_RECOMMENDATION"):
        return "recommendations"
    if et.startswith("APPROVAL"):
        return "approvals"
    if et.startswith("GATEWAY") or et.startswith("SATELLITE") or et.startswith("MESSAGE"):
        return "communication"
    if et.startswith("SYNC"):
        return "sync"
    if et.startswith("SIMULATION"):
        return "simulation"
    if et.startswith("HAZARD"):
        return "hazards"
    return "system"


_cloud_pub_cache: dict[str, str] = {}


async def _cloud_public_key() -> str:
    if "key" not in _cloud_pub_cache:
        doc = await prod_db.devices.find_one({"device_id": CLOUD_DEVICE_ID}, {"_id": 0})
        _cloud_pub_cache["key"] = doc["public_key"] if doc else ""
    return _cloud_pub_cache["key"]


async def _verify_device_signature(env: dict):
    device = await prod_db.devices.find_one({"device_id": env["origin_device_id"]}, {"_id": 0})
    if not device:
        return False, "UNKNOWN_DEVICE"
    if device.get("revoked"):
        return False, "DEVICE_REVOKED"
    sig = env.get("signature")
    if not sig:
        return False, "MISSING_SIGNATURE"
    if not crypto.verify(device["public_key"], env, sig):
        return False, "SIGNATURE_INVALID"
    return True, "OK"


async def ensure_cloud_identity():
    """Provision the cloud node's signing identity (private key on filesystem only)."""
    existing = await prod_db.devices.find_one({"device_id": CLOUD_DEVICE_ID})
    if existing and edge_keystore.has_key(CLOUD_DEVICE_ID):
        _cloud_pub_cache["key"] = existing["public_key"]
        return
    pub = edge_keystore.provision(CLOUD_DEVICE_ID)
    await prod_db.devices.update_one(
        {"device_id": CLOUD_DEVICE_ID},
        {"$set": {"device_id": CLOUD_DEVICE_ID, "public_key": pub,
                  "device_type": "cloud_service", "trust_level": "trusted",
                  "signing_mode": "server_keystore", "revoked": False,
                  "owner_user_id": None, "registered_at": utcnow_iso()}},
        upsert=True)
    _cloud_pub_cache["key"] = pub


bus = EventBus()
