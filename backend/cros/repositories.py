"""Explicit SQL repositories for the PostgreSQL migration boundary."""
from __future__ import annotations

from __future__ import annotations

from typing import Any
from uuid import UUID

from .postgres import connection


async def create_user(
    display_name: str,
    organization_id: UUID | None = None,
    contact_info: dict[str, Any] | None = None,
) -> UUID:
    async with connection() as conn:
        return await conn.fetchval(
            """
            insert into core.app_user (display_name, organization_id, contact_info)
            values ($1, $2, $3::jsonb)
            returning id
            """,
            display_name,
            organization_id,
            __import__("json").dumps(contact_info) if contact_info is not None else None,
        )


async def get_user(user_id: UUID) -> dict[str, Any] | None:
    async with connection() as conn:
        row = await conn.fetchrow(
            """
            select id, organization_id, display_name, contact_info, is_active,
                   created_at, updated_at
            from core.app_user
            where id = $1
            """,
            user_id,
        )
    return dict(row) if row else None


async def register_device(
    public_key: str,
    device_class: str,
    owner_user_id: UUID | None = None,
) -> UUID:
    async with connection() as conn:
        return await conn.fetchval(
            """
            insert into core.device (public_key, device_class, owner_user_id)
            values ($1, $2, $3)
            returning id
            """,
            public_key,
            device_class,
            owner_user_id,
        )


async def revoke_device(device_id: UUID, reason: str) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            update core.device
            set revoked_at = now(), revocation_reason = $2, updated_at = now()
            where id = $1 and revoked_at is null
            """,
            device_id,
            reason,
        )


__all__ = ["create_user", "get_user", "register_device", "revoke_device"]
