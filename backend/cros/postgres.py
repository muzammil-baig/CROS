"""Async PostgreSQL access for the CROS migration boundary.

This module is intentionally independent from the legacy Mongo adapter. It provides
small, explicit SQL primitives for the canonical event and simulation schemas while
existing services migrate incrementally.
"""
from __future__ import annotations

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .config import POSTGRES_URL_NON_POOLING


_pool: asyncpg.Pool | None = None


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
            envelope.get("origin_device_id"), envelope.get("origin_actor_id"),
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


async def healthcheck() -> dict[str, str]:
    try:
        await check_connection()
        return {"status": "up", "backend": "postgresql"}
    except Exception as exc:
        return {"status": "down", "backend": "postgresql", "error": str(exc)[:200]}


async def close() -> None:
    await close_pool()


__all__ = ["check_connection", "close", "connection", "healthcheck", "insert_event", "open_pool"]
