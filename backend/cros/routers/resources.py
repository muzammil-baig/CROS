from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import require
from ..constants import EventType
from ..db import db
from ..errors import ApiError, not_found
from ..models import GeoPoint
from ..services import allocation, capacity, operations
from ..services.routing import compute_route, haversine_m
from ..ulid import new_ulid
from .deps import emit_client_event, scope_responder

router = APIRouter(tags=["resources-facilities"])


class ResourceBody(BaseModel):
    kind: str
    label: str
    capacity: int = Field(default=1, ge=0, le=1000)
    location: GeoPoint
    capabilities: list[str] = Field(default_factory=list)
    responder_user_id: Optional[str] = None


class ResourceStatusBody(BaseModel):
    status: str
    location: Optional[GeoPoint] = None


class AvailabilityBody(BaseModel):
    availability: str


class LocationBody(BaseModel):
    location: GeoPoint


class CapacityBody(BaseModel):
    counter_kind: str
    operation: str
    amount: int = Field(ge=0, le=100000)
    operation_id: Optional[str] = None


class MatchBody(BaseModel):
    location: GeoPoint
    required_specialty: Optional[str] = None
    people_count: int = 1


class AllocationBody(BaseModel):
    request_ids: Optional[list[str]] = None


# ------------------------------------------------------------------ resources
@router.post("/resources", status_code=201)
async def create_resource(body: ResourceBody,
                          user: dict = Depends(require("resource:write"))):
    rid = f"RES-{new_ulid()}"
    doc = {**body.model_dump(), "resource_id": rid, "status": "available",
           "org_id": user.get("org_id"), "location": body.location.model_dump()}
    await db.resources.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.get("/resources")
async def list_resources(status: Optional[str] = None, kind: Optional[str] = None,
                         user: dict = Depends(require("resource:read"))):
    q = {}
    if status:
        q["status"] = status
    if kind:
        q["kind"] = kind
    docs = await db.resources.find(q, {"_id": 0}).sort("label", 1).to_list(500)
    return {"items": docs, "count": len(docs)}


@router.get("/resources/{resource_id}")
async def get_resource(resource_id: str, user: dict = Depends(require("resource:read"))):
    doc = await db.resources.find_one({"resource_id": resource_id}, {"_id": 0})
    if not doc:
        raise not_found("Resource not found")
    return doc


@router.patch("/resources/{resource_id}/status")
async def patch_resource_status(resource_id: str, body: ResourceStatusBody,
                               user: dict = Depends(require("resource:write"))):
    doc = await db.resources.find_one({"resource_id": resource_id}, {"_id": 0})
    if not doc:
        raise not_found("Resource not found")
    if body.status not in ("available", "assigned", "out_of_service", "maintenance"):
        raise ApiError(422, "VALIDATION_FAILED", "Invalid resource status")
    result = await emit_client_event(
        event_type=EventType.RESOURCE_STATUS_CHANGED.value,
        payload={"resource_id": resource_id, "previous_status": doc["status"],
                 "new_status": body.status,
                 "location": body.location.model_dump() if body.location else None,
                 "mission_id": doc.get("assigned_mission_id")},
        user=user)
    return {"resource_id": resource_id, "status": body.status,
            "event_id": result.get("event_id")}


# ----------------------------------------------------------------- responders
@router.get("/responders/me")
async def responder_me(user: dict = Depends(require("mission:respond"))):
    doc = await db.responders.find_one({"user_id": user["user_id"]}, {"_id": 0})
    resource = await db.resources.find_one({"responder_user_id": user["user_id"]}, {"_id": 0})
    missions = await db.missions.find(
        {"responder_user_id": user["user_id"]}, {"_id": 0}).sort("created_at", -1).to_list(50)
    return {"responder": doc, "resource": resource, "missions": missions}


@router.patch("/responders/me/availability")
async def set_availability(body: AvailabilityBody,
                           user: dict = Depends(require("mission:respond"))):
    if body.availability not in ("available", "busy", "off_duty"):
        raise ApiError(422, "VALIDATION_FAILED", "Invalid availability")
    await db.responders.update_one({"user_id": user["user_id"]},
                                   {"$set": {"availability": body.availability}}, upsert=True)
    return {"user_id": user["user_id"], "availability": body.availability}


@router.post("/responders/me/location")
async def set_location(body: LocationBody, user: dict = Depends(require("mission:respond"))):
    from ..models import utcnow_iso
    await db.responders.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"location": body.location.model_dump(),
                  "last_location_at": utcnow_iso()}}, upsert=True)
    await db.resources.update_one({"responder_user_id": user["user_id"]},
                                  {"$set": {"location": body.location.model_dump()}})
    return {"ok": True, "location": body.location.model_dump()}


@router.get("/responders/me/missions")
async def my_missions(user: dict = Depends(require("mission:respond"))):
    docs = await db.missions.find({"responder_user_id": user["user_id"]},
                                  {"_id": 0}).sort("created_at", -1).to_list(100)
    for m in docs:
        if m.get("emergency_request_id"):
            m["request"] = await db.emergency_requests.find_one(
                {"request_id": m["emergency_request_id"]}, {"_id": 0})
    return {"items": docs, "count": len(docs)}


@router.get("/responders")
async def list_responders(user: dict = Depends(require("resource:read"))):
    docs = await db.responders.find({}, {"_id": 0}).to_list(200)
    return {"items": [scope_responder(d, user) for d in docs], "count": len(docs)}


# ----------------------------------------------------------------- facilities
def _facility_view(f: dict) -> dict:
    return {**f, "availability": capacity.availability(f)}


async def _facilities(ftype: str, user):
    docs = await db.facilities.find({"facility_type": ftype}, {"_id": 0}).to_list(200)
    return {"items": [_facility_view(d) for d in docs], "count": len(docs),
            "counter_semantics": "operation_based_crdt_counter",
            "counter_kinds": capacity.COUNTER_KINDS[ftype]}


@router.get("/hospitals")
async def list_hospitals(user: dict = Depends(require("facility:read"))):
    return await _facilities("hospital", user)


@router.get("/shelters")
async def list_shelters(user: dict = Depends(require("facility:read"))):
    return await _facilities("shelter", user)


async def _facility(facility_id: str, ftype: str) -> dict:
    doc = await db.facilities.find_one({"facility_id": facility_id,
                                        "facility_type": ftype}, {"_id": 0})
    if not doc:
        raise not_found(f"{ftype.title()} not found")
    return doc


@router.get("/hospitals/{facility_id}")
async def get_hospital(facility_id: str, user: dict = Depends(require("facility:read"))):
    return _facility_view(await _facility(facility_id, "hospital"))


@router.get("/shelters/{facility_id}")
async def get_shelter(facility_id: str, user: dict = Depends(require("facility:read"))):
    return _facility_view(await _facility(facility_id, "shelter"))


async def _capacity_op(facility_id: str, ftype: str, body: CapacityBody, user: dict):
    await _facility(facility_id, ftype)
    if body.counter_kind not in capacity.COUNTER_KINDS[ftype]:
        raise ApiError(422, "VALIDATION_FAILED",
                       f"counter_kind must be one of {capacity.COUNTER_KINDS[ftype]}")
    if body.operation not in ("increment", "decrement"):
        raise ApiError(422, "VALIDATION_FAILED", "operation must be increment or decrement")
    op_id = body.operation_id or new_ulid()
    prior = await db.capacity_ops.find_one({"operation_id": op_id}, {"_id": 0})
    event_type = (EventType.HOSPITAL_CAPACITY_CHANGED.value if ftype == "hospital"
                  else EventType.SHELTER_CAPACITY_CHANGED.value)
    result = await emit_client_event(
        event_type=event_type,
        payload={"facility_id": facility_id, "operation": body.operation,
                 "amount": body.amount, "counter_kind": body.counter_kind,
                 "operation_id": op_id},
        user=user, event_id=None)
    doc = await _facility(facility_id, ftype)
    return {"facility_id": facility_id, "operation_id": op_id,
            "idempotent": prior is not None,
            "crdt": "operation_based_counter",
            "availability": capacity.availability(doc), "counters": doc.get("counters"),
            "event_id": result.get("event_id")}


@router.patch("/hospitals/{facility_id}/capacity")
async def hospital_capacity(facility_id: str, body: CapacityBody,
                            user: dict = Depends(require("hospital:capacity"))):
    return await _capacity_op(facility_id, "hospital", body, user)


@router.patch("/shelters/{facility_id}/capacity")
async def shelter_capacity(facility_id: str, body: CapacityBody,
                           user: dict = Depends(require("shelter:capacity"))):
    return await _capacity_op(facility_id, "shelter", body, user)


async def _match(ftype: str, body: MatchBody):
    graph = await operations.get_graph(db)
    hazards = await operations.active_hazards(db)
    docs = await db.facilities.find({"facility_type": ftype}, {"_id": 0}).to_list(200)
    out = []
    for f in docs:
        avail = capacity.availability(f)
        free = avail.get("beds_available") if ftype == "hospital" else avail.get("available")
        if body.required_specialty and body.required_specialty not in (f.get("specialties") or []):
            continue
        route = compute_route(graph, hazards, body.location.coordinates,
                              f["location"]["coordinates"], "road")
        out.append({"facility_id": f["facility_id"], "name": f["name"],
                    "available": free, "sufficient": (free or 0) >= body.people_count,
                    "specialties": f.get("specialties"),
                    "eta_seconds": route.get("eta_seconds"),
                    "route_status": route["status"],
                    "distance_m": route.get("distance_m"),
                    "availability": avail})
    out = [o for o in out if o["route_status"] != "NO_ROUTE"]
    out.sort(key=lambda o: (not o["sufficient"], o["eta_seconds"] or 1e9))
    return {"matches": out, "criteria": {"specialty": body.required_specialty,
                                         "people_count": body.people_count},
            "algorithm": "deterministic_capacity_and_route_v1"}


@router.post("/hospitals/match")
async def match_hospital(body: MatchBody, user: dict = Depends(require("facility:read"))):
    return await _match("hospital", body)


@router.post("/shelters/match")
async def match_shelter(body: MatchBody, user: dict = Depends(require("facility:read"))):
    return await _match("shelter", body)


# ---------------------------------------------------------------- allocation
@router.post("/allocations/propose")
async def propose_allocation(body: AllocationBody,
                             user: dict = Depends(require("allocation:propose"))):
    q = {"status": {"$in": ["new", "triaged"]}}
    if body.request_ids:
        q = {"request_id": {"$in": body.request_ids}}
    requests = await db.emergency_requests.find(q, {"_id": 0}).sort(
        "priority.score", -1).to_list(50)
    resources = await db.resources.find({"status": "available"}, {"_id": 0}).to_list(200)
    graph = await operations.get_graph(db)
    hazards = await operations.active_hazards(db)
    proposal = allocation.propose(requests, resources, graph, hazards)
    proposal["proposal_id"] = new_ulid()
    proposal["request_ids"] = [r["request_id"] for r in requests]
    result = await emit_client_event(event_type=EventType.ALLOCATION_PROPOSED.value,
                                     payload=proposal, user=user)
    proposal["event_id"] = result.get("event_id")
    return proposal


@router.get("/allocations/{proposal_id}")
async def get_allocation(proposal_id: str,
                         user: dict = Depends(require("allocation:propose"))):
    doc = await db.allocation_proposals.find_one({"proposal_id": proposal_id}, {"_id": 0})
    if not doc:
        raise not_found("Allocation proposal not found")
    return doc
