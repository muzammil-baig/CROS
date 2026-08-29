from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from ..auth import require
from ..constants import EventType, Priority
from ..db import db
from ..errors import ApiError, not_found
from ..events import bus
from ..models import GeoPoint
from ..services import operations, prioritization
from ..ulid import is_ulid, new_ulid
from .deps import emit_client_event, publish_offline_envelope, scope_request

router = APIRouter(tags=["emergency-requests"])

CATEGORIES = ["rescue", "medical", "person_trapped", "hazard", "evacuation",
              "welfare_check", "safe_report"]


class CreateRequestBody(BaseModel):
    event_id: Optional[str] = None
    request_id: Optional[str] = None
    category: str
    description: str = Field(default="", max_length=2000)
    location: GeoPoint
    people_count: int = Field(default=1, ge=0, le=10000)
    vulnerabilities: list[str] = Field(default_factory=list)
    water_rising: bool = False
    reporter_name: Optional[str] = None
    reporter_phone: Optional[str] = None
    incident_id: Optional[str] = None
    created_offline: bool = False
    offline_envelope: Optional[dict] = None


class OverridePriorityBody(BaseModel):
    tier: str
    reason: str = Field(min_length=3, max_length=500)


class CancelBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/requests", status_code=201)
async def create_request(body: CreateRequestBody,
                         idempotency_key: Optional[str] = Header(None,
                                                                 alias="Idempotency-Key"),
                         user: dict = Depends(require("request:create"))):
    if body.category not in CATEGORIES:
        raise ApiError(422, "VALIDATION_FAILED", "Unknown category",
                       [{"field": "category", "issue": f"must be one of {CATEGORIES}"}])
    if body.offline_envelope:
        # Envelope was created on the device while disconnected: identity preserved.
        result = await publish_offline_envelope(body.offline_envelope)
        request_id = (body.offline_envelope.get("payload") or {}).get("request_id")
        if result["status"] == "rejected":
            raise ApiError(422, "EVENT_REJECTED", str(result.get("reason")))
        if result["status"] == "applied":
            await operations.run_pipeline(db, request_id, actor_id=user["user_id"])
        doc = await db.emergency_requests.find_one({"request_id": request_id}, {"_id": 0})
        return {"request_id": request_id, "event_status": result["status"],
                "event_id": result.get("event_id"),
                "request": scope_request(doc, user) if doc else None}

    request_id = body.request_id or f"REQ-{new_ulid()}"
    event_id = body.event_id or idempotency_key or new_ulid()
    if not is_ulid(event_id):
        event_id = new_ulid()
    priority = Priority.CRITICAL.value if body.category in (
        "rescue", "medical", "person_trapped") else Priority.NORMAL.value
    result = await emit_client_event(
        event_type=EventType.RESCUE_REQUEST_CREATED.value,
        payload={"request_id": request_id, "incident_id": body.incident_id,
                 "location": body.location.model_dump(), "category": body.category,
                 "description": body.description, "people_count": body.people_count,
                 "vulnerabilities": body.vulnerabilities,
                 "water_rising": body.water_rising,
                 "source_type": "citizen" if user["role"] == "citizen" else user["role"],
                 "reporter_name": body.reporter_name or user.get("name"),
                 "reporter_phone": body.reporter_phone},
        user=user, priority=priority, event_id=event_id)
    if result["status"] == "duplicate":
        prior = await db.events.find_one({"event_id": event_id}, {"_id": 0, "payload": 1})
        prior_request_id = ((prior or {}).get("payload") or {}).get("request_id") or request_id
        existing = await db.emergency_requests.find_one(
            {"request_id": prior_request_id}, {"_id": 0})
        return {"request_id": prior_request_id,
                "event_status": "duplicate", "event_id": event_id,
                "request": scope_request(existing, user) if existing else None}
    pipeline = await operations.run_pipeline(db, request_id, actor_id=user["user_id"],
                                            correlation_id=result["event"]["correlation_id"])
    doc = await db.emergency_requests.find_one({"request_id": request_id}, {"_id": 0})
    return {"request_id": request_id, "event_status": result["status"],
            "event_id": result["event_id"], "request": scope_request(doc, user),
            "pipeline": pipeline}


@router.get("/requests")
async def list_requests(status: Optional[str] = None, incident_id: Optional[str] = None,
                        tier: Optional[str] = None, limit: int = Query(200, le=500),
                        user: dict = Depends(require("request:read"))):
    q = {}
    if status:
        q["status"] = status
    if incident_id:
        q["incident_id"] = incident_id
    if tier:
        q["priority.tier"] = tier
    docs = await db.emergency_requests.find(q, {"_id": 0}).sort(
        [("priority.score", -1), ("created_at", -1)]).to_list(limit)
    return {"items": [scope_request(d, user) for d in docs], "count": len(docs)}


@router.get("/requests/mine")
async def my_requests(user: dict = Depends(require("request:create"))):
    docs = await db.emergency_requests.find(
        {"reporter_user_id": user["user_id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"items": docs, "count": len(docs)}


async def _get(request_id: str) -> dict:
    doc = await db.emergency_requests.find_one({"request_id": request_id}, {"_id": 0})
    if not doc:
        raise not_found("Emergency request not found")
    return doc


@router.get("/requests/{request_id}")
async def get_request(request_id: str, user: dict = Depends(require("request:read"))):
    return scope_request(await _get(request_id), user)


@router.get("/requests/{request_id}/status")
async def request_status(request_id: str, user: dict = Depends(require("request:read"))):
    d = await _get(request_id)
    return {"request_id": request_id, "status": d.get("status"),
            "mission_id": d.get("mission_id"), "sync_state": d.get("sync_state"),
            "updated_at": d.get("updated_at")}


@router.get("/requests/{request_id}/priority")
async def request_priority(request_id: str, user: dict = Depends(require("request:read"))):
    d = await _get(request_id)
    return {"request_id": request_id, "priority": d.get("priority"),
            "algorithm_weights": prioritization.CATEGORY_WEIGHT}


@router.post("/requests/{request_id}/priority/override")
async def override_priority(request_id: str, body: OverridePriorityBody,
                            user: dict = Depends(require("request:prioritize_override"))):
    d = await _get(request_id)
    if body.tier not in ("critical", "high", "normal", "low"):
        raise ApiError(422, "VALIDATION_FAILED", "Invalid tier")
    pr = dict(d.get("priority") or {})
    pr.update({"tier": body.tier, "overridden_by": user["user_id"],
               "override_reason": body.reason, "algorithm": "human_override"})
    result = await emit_client_event(
        event_type=EventType.REQUEST_PRIORITIZED.value,
        payload={"request_id": request_id, "priority": pr, "status": d.get("status")},
        user=user, priority=body.tier)
    return {"request_id": request_id, "priority": pr, "event_id": result.get("event_id")}


@router.get("/requests/{request_id}/verification")
async def request_verification(request_id: str,
                               user: dict = Depends(require("request:read"))):
    d = await _get(request_id)
    reports = await db.reports.find({"request_id": request_id}, {"_id": 0}).to_list(100)
    return {"request_id": request_id, "verification": d.get("verification"),
            "contributing_reports": reports}


@router.get("/requests/{request_id}/history")
async def request_history(request_id: str, user: dict = Depends(require("request:read"))):
    await _get(request_id)
    events = await db.events.find(
        {"payload.request_id": request_id}, {"_id": 0}).sort("logical_timestamp", 1).to_list(300)
    return {"request_id": request_id, "events": events, "count": len(events)}


@router.get("/requests/{request_id}/duplicates")
async def request_duplicates(request_id: str, user: dict = Depends(require("request:read"))):
    d = await _get(request_id)
    merged = await db.emergency_requests.find(
        {"duplicate_of": request_id}, {"_id": 0}).to_list(50)
    return {"request_id": request_id, "duplicate_of": d.get("duplicate_of"),
            "merged_requests": [scope_request(m, user) for m in merged],
            "note": "Contributing reports are retained; deduplication never deletes provenance"}


@router.post("/requests/{request_id}/cancel")
async def cancel_request(request_id: str, body: CancelBody,
                         user: dict = Depends(require("request:cancel"))):
    d = await _get(request_id)
    if user["role"] == "citizen" and d.get("reporter_user_id") != user["user_id"]:
        raise not_found("Emergency request not found")
    result = await emit_client_event(
        event_type=EventType.REQUEST_CANCELLED.value,
        payload={"request_id": request_id, "reason": body.reason},
        user=user, priority="normal")
    return {"request_id": request_id, "status": "cancelled",
            "event_id": result.get("event_id")}
