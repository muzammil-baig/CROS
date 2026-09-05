"""Persistence boundary.

Mongo is retained as an explicit rollback adapter. PostgreSQL mode must not import
Motor or touch Mongo configuration during application startup.
"""
from .config import PERSISTENCE_BACKEND


class _PostgresHandle:
    name = "postgresql"

    def __getattr__(self, name):
        raise RuntimeError(
            f"Mongo collection '{name}' is not available in PostgreSQL mode; "
            "migrate this repository call to cros.postgres"
        )


if PERSISTENCE_BACKEND == "postgres":
    client = None
    db = _PostgresHandle()
    sim_db = _PostgresHandle()
else:
    from motor.motor_asyncio import AsyncIOMotorClient
    from .config import MONGO_URL, DB_NAME, SIM_DB_NAME

    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    sim_db = client[SIM_DB_NAME]


def get_db(simulation: bool = False):
    return sim_db if simulation else db


async def ensure_indexes():
    if PERSISTENCE_BACKEND == "postgres":
        from .postgres import check_connection
        if not await check_connection():
            raise RuntimeError("PostgreSQL connectivity check failed")
        return
    for d in (db, sim_db):
        await d.events.create_index("event_id", unique=True)
        await d.events.create_index([("event_type", 1), ("wall_clock_timestamp", -1)])
        await d.events.create_index("correlation_id")
        await d.events.create_index("logical_timestamp")
        await d.emergency_requests.create_index("request_id", unique=True)
        await d.emergency_requests.create_index("location_2dsphere_placeholder", sparse=True)
        await d.emergency_requests.create_index([("geo", "2dsphere")])
        await d.incidents.create_index("incident_id", unique=True)
        await d.missions.create_index("mission_id", unique=True)
        await d.hazards.create_index("hazard_id", unique=True)
        await d.resources.create_index("resource_id", unique=True)
        await d.facilities.create_index("facility_id", unique=True)
        await d.gateways.create_index("gateway_id", unique=True)
        await d.recommendations.create_index("recommendation_id", unique=True)
        await d.approvals.create_index("approval_id", unique=True)
        await d.approvals.create_index("recommendation_id")
        await d.audit_log.create_index([("created_at", -1)])
        await d.capacity_ops.create_index("operation_id", unique=True)
        await d.comm_messages.create_index("message_id", unique=True)
        await d.reports.create_index("report_id", unique=True)
    await db.users.create_index("email", unique=True)
    await db.users.create_index("user_id", unique=True)
    await db.devices.create_index("device_id", unique=True)
    await db.organizations.create_index("org_id", unique=True)
    await db.login_attempts.create_index("identifier")
    await db.replay_nonces.create_index("expires_at", expireAfterSeconds=0)
