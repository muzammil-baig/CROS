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
            else _uuid_or_none(envelope.get("origin_device_id")),
            _uuid_or_none(envelope.get("origin_actor_id")),
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
        row = await conn.fetchrow(
            """
            insert into simulation.simulation_run
              (simulation_id, scenario, params, started_by, status, started_at, isolation)
            values ($1, $2, $3::jsonb, $4, 'running', now(), $5::jsonb)
            returning id, simulation_id, scenario, params, started_by, status, started_at, completed_at,
                      metrics, error, steps, isolation
            """,
            uuid4(), scenario, json.dumps(params), _uuid_or_none(started_by), json.dumps(isolation),
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
    if status in {"completed", "aborted", "failed"}:
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
    "create_simulation_run", "get_simulation_run", "healthcheck", "insert_event",
    "list_simulation_events", "list_simulation_runs", "open_pool",
    "simulation_event_exists", "update_simulation_run",
]
