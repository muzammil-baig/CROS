from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import require
from ..config import PERSISTENCE_BACKEND
from ..db import db, sim_db
from ..errors import ApiError, not_found
from ..services import simulation

router = APIRouter(tags=["simulation"])


class StartBody(BaseModel):
    scenario: str
    ticks: int = Field(default=4, ge=1, le=20)
    rainfall_mm_per_hour: float = Field(default=25.0, ge=0, le=300)
    requests_per_tick: int = Field(default=2, ge=0, le=10)


@router.get("/simulations/scenarios")
async def scenarios(user: dict = Depends(require("simulation:run"))):
    return {"scenarios": [{"key": k, "description": v}
                          for k, v in simulation.SCENARIOS.items()],
            "isolation": {"schema": "simulation" if PERSISTENCE_BACKEND == "postgres" else sim_db.name,
                          "production_database": "core/events" if PERSISTENCE_BACKEND == "postgres" else db.name,
                          "event_namespace": "simulation",
                          "transport_registry": "dedicated_instance"}}


@router.post("/simulations", status_code=201)
async def start_simulation(body: StartBody, user: dict = Depends(require("simulation:run"))):
    try:
        return await simulation.start(scenario=body.scenario,
                                      params=body.model_dump(exclude={"scenario"}),
                                      actor=user)
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_FAILED", str(exc))
    except RuntimeError as exc:
        raise ApiError(503, "SIMULATION_ISOLATION_FAILURE", str(exc))


@router.get("/simulations")
async def list_simulations(user: dict = Depends(require("simulation:run"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        docs = await postgres.list_simulation_runs()
        return {"items": docs, "count": len(docs)}
    docs = await sim_db.simulation_runs.find({}, {"_id": 0, "steps": 0}).sort(
        "started_at", -1).to_list(50)
    return {"items": docs, "count": len(docs)}


@router.get("/simulations/{simulation_id}")
async def get_simulation(simulation_id: str, user: dict = Depends(require("simulation:run"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        try:
            doc = await postgres.get_simulation_run(simulation_id)
        except ValueError:
            doc = None
        if not doc:
            raise not_found("Simulation not found")
        return {**doc, "simulation_id": simulation_id}
    doc = await sim_db.simulation_runs.find_one({"simulation_id": simulation_id}, {"_id": 0})
    if not doc:
        raise not_found("Simulation not found")
    return doc


@router.get("/simulations/{simulation_id}/events")
async def simulation_events(simulation_id: str, limit: int = Query(200, le=1000),
                            user: dict = Depends(require("simulation:run"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        try:
            events = await postgres.list_simulation_events(simulation_id, limit)
        except ValueError:
            raise not_found("Simulation not found")
        return {"simulation_id": simulation_id, "events": events, "count": len(events),
                "namespace": "simulation", "schema": "simulation"}
    events = await sim_db.events.find({}, {"_id": 0}).sort(
        "logical_timestamp", -1).to_list(limit)
    return {"simulation_id": simulation_id, "events": events, "count": len(events),
            "namespace": "simulation", "database": sim_db.name}


@router.post("/simulations/{simulation_id}/abort")
async def abort_simulation(simulation_id: str,
                           user: dict = Depends(require("simulation:run"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        try:
            doc = await postgres.get_simulation_run(simulation_id)
        except ValueError:
            doc = None
        if not doc:
            raise not_found("Simulation not found")
        return await simulation.abort(simulation_id)
    doc = await sim_db.simulation_runs.find_one({"simulation_id": simulation_id}, {"_id": 0})
    if not doc:
        raise not_found("Simulation not found")
    return await simulation.abort(simulation_id)


@router.get("/simulations/{simulation_id}/metrics")
async def simulation_metrics(simulation_id: str,
                             user: dict = Depends(require("simulation:run"))):
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        try:
            doc = await postgres.get_simulation_run(simulation_id)
        except ValueError:
            doc = None
        if not doc:
            raise not_found("Simulation not found")
        return doc.get("metrics") or {}
    doc = await sim_db.simulation_runs.find_one({"simulation_id": simulation_id}, {"_id": 0})
    if not doc:
        raise not_found("Simulation not found")
    if doc.get("metrics"):
        return doc["metrics"]
    from ..services.transport import TransportRegistry
    return await simulation.compute_metrics(sim_db, simulation_id, TransportRegistry())
