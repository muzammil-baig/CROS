"""Async PostgreSQL access for the CROS migration boundary.

This module is intentionally independent from the legacy Mongo adapter. It provides
small, explicit SQL primitives for the canonical event and simulation schemas while
existing services migrate incrementally.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5, uuid4

import asyncpg

from .config import POSTGRES_URL_NON_POOLING


_pool: asyncpg.Pool | None = None


def _uuid_or_none(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return uuid5(NAMESPACE_URL, f"cros:{value}")


async def open_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not POSTGRES_URL_NON_POOLING:
            raise RuntimeError("POSTGRES_URL is required for PostgreSQL persistence")
        _pool = await asyncpg.create_pool(POSTGRES_URL_NON_POOLING, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    pool = await open_pool()
    async with pool.acquire() as conn:
        yield conn


async def check_connection() -> bool:
    async with connection() as conn:
        return await conn.fetchval("select 1") == 1


async def insert_event(envelope: dict[str, Any]) -> bool:
    """Insert an event once; return False when event_id already exists."""
    async with connection() as conn:
        actor_id = _uuid_or_none(envelope.get("origin_actor_id"))
        if actor_id and not await conn.fetchval("select exists(select 1 from core.app_user where id = $1)", actor_id):
            actor_id = None
        result = await conn.execute(
            """
            insert into events.event_log
              (event_id, event_type, schema_version, aggregate_type, aggregate_id,
               correlation_id, causal_parent_ids, hlc_timestamp, wall_clock_timestamp,
               origin_device_id, origin_actor_id, payload, signature, received_at)
            values ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12::jsonb, $13, $14)
            on conflict (event_id) do nothing
            """,
            envelope["event_id"], envelope["event_type"], envelope.get("schema_version", 1),
            envelope.get("aggregate_type"), envelope.get("aggregate_id"),
            envelope.get("correlation_id"),
            __import__("json").dumps(envelope.get("causal_parent_ids", [])),
            envelope["logical_timestamp"], envelope["wall_clock_timestamp"],
            None if envelope.get("origin_device_id") == "cloud-service-node"
            else _device_uuid(envelope.get("origin_device_id")) if envelope.get("origin_device_id") else None,
            actor_id,
            __import__("json").dumps(envelope["payload"]), envelope.get("signature"),
            envelope.get("received_at") or datetime.now(timezone.utc),
        )
    return result.endswith("1")


async def simulation_event_exists(event_id: str) -> bool:
    async with connection() as conn:
        return await conn.fetchval(
            "select exists(select 1 from simulation.simulation_event where event_id = $1)",
            event_id,
        )


async def create_simulation_run(*, scenario: str, params: dict[str, Any], started_by: str | None,
                                description: str, isolation: dict[str, Any]) -> dict[str, Any]:
    import json
    async with connection() as conn:
        simulation_id = await conn.fetchval(
            "insert into simulation.simulation (name, description) values ($1, $2) returning id",
            scenario, description,
        )
        row = await conn.fetchrow(
            """
            insert into simulation.simulation_run
              (simulation_id, scenario, params, started_by, status, started_at, isolation)
            values ($1, $2, $3::jsonb, $4, 'running', now(), $5::jsonb)
            returning id, simulation_id, scenario, params, started_by, status, started_at, completed_at,
                      metrics, error, steps, isolation
            """,
            simulation_id, scenario, json.dumps(params), _uuid_or_none(started_by), json.dumps(isolation),
        )
    return dict(row)


async def get_simulation_run(run_id: str) -> dict[str, Any] | None:
    async with connection() as conn:
        row = await conn.fetchrow(
            "select id, scenario, params, started_by, status, started_at, completed_at, metrics, error, steps, isolation from simulation.simulation_run where id = $1",
            UUID(run_id),
        )
    return dict(row) if row else None


async def list_simulation_runs(limit: int = 50) -> list[dict[str, Any]]:
    async with connection() as conn:
        rows = await conn.fetch(
            "select id, scenario, params, started_by, status, started_at, completed_at, metrics, error, isolation from simulation.simulation_run order by started_at desc limit $1",
            limit,
        )
    return [dict(row) for row in rows]


async def update_simulation_run(run_id: str, *, status: str | None = None,
                                metrics: dict[str, Any] | None = None,
                                error: str | None = None,
                                steps: list[dict[str, Any]] | None = None) -> None:
    import json
    fields, values = [], []
    if status is not None:
        fields.append(f"status = ${len(values) + 1}"); values.append(status)
    if metrics is not None:
        fields.append(f"metrics = ${len(values) + 1}::jsonb"); values.append(json.dumps(metrics))
    if error is not None:
        fields.append(f"error = ${len(values) + 1}"); values.append(error[:500])
    if steps is not None:
        fields.append(f"steps = ${len(values) + 1}::jsonb"); values.append(json.dumps(steps))
    if status in {"completed", "cancelled", "failed"}:
        fields.append("completed_at = now()")
    if not fields:
        return
    values.append(UUID(run_id))
    async with connection() as conn:
        await conn.execute(f"update simulation.simulation_run set {', '.join(fields)} where id = ${len(values)}", *values)


async def append_simulation_event(*, run_id: str, event_id: str, event_type: str,
                                  payload: dict[str, Any], hlc_timestamp: str) -> bool:
    import json
    async with connection() as conn:
        result = await conn.execute(
            "insert into simulation.simulation_event (run_id, event_id, event_type, payload, hlc_timestamp) values ($1, $2, $3, $4::jsonb, $5) on conflict (event_id) do nothing",
            UUID(run_id), event_id, event_type, json.dumps(payload), hlc_timestamp,
        )
    return result.endswith("1")


async def list_simulation_events(run_id: str, limit: int = 200) -> list[dict[str, Any]]:
    async with connection() as conn:
        rows = await conn.fetch(
            "select id, run_id, event_id, event_type, payload, hlc_timestamp, created_at from simulation.simulation_event where run_id = $1 order by created_at desc limit $2",
            UUID(run_id), limit,
        )
    return [dict(row) for row in rows]


def _nested_value(document: Any, path: str) -> tuple[bool, Any]:
    if not isinstance(path, str):
        return False, None
    parts = path.split(".", 1)
    if isinstance(document, list):
        values = [_nested_value(item, path)[1] for item in document
                  if _nested_value(item, path)[0]]
        return bool(values), values
    if not isinstance(document, dict) or parts[0] not in document:
        return False, None
    if len(parts) == 1:
        return True, document[parts[0]]
    return _nested_value(document[parts[0]], parts[1])


def _matches_value(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list) and not isinstance(expected, list):
        return expected in actual
    return actual == expected


async def _entity_matches(payload: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if not isinstance(key, str) or key.startswith("$"):
            continue
        exists, actual = _nested_value(payload, key)
        if isinstance(expected, dict):
            if "$in" in expected and not any(_matches_value(actual, item) for item in expected["$in"]):
                return False
            if "$nin" in expected and any(_matches_value(actual, item) for item in expected["$nin"]):
                return False
            if "$ne" in expected and _matches_value(actual, expected["$ne"]):
                return False
            if "$exists" in expected and exists != expected["$exists"]:
                return False
        elif not exists or not _matches_value(actual, expected):
            return False
    return True


async def list_entities(collection: str, *, query: dict[str, Any], limit: int = 1000,
                        sort: tuple[str, int] | None = None) -> list[dict[str, Any]]:
    async with connection() as conn:
        if collection == "events":
            rows = await conn.fetch(
                "select event_id, event_type, schema_version, aggregate_type, aggregate_id, correlation_id, causal_parent_ids, hlc_timestamp, wall_clock_timestamp, origin_device_id, origin_actor_id, payload, signature, received_at from events.event_log order by received_at desc limit $1",
                max(limit, 100000) if query else limit,
            )
            import json
            result = []
            for row in rows:
                item = dict(row)
                for key in ("causal_parent_ids", "payload"):
                    if isinstance(item.get(key), str):
                        item[key] = json.loads(item[key])
                item["_id"] = item["event_id"]
                item["logical_timestamp"] = item.get("hlc_timestamp")
                item["priority"] = (item.get("payload") or {}).get("priority", "normal")
                item["applied"] = True
                if await _entity_matches(item, query):
                    result.append(item)
            return result[:limit]
        rows = await conn.fetch(
            "select entity_id, payload, created_at, updated_at from operational.entity where collection = $1 order by updated_at desc limit $2",
            collection, max(limit, 100000) if query else limit,
        )
    import json
    result = []
    for row in rows:
        raw_payload = row["payload"] or {}
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload)
        payload.setdefault("_id", row["entity_id"])
        if await _entity_matches(payload, query):
            result.append(payload)
    if sort:
        sort_fields = sort if isinstance(sort, list) else [sort]
        for field, direction in reversed(sort_fields):
            result.sort(key=lambda item: _nested_value(item, field)[1] or 0
                        if isinstance(_nested_value(item, field)[1], (int, float))
                        else str(_nested_value(item, field)[1] or ""),
                        reverse=direction < 0)
    return result[:limit]


async def upsert_entity(collection: str, entity_id: str, payload: dict[str, Any]) -> None:
    import json
    async with connection() as conn:
        await conn.execute(
            "insert into operational.entity (collection, entity_id, payload) values ($1, $2, $3::jsonb) on conflict (collection, entity_id) do update set payload = excluded.payload, updated_at = now()",
            collection, entity_id, json.dumps(payload, default=str),
        )


async def delete_entity(collection: str, entity_id: str) -> None:
    async with connection() as conn:
        await conn.execute("delete from operational.entity where collection = $1 and entity_id = $2", collection, entity_id)


async def count_entities(collection: str, query: dict[str, Any]) -> int:
    rows = await list_entities(collection, query=query, limit=100000)
    return len(rows)


def _user_document(row: dict[str, Any]) -> dict[str, Any]:
    import json
    contact = row.get("contact_info") or {}
    if isinstance(contact, str):
        contact = json.loads(contact)
    return {
        "user_id": str(row["id"]),
        "email": contact.get("email", ""),
        "name": row.get("display_name"),
        "role": contact.get("role", "citizen"),
        "org_id": str(row["organization_id"]) if row.get("organization_id") else None,
        "password_hash": row.get("credential_hash"),
        "disabled": not row.get("is_active", True),
    }


async def find_user_by_id(user_id: str) -> dict[str, Any] | None:
    async with connection() as conn:
        row = await conn.fetchrow(
            "select id, organization_id, display_name, contact_info, is_active, credential_hash from core.app_user where id = $1",
            UUID(user_id),
        )
    return _user_document(dict(row)) if row else None


async def upsert_organization(*, org_id: str, name: str, org_type: str) -> None:
    async with connection() as conn:
        existing_id = await conn.fetchval("select id from core.organization where name = $1", name)
        await conn.execute(
            "insert into core.organization (id, name, org_type, is_active, created_at) values ($1, $2, $3, true, now()) on conflict (id) do update set name = excluded.name, org_type = excluded.org_type, is_active = true",
            existing_id or uuid5(NAMESPACE_URL, f"cros-org:{org_id}"), name, org_type,
        )


async def upsert_user(*, user_id: str, email: str, name: str, role: str,
                      org_id: str | None, password_hash: str) -> None:
    import json
    async with connection() as conn:
        organization_id = None
        if org_id:
            organization_id = await conn.fetchval("select id from core.organization where name = $1", {
                "ORG-NDRA": "National Disaster Response Agency",
                "ORG-CITYFIRE": "Metro City Fire & Rescue",
                "ORG-MEDNET": "Regional Health Network",
            }.get(org_id, org_id))
        await conn.execute(
            """
            insert into core.app_user (id, organization_id, display_name, contact_info,
                                       is_active, credential_hash, created_at, updated_at)
            values ($1, $2, $3, $4::jsonb, true, $5, now(), now())
            on conflict (id) do update set organization_id = excluded.organization_id,
              display_name = excluded.display_name, contact_info = excluded.contact_info,
              is_active = true, credential_hash = excluded.credential_hash, updated_at = now()
            """,
            _uuid_or_none(user_id) or uuid5(NAMESPACE_URL, f"cros-user:{email}"),
            organization_id, name, json.dumps({"email": email, "role": role}), password_hash,
        )


async def find_user_by_email(email: str) -> dict[str, Any] | None:
    async with connection() as conn:
        row = await conn.fetchrow(
            "select id, organization_id, display_name, contact_info, is_active, credential_hash from core.app_user where lower(contact_info->>'email') = lower($1) limit 1",
            email,
        )
    return _user_document(dict(row)) if row else None


def _device_uuid(device_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"cros-device:{device_id}")


async def find_device(device_id: str) -> dict[str, Any] | None:
    async with connection() as conn:
        row = await conn.fetchrow(
            "select id, owner_user_id, public_key, device_class, trust_score, revoked_at from core.device where id = $1",
            _device_uuid(device_id),
        )
    if not row:
        return None
    result = dict(row)
    result["device_id"] = device_id
    result["signing_mode"] = "server_keystore"
    result["trust_level"] = "trusted" if float(result.get("trust_score") or 0) >= 0.8 else "provisional"
    result["revoked"] = result.get("revoked_at") is not None
    return result


async def revoke_device(device_id: str, reason: str | None) -> None:
    async with connection() as conn:
        await conn.execute(
            "update core.device set revoked_at = now(), revocation_reason = $2, updated_at = now() where id = $1",
            _device_uuid(device_id), reason,
        )


async def upsert_device(*, device_id: str, owner_user_id: str | None, public_key: str,
                        signing_mode: str, trust_level: str) -> None:
    async with connection() as conn:
        owner_uuid = _uuid_or_none(owner_user_id)
        if owner_uuid and not await conn.fetchval("select exists(select 1 from core.app_user where id = $1)", owner_uuid):
            owner_uuid = None
        await conn.execute(
            """
            insert into core.device (id, owner_user_id, public_key, device_class, provisioned_at,
                                     trust_score, created_at, updated_at)
            values ($1, $2, $3, $4, now(), $5, now(), now())
            on conflict (id) do update set owner_user_id = excluded.owner_user_id,
              public_key = excluded.public_key, device_class = excluded.device_class,
              trust_score = excluded.trust_score, updated_at = now()
            """,
            _device_uuid(device_id), owner_uuid,
            public_key, "command_center", 1.0 if trust_level == "trusted" else 0.5,
        )


async def healthcheck() -> dict[str, str]:
    try:
        await check_connection()
        return {"status": "up", "backend": "postgresql"}
    except Exception as exc:
        return {"status": "down", "backend": "postgresql", "error": str(exc)[:200]}


async def close() -> None:
    await close_pool()


__all__ = [
    "append_simulation_event", "check_connection", "close", "connection",
    "count_entities", "create_simulation_run", "delete_entity", "find_user_by_id",
    "find_user_by_email", "get_simulation_run", "healthcheck", "insert_event",
    "list_entities", "list_simulation_events", "list_simulation_runs", "open_pool",
    "simulation_event_exists", "update_simulation_run", "upsert_entity",
]
