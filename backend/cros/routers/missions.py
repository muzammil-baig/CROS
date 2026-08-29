from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from ..auth import require
from ..constants import MISSION_TRANSITIONS, MissionStatus
from ..db import db
from ..errors import ApiError, not_found
from ..services import operations
from ..services.routing import compute_route
from .deps import scope_request

router = APIRouter(tags=["missions"])


class ReasonBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)
    event_id: Optional[str] = None


class StatusBody(BaseModel):
    """Explicit responder progress command. Only responder-owned stages allowed."""
    status: str
    reason: Optional[str] = None
    outcome: Optional[dict] = None
    event_id: Optional[str] = None


RESPONDER_STAGES = {"en_route": MissionStatus.EN_ROUTE, "on_scene": MissionStatus.ON_SCENE,
                    "completed": MissionStatus.COMPLETED, "failed": MissionStatus.FAILED}


async def _mission(mission_id: str) -> dict:
    doc = await db.missions.find_one({"mission_id": mission_id}, {"_id": 0})
    if not doc:
        raise not_found("Mission not found")
    return doc


@router.get("/missions")
async def list_missions(status: Optional[str] = None, incident_id: Optional[str] = None,
                        limit: int = Query(200, le=500),
                        user: dict = Depends(require("mission:read"))):
    q = {}
    if status:
        q["status"] = status
    if incident_id:
        q["incident_id"] = incident_id
    if user["role"] == "field_responder":
        q["responder_user_id"] = user["user_id"]
    docs = await db.missions.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.get("/missions/{mission_id}")
async def get_mission(mission_id: str, user: dict = Depends(require("mission:read"))):
    m = await _mission(mission_id)
    if user["role"] == "field_responder" and m.get("responder_user_id") != user["user_id"]:
        raise not_found("Mission not found")
    m["allowed_transitions"] = [s.value for s in
                                MISSION_TRANSITIONS[MissionStatus(m["status"])]]
    if m.get("emergency_request_id"):
        req = await db.emergency_requests.find_one(
            {"request_id": m["emergency_request_id"]}, {"_id": 0})
        m["request"] = scope_request(req, user) if req else None
    return m


@router.post("/missions/{mission_id}/approve")
async def approve_mission(mission_id: str, body: ReasonBody,
                          user: dict = Depends(require("mission:approve"))):
    m = await _mission(mission_id)
    if m.get("recommendation_id"):
        approval = await db.approvals.find_one(
            {"recommendation_id": m["recommendation_id"]}, {"_id": 0})
        if not approval or approval["decision"] != "approved":
            raise ApiError(409, "APPROVAL_REQUIRED",
                           "This mission originates from an AI recommendation and must be "
                           "approved through /recommendations/{id}/approve")
    return await operations.transition_mission(
        db, mission_id=mission_id, target=MissionStatus.APPROVED, actor=user,
        reason=body.reason, event_id=body.event_id)


@router.post("/missions/{mission_id}/reject")
async def reject_mission(mission_id: str, body: ReasonBody,
                         user: dict = Depends(require("mission:approve"))):
    return await operations.transition_mission(
        db, mission_id=mission_id, target=MissionStatus.REJECTED, actor=user,
        reason=body.reason, event_id=body.event_id)


@router.post("/missions/{mission_id}/dispatch")
async def dispatch_mission(mission_id: str, body: ReasonBody,
                           user: dict = Depends(require("mission:dispatch"))):
    return await operations.transition_mission(
        db, mission_id=mission_id, target=MissionStatus.DISPATCHED, actor=user,
        reason=body.reason, event_id=body.event_id)


@router.post("/missions/{mission_id}/accept")
async def accept_mission(mission_id: str, body: ReasonBody,
                         user: dict = Depends(require("mission:respond"))):
    return await operations.transition_mission(
        db, mission_id=mission_id, target=MissionStatus.ACCEPTED_BY_RESPONDER, actor=user,
        reason=body.reason, event_id=body.event_id)


@router.post("/missions/{mission_id}/decline")
async def decline_mission(mission_id: str, body: ReasonBody,
                          user: dict = Depends(require("mission:respond"))):
    if not body.reason:
        raise ApiError(422, "VALIDATION_FAILED", "A reason is required to decline a mission")
    return await operations.transition_mission(
        db, mission_id=mission_id, target=MissionStatus.DECLINED, actor=user,
        reason=body.reason, event_id=body.event_id)


@router.post("/missions/{mission_id}/status")
async def mission_status(mission_id: str, body: StatusBody,
                         user: dict = Depends(require("mission:respond"))):
    """Responder progress only — approval/dispatch stages are NOT reachable here."""
    if body.status not in RESPONDER_STAGES:
        raise ApiError(422, "VALIDATION_FAILED",
                       f"status must be one of {sorted(RESPONDER_STAGES)}",
                       [{"field": "status", "issue": "not a responder-controlled stage"}])
    return await operations.transition_mission(
        db, mission_id=mission_id, target=RESPONDER_STAGES[body.status], actor=user,
        reason=body.reason, outcome=body.outcome, event_id=body.event_id)


@router.get("/missions/{mission_id}/route")
async def mission_route(mission_id: str, user: dict = Depends(require("mission:read"))):
    m = await _mission(mission_id)
    return {"mission_id": mission_id, "route": m.get("route")}


@router.post("/missions/{mission_id}/route/recompute")
async def recompute_route(mission_id: str, user: dict = Depends(require("mission:read"))):
    m = await _mission(mission_id)
    graph = await operations.get_graph(db)
    hazards = await operations.active_hazards(db)
    resource = await db.resources.find_one({"resource_id": m.get("resource_id")}, {"_id": 0})
    req = await db.emergency_requests.find_one(
        {"request_id": m.get("emergency_request_id")}, {"_id": 0})
    if not resource or not req:
        raise ApiError(409, "MISSING_GEOMETRY", "Resource or request location unavailable")
    route = compute_route(graph, hazards, resource["location"]["coordinates"],
                          req["location"]["coordinates"],
                          "boat" if resource.get("kind") == "boat" else "road")
    await db.missions.update_one({"mission_id": mission_id}, {"$set": {"route": route}})
    return {"mission_id": mission_id, "route": route}


@router.get("/missions/{mission_id}/history")
async def mission_history(mission_id: str, user: dict = Depends(require("mission:read"))):
    m = await _mission(mission_id)
    events = await db.events.find({"payload.mission_id": mission_id},
                                  {"_id": 0}).sort("logical_timestamp", 1).to_list(200)
    return {"mission_id": mission_id, "history": m.get("history", []), "events": events}
