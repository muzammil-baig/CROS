"""PostgreSQL-backed simulation storage with schema-qualified queries."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from .postgres import connection


async def create_simulation(name: str, description: str | None = None) -> UUID:
    async with connection() as conn:
        return await conn.fetchval(
            "insert into simulation.simulation (name, description) values ($1, $2) returning id",
            name,
            description,
        )


async def start_run(simulation_id: UUID) -> UUID:
    async with connection() as conn:
        return await conn.fetchval(
            """
            insert into simulation.simulation_run (simulation_id, status, started_at)
            values ($1, 'running', now())
            returning id
            """,
            simulation_id,
        )


async def append_event(
    run_id: UUID,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    hlc_timestamp: str,
) -> bool:
    async with connection() as conn:
        result = await conn.execute(
            """
            insert into simulation.simulation_event
              (run_id, event_id, event_type, payload, hlc_timestamp)
            values ($1, $2, $3, $4::jsonb, $5)
            on conflict (event_id) do nothing
            """,
            run_id,
            event_id,
            event_type,
            __import__("json").dumps(payload),
            hlc_timestamp,
        )
    return result.endswith("1")


async def finish_run(run_id: UUID, status: str) -> None:
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("invalid terminal simulation status")
    async with connection() as conn:
        await conn.execute(
            "update simulation.simulation_run set status = $2, completed_at = now() where id = $1",
            run_id,
            status,
        )


__all__ = ["append_event", "create_simulation", "finish_run", "start_run"]
