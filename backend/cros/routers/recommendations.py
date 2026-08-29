from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import require
from ..db import db
from ..errors import not_found
from ..services import operations

router = APIRouter(tags=["recommendations"])


class DecisionBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class OverrideBody(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    acknowledge_risk: bool


@router.get("/recommendations")
async def list_recommendations(status: Optional[str] = None,
                               risk_tier: Optional[str] = None,
                               limit: int = Query(100, le=300),
                               user: dict = Depends(require("recommendation:read"))):
    q = {}
    if status:
        q["approval_status"] = status
    if risk_tier:
        q["risk_tier"] = risk_tier
    docs = await db.recommendations.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.get("/recommendations/{recommendation_id}")
async def get_recommendation(recommendation_id: str,
                             user: dict = Depends(require("recommendation:read"))):
    doc = await db.recommendations.find_one({"recommendation_id": recommendation_id},
                                            {"_id": 0})
    if not doc:
        raise not_found("Recommendation not found")
    approval = await db.approvals.find_one({"recommendation_id": recommendation_id},
                                           {"_id": 0})
    doc["approval"] = approval
    if doc.get("emergency_request_id"):
        doc["request"] = await db.emergency_requests.find_one(
            {"request_id": doc["emergency_request_id"]}, {"_id": 0})
    events = await db.events.find({"payload.recommendation_id": recommendation_id},
                                  {"_id": 0}).sort("logical_timestamp", 1).to_list(50)
    doc["events"] = events
    return doc


@router.post("/recommendations/{recommendation_id}/approve")
async def approve(recommendation_id: str, body: DecisionBody,
                  user: dict = Depends(require("recommendation:decide"))):
    return await operations.decide_recommendation(
        db, recommendation_id=recommendation_id, approver=user, approve=True,
        reason=body.reason)


@router.post("/recommendations/{recommendation_id}/reject")
async def reject(recommendation_id: str, body: DecisionBody,
                 user: dict = Depends(require("recommendation:decide"))):
    return await operations.decide_recommendation(
        db, recommendation_id=recommendation_id, approver=user, approve=False,
        reason=body.reason)


@router.post("/recommendations/{recommendation_id}/emergency-override")
async def emergency_override(recommendation_id: str, body: OverrideBody,
                             user: dict = Depends(require("recommendation:override"))):
    from ..errors import ApiError
    if not body.acknowledge_risk:
        raise ApiError(422, "VALIDATION_FAILED", "Risk acknowledgement is required")
    return await operations.decide_recommendation(
        db, recommendation_id=recommendation_id, approver=user, approve=True,
        reason=body.reason, emergency_override=True)


@router.get("/recommendations/{recommendation_id}/outcome")
async def outcome(recommendation_id: str,
                  user: dict = Depends(require("recommendation:read"))):
    rec = await db.recommendations.find_one({"recommendation_id": recommendation_id},
                                            {"_id": 0})
    if not rec:
        raise not_found("Recommendation not found")
    mission = await db.missions.find_one({"recommendation_id": recommendation_id}, {"_id": 0})
    approval = await db.approvals.find_one({"recommendation_id": recommendation_id},
                                           {"_id": 0})
    return {"recommendation_id": recommendation_id,
            "approval_status": rec.get("approval_status"),
            "critic": rec.get("critic"), "risk_tier": rec.get("risk_tier"),
            "confidence": rec.get("confidence"), "approval": approval,
            "mission": mission,
            "mission_status": mission.get("status") if mission else None}


@router.get("/approvals")
async def list_approvals(pending: bool = True,
                         user: dict = Depends(require("recommendation:read"))):
    if pending:
        docs = await db.recommendations.find(
            {"approval_status": {"$in": ["pending_approval", "blocked"]}},
            {"_id": 0}).sort("created_at", -1).to_list(100)
        return {"queue": docs, "count": len(docs), "kind": "pending"}
    docs = await db.approvals.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return {"queue": docs, "count": len(docs), "kind": "decided", "immutable": True}
