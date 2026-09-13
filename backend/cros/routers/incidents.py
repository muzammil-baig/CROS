from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import shape

from ..auth import require
from ..constants import EventType
from ..db import db
from ..errors import ApiError, not_found
from ..models import GeoPoint
from ..services import operations
from ..services.hazard import get_module, registered_types
from ..ulid import new_ulid
from .deps import emit_client_event

router = APIRouter(tags=["incidents-hazards"])

INCIDENT_TRANSITIONS = {"active": ["contained", "closed"], "contained": ["active", "closed"],
                        "closed": []}


class CreateIncidentBody(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    hazard_type: str = "flood"
    severity: str = "moderate"
    location: GeoPoint
    area: Optional[dict] = None


class TransitionBody(BaseModel):
    new_status: str
    reason: Optional[str] = None


class CommanderBody(BaseModel):
    commander_user_id: str


class PatchIncidentBody(BaseModel):
    name: Optional[str] = None
    severity: Optional[str] = None


class CreateHazardBody(BaseModel):
    hazard_type: str = "flood"
    geometry: dict
    description: str = Field(default="", max_length=1000)

    @field_validator("geometry")
    @classmethod
    def validate_geometry(cls, value: dict) -> dict:
        if not isinstance(value, dict) or value.get("type") not in {
                "Point", "LineString", "Polygon", "MultiPoint", "MultiLineString",
                "MultiPolygon", "GeometryCollection"}:
            raise ValueError("geometry must be a supported GeoJSON geometry")
        if any(key.startswith("__") for key in value):
            raise ValueError("geometry contains forbidden fields")
        try:
            geometry = shape(value)
        except Exception as exc:
            raise ValueError("geometry is not valid GeoJSON") from exc
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("geometry must be non-empty and valid")
        minx, miny, maxx, maxy = geometry.bounds
        if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and
                -90 <= miny <= 90 and -90 <= maxy <= 90):
            raise ValueError("geometry coordinates are outside geographic bounds")
        return value
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    water_level_m: Optional[float] = None
    rise_rate_m_per_hour: Optional[float] = None
    rainfall_mm_per_hour: Optional[float] = None
    wind_speed_kph: Optional[float] = None
    gust_speed_kph: Optional[float] = None
    storm_surge_m: Optional[float] = None
    surge_rate_m_per_hour: Optional[float] = None
    intensification_kph_per_hour: Optional[float] = None
    track_speed_kph: Optional[float] = None
    debris: bool = False
    road_blocked: bool = False
    incident_id: Optional[str] = None


# ------------------------------------------------------------------ incidents
@router.post("/incidents", status_code=201)
async def create_incident(body: CreateIncidentBody,
                          user: dict = Depends(require("incident:create"))):
    incident_id = f"INC-{new_ulid()}"
    result = await emit_client_event(
        event_type=EventType.INCIDENT_CREATED.value,
        payload={"incident_id": incident_id, "name": body.name,
                 "hazard_type": body.hazard_type, "status": "active",
                 "severity": body.severity, "commander_user_id": user["user_id"],
                 "location": body.location.model_dump(), "area": body.area},
        user=user, priority="high")
    doc = await db.incidents.find_one({"incident_id": incident_id}, {"_id": 0})
    return {"incident": doc, "event_id": result.get("event_id")}


@router.get("/incidents")
async def list_incidents(status: Optional[str] = None,
                         user: dict = Depends(require("incident:read"))):
    q = {"status": status} if status else {}
    docs = await db.incidents.find(q, {"_id": 0}).sort("created_at", -1).to_list(100)
    for d in docs:
        d["request_count"] = await db.emergency_requests.count_documents(
            {"incident_id": d["incident_id"]})
        d["open_request_count"] = await db.emergency_requests.count_documents(
            {"incident_id": d["incident_id"], "status": {"$in": ["new", "triaged"]}})
        d["mission_count"] = await db.missions.count_documents(
            {"incident_id": d["incident_id"]})
    return {"items": docs, "count": len(docs)}


@router.get("/incidents/search")
async def search_incidents(q: str = Query(min_length=1),
                           user: dict = Depends(require("incident:read"))):
    docs = await db.incidents.find({"name": {"$regex": q, "$options": "i"}},
                                   {"_id": 0}).to_list(50)
    return {"items": docs, "count": len(docs)}


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, user: dict = Depends(require("incident:read"))):
    doc = await db.incidents.find_one({"incident_id": incident_id}, {"_id": 0})
    if not doc:
        raise not_found("Incident not found")
    return doc


@router.patch("/incidents/{incident_id}")
async def patch_incident(incident_id: str, body: PatchIncidentBody,
                         user: dict = Depends(require("incident:update"))):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise ApiError(422, "VALIDATION_FAILED", "No fields to update")
    result = await emit_client_event(
        event_type=EventType.INCIDENT_UPDATED.value,
        payload={"incident_id": incident_id, "changes": changes}, user=user)
    return {"incident_id": incident_id, "changes": changes,
            "event_id": result.get("event_id")}


@router.post("/incidents/{incident_id}/transition")
async def transition_incident(incident_id: str, body: TransitionBody,
                              user: dict = Depends(require("incident:update"))):
    doc = await db.incidents.find_one({"incident_id": incident_id}, {"_id": 0})
    if not doc:
        raise not_found("Incident not found")
    allowed = INCIDENT_TRANSITIONS.get(doc["status"], [])
    if body.new_status not in allowed:
        raise ApiError(409, "TRANSITION_INVALID",
                       f"Invalid transition {doc['status']} -> {body.new_status}")
    result = await emit_client_event(
        event_type=EventType.INCIDENT_STATUS_CHANGED.value,
        payload={"incident_id": incident_id, "previous_status": doc["status"],
                 "new_status": body.new_status, "reason": body.reason},
        user=user, priority="high")
    return {"incident_id": incident_id, "status": body.new_status,
            "event_id": result.get("event_id")}


@router.post("/incidents/{incident_id}/commander")
async def assign_commander(incident_id: str, body: CommanderBody,
                           user: dict = Depends(require("incident:update"))):
    target = await db.users.find_one({"user_id": body.commander_user_id}, {"_id": 0})
    if not target or target["role"] != "incident_commander":
        raise ApiError(422, "VALIDATION_FAILED", "Target user is not an incident commander")
    result = await emit_client_event(
        event_type=EventType.INCIDENT_UPDATED.value,
        payload={"incident_id": incident_id,
                 "commander_user_id": body.commander_user_id, "changes": {}},
        user=user, priority="high")
    return {"incident_id": incident_id, "commander_user_id": body.commander_user_id,
            "event_id": result.get("event_id")}


@router.get("/incidents/{incident_id}/timeline")
async def incident_timeline(incident_id: str, limit: int = Query(200, le=1000),
                            user: dict = Depends(require("incident:read"))):
    events = await db.events.find(
        {"payload.incident_id": incident_id}, {"_id": 0}).sort(
        "logical_timestamp", -1).to_list(limit)
    return {"incident_id": incident_id, "events": events, "count": len(events)}


# -------------------------------------------------------------------- hazards
@router.post("/hazards", status_code=201)
async def create_hazard(body: CreateHazardBody,
                        user: dict = Depends(require("hazard:create"))):
    hazard_id = f"HZ-{new_ulid()}"
    if body.hazard_type not in registered_types() and body.hazard_type not in (
            "bridge_damage", "debris", "structural", "fire", "landslide"):
        raise ApiError(422, "VALIDATION_FAILED",
                       f"Unsupported hazard_type; modules: {registered_types()}",
                       [{"field": "hazard_type", "issue": "no hazard module registered"}])
    module = get_module(body.hazard_type)
    payload = body.model_dump()
    payload.update({
        "hazard_id": hazard_id,
        "source_provenance": f"{user['role']}_report",
        "verification": {"status": "VERIFIED" if user["role"] in
                         ("field_responder", "incident_commander", "government_officer")
                         else "UNVERIFIED",
                         "confidence": 0.9 if user["role"] != "citizen" else 0.5},
        "active": True})
    if body.hazard_type in registered_types():
        # Hazard-specific numerical logic stays inside the pluggable module.
        computed = module.severity(payload)
        if computed > 0:
            payload["severity"] = computed
        payload["road_blocked"] = module.blocks_road(payload) or body.road_blocked
    result = await emit_client_event(event_type=EventType.HAZARD_REPORTED.value,
                                     payload=payload, user=user, priority="high")
    doc = await db.hazards.find_one({"hazard_id": hazard_id}, {"_id": 0})
    return {"hazard": doc, "hazard_id": hazard_id,
            "hazard_module": body.hazard_type if body.hazard_type
            in registered_types() else "generic", "severity": (doc or {}).get("severity", payload.get("severity")),
            "road_blocked": (doc or {}).get("road_blocked", payload.get("road_blocked", False)),
            "event_id": result.get("event_id")}


@router.get("/hazards")
async def list_hazards(active: bool = True, incident_id: Optional[str] = None,
                       user: dict = Depends(require("hazard:read"))):
    q = {"active": active}
    if incident_id:
        q["incident_id"] = incident_id
    docs = await db.hazards.find(q, {"_id": 0}).sort("severity", -1).to_list(500)
    return {"items": docs, "count": len(docs), "hazard_modules": registered_types()}


async def _hazard(hazard_id: str) -> dict:
    doc = await db.hazards.find_one({"hazard_id": hazard_id}, {"_id": 0})
    if not doc:
        raise not_found("Hazard not found")
    return doc


@router.get("/hazards/{hazard_id}")
async def get_hazard(hazard_id: str, user: dict = Depends(require("hazard:read"))):
    return await _hazard(hazard_id)


@router.get("/hazards/{hazard_id}/geometry")
async def hazard_geometry(hazard_id: str, user: dict = Depends(require("hazard:read"))):
    d = await _hazard(hazard_id)
    return {"hazard_id": hazard_id, "geometry": d.get("geometry")}


@router.get("/hazards/{hazard_id}/provenance")
async def hazard_provenance(hazard_id: str, user: dict = Depends(require("hazard:read"))):
    d = await _hazard(hazard_id)
    events = await db.events.find({"payload.hazard_id": hazard_id},
                                  {"_id": 0}).sort("logical_timestamp", 1).to_list(100)
    return {"hazard_id": hazard_id, "source_provenance": d.get("source_provenance"),
            "origin_device_id": d.get("origin_device_id"),
            "verification": d.get("verification"), "events": events}


@router.get("/hazards/{hazard_id}/evidence")
async def hazard_evidence(hazard_id: str, user: dict = Depends(require("hazard:read"))):
    d = await _hazard(hazard_id)
    reports = await db.reports.find({"kind": "hazard"}, {"_id": 0}).to_list(100)
    return {"hazard_id": hazard_id, "verification": d.get("verification"),
            "supporting_reports": reports}


@router.get("/hazards/{hazard_id}/prediction")
async def hazard_prediction(hazard_id: str, horizon_minutes: int = Query(60, ge=5, le=720),
                            user: dict = Depends(require("hazard:read"))):
    d = await _hazard(hazard_id)
    module = get_module(d.get("hazard_type", "flood"))
    return module.predict(d, horizon_minutes)


@router.get("/geo/road-graph")
async def road_graph(user: dict = Depends(require("hazard:read"))):
    graph = await operations.get_graph(db)
    hazards = await operations.active_hazards(db)
    from ..services.routing import hazard_overlay
    overlay = hazard_overlay(graph, hazards)
    return {"graph_id": graph["graph_id"], "area": graph["area"],
            "node_count": len(graph["nodes"]), "edges": graph["edges"],
            "nodes": graph["nodes"], "overlay": overlay,
            "graph_updated_at": graph["updated_at"],
            "blocked_edges": sum(1 for v in overlay.values() if v["blocked"])}
