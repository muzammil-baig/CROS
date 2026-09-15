"""Persistence boundary with a PostgreSQL-backed document compatibility layer."""
from datetime import datetime, timezone
from uuid import uuid4

from .config import PERSISTENCE_BACKEND


class _Result:
    def __init__(self, *, inserted_id=None, matched_count=0, modified_count=0, deleted_count=0):
        self.inserted_id = inserted_id
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.deleted_count = deleted_count


class _Cursor:
    def __init__(self, collection, query, projection=None):
        self.collection, self.query, self.projection = collection, query, projection
        self._sort = None
        self._limit = None

    def sort(self, field, direction=-1):
        self._sort = (field, direction)
        return self

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        return await self.collection._find_many(self.query, self.projection, self._sort, self._limit or length)


class _PostgresCollection:
    def __init__(self, collection):
        self.collection = collection
        self.name = collection

    async def _find_many(self, query, projection=None, sort=None, limit=None):
        from . import postgres
        rows = await postgres.list_entities(self.collection, query=query, limit=limit or 1000, sort=sort)
        if not projection:
            return rows
        return [{k: v for k, v in row.items() if projection.get(k, 1) != 0} for row in rows]

    def find(self, query=None, projection=None):
        return _Cursor(self, query or {}, projection)

    async def find_one(self, query=None, projection=None):
        rows = await self._find_many(query or {}, projection, limit=1)
        return rows[0] if rows else None

    async def insert_one(self, document):
        from . import postgres
        doc = dict(document)
        doc.setdefault("_id", str(uuid4()))
        doc.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        await postgres.upsert_entity(self.collection, str(doc.get("_id") or doc.get("id")), doc)
        return _Result(inserted_id=doc["_id"])

    async def update_one(self, query, update, upsert=False):
        from . import postgres
        current = await self.find_one(query)
        if current is None and not upsert:
            return _Result()
        doc = current or dict(query)
        if "$setOnInsert" in update and current is None:
            doc.update(update["$setOnInsert"])
        if "$set" in update:
            doc.update(update["$set"])
        if "$inc" in update:
            for key, amount in update["$inc"].items():
                doc[key] = doc.get(key, 0) + amount
        if "$push" in update:
            for key, value in update["$push"].items():
                doc.setdefault(key, []).append(value)
        if "$addToSet" in update:
            for key, value in update["$addToSet"].items():
                values = value.get("$each", []) if isinstance(value, dict) and "$each" in value else [value]
                target = doc.setdefault(key, [])
                for item in values:
                    if item not in target:
                        target.append(item)
        doc.setdefault("_id", str(uuid4()))
        entity_id = str(doc.get("_id") or doc.get("id"))
        await postgres.upsert_entity(self.collection, entity_id, doc)
        return _Result(inserted_id=entity_id if current is None else None, matched_count=0 if current is None else 1, modified_count=1)

    async def delete_one(self, query):
        from . import postgres
        current = await self.find_one(query)
        if not current:
            return _Result()
        await postgres.delete_entity(self.collection, str(current.get("_id") or current.get("id")))
        return _Result(deleted_count=1)

    async def delete_many(self, query=None):
        from . import postgres
        rows = await self._find_many(query or {}, limit=100000)
        for row in rows:
            await postgres.delete_entity(self.collection, str(row.get("_id") or row.get("id")))
        return _Result(deleted_count=len(rows))

    async def count_documents(self, query=None):
        from . import postgres
        return await postgres.count_entities(self.collection, query or {})


class _PostgresHandle:
    name = "postgresql"

    def __getattr__(self, name):
        return _PostgresCollection(name)


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
