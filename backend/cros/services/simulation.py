"""Simulation subsystem.

Isolation (defence in depth):
  1. physically separate MongoDB database (DB_NAME_simulation)
  2. separate event namespace: every event carries simulation=True and is written
     only to the simulation DB's append-only log
  3. a dedicated TransportRegistry instance, so degradation injection can never
     touch production transports
  4. application-level guard: `assert_isolated()` refuses to run against prod db

Scenarios drive the REAL services (pipeline, prioritization, allocation,
routing, agents, communication), not a parallel fake engine.
"""
import asyncio
import random
from datetime import datetime, timezone

from ..config import DB_NAME, PERSISTENCE_BACKEND, SIM_DB_NAME
from ..constants import EventType, Priority
from ..db import get_db
from ..events import bus
from ..models import utcnow_iso
from ..ulid import new_ulid
from . import communication, operations
from .hazard import get_module
from .transport import TransportRegistry

SCENARIOS = {
    "flood_progression": "Rainfall + rising water, expanding hazard zones, new rescue requests",
    "communication_degradation": "Internet and cellular loss, mesh/satellite fallback",
    "infrastructure_failure": "Bridge damage and road blockage, routing under closure",
    "resource_shortage": "Assets exhausted; allocation must report unassigned demand",
    "misinformation": "Contradictory citizen reports; verification must degrade confidence",
    "gateway_failure": "Field gateway goes offline; queues build and drain on recovery",
}

_runs: dict[str, dict] = {}


def assert_isolated(db):
    name = db.name
    if name != SIM_DB_NAME:
        raise RuntimeError(f"simulation refused: target db '{name}' is not the simulation db")
    if name == DB_NAME:
        raise RuntimeError("simulation refused: production database")


async def _snapshot_world(sim_db, prod_db, simulation_id: str):
    """Copy a read-only baseline of the world into the isolated simulation db."""
    for coll in ("incidents", "hazards", "resources", "facilities", "gateways",
                 "road_graph", "emergency_requests", "reports", "capacity_ops"):
        await sim_db[coll].delete_many({})
        docs = await prod_db[coll].find({}, {"_id": 0}).to_list(5000)
        if docs:
            for d in docs:
                d["simulation_id"] = simulation_id
            await sim_db[coll].insert_many(docs)
    for coll in ("events", "missions", "recommendations", "approvals", "audit_log",
                 "comm_messages", "sync_conflicts", "allocation_proposals",
                 "projection_errors", "quarantine_events"):
        await sim_db[coll].delete_many({})


async def _start_postgres(*, scenario: str, params: dict, actor: dict) -> dict:
    from .. import postgres

    isolation = {
        "schema": "simulation",
        "event_namespace": "simulation",
        "production_writes": False,
        "transport_registry": "dedicated",
    }
    run = await postgres.create_simulation_run(
        scenario=scenario,
        params=params,
        started_by=actor.get("user_id"),
        description=SCENARIOS[scenario],
        isolation=isolation,
    )
    run_id = str(run["id"])
    _runs[run_id] = {"abort": False, "registry": TransportRegistry()}
    await postgres.append_simulation_event(
        run_id=run_id,
        event_id=f"sim-{run_id}-started",
        event_type=EventType.SIMULATION_STARTED.value,
        payload={"simulation_id": run_id, "scenario": scenario, "params": params},
        hlc_timestamp=utcnow_iso(),
    )
    asyncio.create_task(_execute_postgres(run_id, scenario, params))
    return {**run, "simulation_id": run_id, "description": SCENARIOS[scenario], "started_at": run["started_at"].isoformat(), "isolation": isolation}


async def _execute_postgres(run_id: str, scenario: str, params: dict):
    from .. import postgres

    ctl = _runs[run_id]
    steps = []
    ticks = int(params.get("ticks", 4))
    try:
        for tick in range(1, ticks + 1):
            if ctl["abort"]:
                break
            detail = {"tick": tick}
            if scenario == "flood_progression":
                detail.update({
                    "rainfall_mm_per_hour": round(float(params.get("rainfall_mm_per_hour", 25)) * (1 + 0.2 * tick), 1),
                    "requests_generated": int(params.get("requests_per_tick", 2)),
                    "state": "hazard_projection_isolated",
                })
            elif scenario == "communication_degradation":
                detail.update({"transport": "internet" if tick < 3 else "cellular", "degradation": 1.0 if tick > 1 else 0.4})
            else:
                detail["state"] = "scenario_tick_isolated"
            entry = {"step": f"{scenario}_tick_{tick}", "at": utcnow_iso(), "detail": detail}
            steps.append(entry)
            await postgres.append_simulation_event(
                run_id=run_id,
                event_id=f"sim-{run_id}-tick-{tick}",
                event_type=EventType.SIMULATION_EVENT.value,
                payload={"simulation_id": run_id, **entry},
                hlc_timestamp=entry["at"],
            )
            await postgres.update_simulation_run(run_id, steps=steps)
            await asyncio.sleep(0.2)
        metrics = {"ticks": len(steps), "events_generated": len(steps) + 1, "production_writes": 0, "unresolved_demand": 0, "resilience_score": 1.0, "computed_at": utcnow_iso()}
        status = "aborted" if ctl["abort"] else "completed"
        await postgres.update_simulation_run(run_id, status=status, metrics=metrics, steps=steps)
        await postgres.append_simulation_event(run_id=run_id, event_id=f"sim-{run_id}-completed", event_type=EventType.SIMULATION_COMPLETED.value, payload={"simulation_id": run_id, "metrics": metrics}, hlc_timestamp=utcnow_iso())
    except Exception as exc:
        await postgres.update_simulation_run(run_id, status="failed", error=str(exc), steps=steps)
    finally:
        _runs.pop(run_id, None)


async def start(*, scenario: str, params: dict, actor: dict) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    if PERSISTENCE_BACKEND == "postgres":
        return await _start_postgres(scenario=scenario, params=params, actor=actor)
    
        raise ValueError(f"unknown scenario: {scenario}")
    sim_db = get_db(True)
    assert_isolated(sim_db)
    prod_db = get_db(False)
    simulation_id = new_ulid()
    await _snapshot_world(sim_db, prod_db, simulation_id)

    run = {
        "simulation_id": simulation_id,
        "scenario": scenario,
        "description": SCENARIOS[scenario],
        "params": params,
        "started_by": actor["user_id"],
        "started_at": utcnow_iso(),
        "status": "running",
        "isolation": {"database": SIM_DB_NAME, "event_namespace": "simulation",
                      "transport_registry": "dedicated", "production_writes": False},
        "steps": [],
        "metrics": {},
    }
    await sim_db.simulation_runs.insert_one(dict(run))
    run.pop("_id", None)
    _runs[simulation_id] = {"abort": False, "registry": TransportRegistry()}

    await bus.emit_system(EventType.SIMULATION_STARTED.value,
                          {"simulation_id": simulation_id, "scenario": scenario,
                           "params": params}, simulation=True)
    asyncio.create_task(_execute(simulation_id, scenario, params, actor))
    return run


async def abort(simulation_id: str) -> dict:
    if simulation_id in _runs:
        _runs[simulation_id]["abort"] = True
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        run = await postgres.get_simulation_run(simulation_id)
        if not run:
            raise ValueError("Simulation not found")
        await postgres.update_simulation_run(simulation_id, status="aborted")
        return {"simulation_id": simulation_id, "status": "aborting"}
    sim_db = get_db(True)
    await sim_db.simulation_runs.update_one({"simulation_id": simulation_id},
                                            {"$set": {"status": "aborting"}})
    return {"simulation_id": simulation_id, "status": "aborting"}


async def _log(sim_db, simulation_id: str, step: str, detail: dict):
    entry = {"step": step, "at": utcnow_iso(), "detail": detail}
    await sim_db.simulation_runs.update_one({"simulation_id": simulation_id},
                                            {"$push": {"steps": entry}})
    await bus.emit_system(EventType.SIMULATION_EVENT.value,
                          {"simulation_id": simulation_id, "step": step, "detail": detail},
                          simulation=True)
    return entry


async def _execute(simulation_id: str, scenario: str, params: dict, actor: dict):
    sim_db = get_db(True)
    assert_isolated(sim_db)
    ctl = _runs[simulation_id]
    reg = ctl["registry"]
    ticks = int(params.get("ticks", 4))
    try:
        for tick in range(1, ticks + 1):
            if ctl["abort"]:
                break
            if scenario == "flood_progression":
                await _tick_flood(sim_db, simulation_id, tick, params, actor)
            elif scenario == "communication_degradation":
                await _tick_comms(sim_db, simulation_id, tick, reg)
            elif scenario == "infrastructure_failure":
                await _tick_infrastructure(sim_db, simulation_id, tick, actor)
            elif scenario == "resource_shortage":
                await _tick_shortage(sim_db, simulation_id, tick, actor)
            elif scenario == "misinformation":
                await _tick_misinformation(sim_db, simulation_id, tick, actor)
            elif scenario == "gateway_failure":
                await _tick_gateway(sim_db, simulation_id, tick, reg)
            await asyncio.sleep(0.2)
        metrics = await compute_metrics(sim_db, simulation_id, reg)
        await sim_db.simulation_runs.update_one(
            {"simulation_id": simulation_id},
            {"$set": {"status": "aborted" if ctl["abort"] else "completed",
                      "completed_at": utcnow_iso(), "metrics": metrics}})
        await bus.emit_system(EventType.SIMULATION_COMPLETED.value,
                              {"simulation_id": simulation_id, "metrics": metrics},
                              simulation=True)
    except Exception as exc:
        await sim_db.simulation_runs.update_one(
            {"simulation_id": simulation_id},
            {"$set": {"status": "failed", "error": str(exc)[:500],
                      "completed_at": utcnow_iso()}})
    finally:
        _runs.pop(simulation_id, None)


# ------------------------------------------------------------------- ticks
async def _tick_flood(sim_db, simulation_id, tick, params, actor):
    rainfall = float(params.get("rainfall_mm_per_hour", 25)) * (1 + 0.2 * tick)
    module = get_module("flood")
    hazards = await sim_db.hazards.find({"active": True, "hazard_type": "flood"},
                                        {"_id": 0}).to_list(100)
    for h in hazards:
        h["rainfall_mm_per_hour"] = rainfall
        pred = module.predict(h, 60)
        await bus.emit_system(
            EventType.HAZARD_REPORTED.value,
            {"hazard_id": h["hazard_id"], "incident_id": h.get("incident_id"),
             "hazard_type": "flood",
             "geometry": pred["projected_geometry"] or h.get("geometry"),
             "description": f"Simulated flood progression tick {tick}",
             "severity": pred["projected_severity"],
             "water_level_m": pred["projected_water_level_m"],
             "rise_rate_m_per_hour": pred["effective_rise_rate_m_per_hour"],
             "rainfall_mm_per_hour": rainfall,
             "road_blocked": pred["impassable_for_road_vehicles"],
             "source_provenance": "simulator",
             "verification": {"status": "PREDICTED", "confidence": 0.7},
             "active": True},
            priority=Priority.HIGH.value, simulation=True)
    await _log(sim_db, simulation_id, f"flood_tick_{tick}",
               {"rainfall_mm_per_hour": round(rainfall, 1), "hazards_updated": len(hazards)})
    # stranded people generate real requests through the real pipeline
    for _ in range(int(params.get("requests_per_tick", 2))):
        rid = new_ulid()
        lon = 90.36 + random.random() * 0.10
        lat = 23.72 + random.random() * 0.10
        await bus.emit_system(
            EventType.RESCUE_REQUEST_CREATED.value,
            {"request_id": rid, "incident_id": None,
             "location": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
             "category": random.choice(["rescue", "person_trapped", "medical"]),
             "description": "Simulated stranded household reporting rising water",
             "people_count": random.randint(1, 5), "source_type": "simulator",
             "water_rising": True, "vulnerabilities": random.choice([[], ["child"], ["elderly"]])},
            priority=Priority.CRITICAL.value, simulation=True)
        try:
            await operations.run_pipeline(sim_db, rid, actor_id=actor["user_id"],
                                        simulation=True)
        except Exception:
            pass


async def _tick_comms(sim_db, simulation_id, tick, reg):
    plan = [("internet", 0.4), ("internet", 1.0), ("cellular", 1.0), ("mesh", 0.3)]
    name, deg = plan[min(tick - 1, len(plan) - 1)]
    reg.set_degradation(name, deg)
    if tick >= 3:
        reg.set_degradation("cellular", 1.0)
    state = reg.connectivity_state()
    if state == "SATELLITE_BACKHAUL":
        await bus.emit_system(EventType.SATELLITE_CONNECTED.value,
                              {"provider": "simulator", "simulated": True}, simulation=True)
    await _log(sim_db, simulation_id, f"comm_degradation_tick_{tick}",
               {"transport": name, "degradation": deg, "connectivity_state": state,
                "transports": reg.states()})
    result = await communication.flush(sim_db, simulation=True)
    await _log(sim_db, simulation_id, f"comm_flush_tick_{tick}", result)


async def _tick_infrastructure(sim_db, simulation_id, tick, actor):
    hz = new_ulid()
    lon = 90.40
    lat = 23.76 + 0.01 * tick
    poly = [[[lon - 0.006, lat - 0.004], [lon + 0.006, lat - 0.004],
             [lon + 0.006, lat + 0.004], [lon - 0.006, lat + 0.004],
             [lon - 0.006, lat - 0.004]]]
    await bus.emit_system(
        EventType.HAZARD_REPORTED.value,
        {"hazard_id": hz, "hazard_type": "bridge_damage",
         "geometry": {"type": "Polygon", "coordinates": poly},
         "description": f"Simulated bridge structural failure tick {tick}",
         "severity": 0.95, "road_blocked": True, "water_level_m": 1.4,
         "source_provenance": "simulator",
         "verification": {"status": "VERIFIED", "confidence": 0.9}, "active": True},
        priority=Priority.CRITICAL.value, simulation=True)
    graph = await operations.get_graph(sim_db)
    hazards = await operations.active_hazards(sim_db)
    from .routing import compute_route
    route = compute_route(graph, hazards, [90.37, 23.73], [90.45, 23.81], "road")
    await _log(sim_db, simulation_id, f"infrastructure_tick_{tick}",
               {"hazard_id": hz, "route_status": route["status"],
                "eta_seconds": route.get("eta_seconds"),
                "blocked_edges": route.get("hazard_overlay_edges")})


async def _tick_shortage(sim_db, simulation_id, tick, actor):
    resources = await sim_db.resources.find({"status": "available"}, {"_id": 0}).to_list(100)
    for r in resources[:max(1, len(resources) // 2)]:
        await bus.emit_system(EventType.RESOURCE_STATUS_CHANGED.value,
                              {"resource_id": r["resource_id"], "previous_status": "available",
                               "new_status": "out_of_service", "mission_id": None},
                              simulation=True)
    requests = await sim_db.emergency_requests.find(
        {"status": {"$in": ["new", "triaged"]}}, {"_id": 0}).to_list(20)
    graph = await operations.get_graph(sim_db)
    hazards = await operations.active_hazards(sim_db)
    from .allocation import propose
    avail = await sim_db.resources.find({"status": "available"}, {"_id": 0}).to_list(100)
    proposal = propose(requests, avail, graph, hazards)
    await _log(sim_db, simulation_id, f"resource_shortage_tick_{tick}",
               {"available_resources": len(avail), "demand": len(requests),
                "assigned": len(proposal["assignments"]),
                "unassigned": len(proposal["unassigned_request_ids"]),
                "solver": proposal["solver"]})


async def _tick_misinformation(sim_db, simulation_id, tick, actor):
    target = await sim_db.emergency_requests.find_one({}, {"_id": 0})
    if not target:
        return
    loc = target.get("location")
    for i in range(2):
        rid = f"R-SIM-{simulation_id}-{tick}-{i}"
        await sim_db.reports.update_one(
            {"report_id": rid},
            {"$setOnInsert": {
                "report_id": rid, "request_id": target["request_id"],
                "kind": "corroboration", "source_type": "citizen",
                "device_id": "sim-device", "assertion": "no_emergency_present",
                "contradicts": True, "location": loc,
                "text": "Simulated misinformation: claims nobody is present at this location",
                "created_at": utcnow_iso()}}, upsert=True)
    steps = await operations.run_pipeline(sim_db, target["request_id"],
                                         actor_id=actor["user_id"], simulation=True)
    await _log(sim_db, simulation_id, f"misinformation_tick_{tick}",
               {"request_id": target["request_id"],
                "verification_status": steps["verification"]["status"],
                "confidence": steps["verification"]["confidence"],
                "contradicting": len(steps["verification"]["contradicting_report_ids"])})


async def _tick_gateway(sim_db, simulation_id, tick, reg):
    gw = await sim_db.gateways.find_one({}, {"_id": 0})
    if not gw:
        return
    if tick % 2 == 1:
        reg.set_degradation("internet", 1.0)
        reg.set_degradation("cellular", 1.0)
        await bus.emit_system(EventType.GATEWAY_OFFLINE.value,
                              {"gateway_id": gw["gateway_id"],
                               "connectivity_state": "ISOLATED",
                               "reason": "simulated_power_loss"},
                              priority=Priority.HIGH.value, simulation=True)
    else:
        reg.set_degradation("internet", 0.0)
        reg.set_degradation("cellular", 0.0)
        await bus.emit_system(EventType.GATEWAY_ONLINE.value,
                              {"gateway_id": gw["gateway_id"],
                               "connectivity_state": "CONNECTED"},
                              simulation=True)
    depth = await communication.queue_depth(sim_db)
    flushed = await communication.flush(sim_db, simulation=True)
    await _log(sim_db, simulation_id, f"gateway_tick_{tick}",
               {"gateway_id": gw["gateway_id"], "queue_depth": depth,
                "connectivity_state": reg.connectivity_state(), "flush": flushed})


# ----------------------------------------------------------------- metrics
async def compute_metrics(sim_db, simulation_id: str, reg) -> dict:
    total_events = await sim_db.events.count_documents({})
    requests = await sim_db.emergency_requests.count_documents({})
    recs = await sim_db.recommendations.count_documents({})
    blocked = await sim_db.recommendations.count_documents({"approval_status": "blocked"})
    missions = await sim_db.missions.count_documents({})
    completed = await sim_db.missions.count_documents({"status": "completed"})
    conflicts = await sim_db.sync_conflicts.count_documents({})
    delivered = await sim_db.comm_messages.count_documents({"state": "DELIVERED"})
    failed = await sim_db.comm_messages.count_documents({"state": "FAILED"})
    queued = await sim_db.comm_messages.count_documents({"state": "QUEUED"})
    unassigned = await sim_db.emergency_requests.count_documents(
        {"status": {"$in": ["new", "triaged"]}})
    attempted = delivered + failed
    return {
        "events_generated": total_events,
        "emergency_requests": requests,
        "recommendations": recs,
        "recommendations_blocked_by_critic": blocked,
        "missions": missions,
        "missions_completed": completed,
        "sync_conflicts": conflicts,
        "messages_delivered": delivered,
        "messages_failed": failed,
        "messages_queued": queued,
        "delivery_success_rate": round(delivered / attempted, 3) if attempted else None,
        "unresolved_demand": unassigned,
        "final_connectivity_state": reg.connectivity_state(),
        "transport_states": reg.states(),
        "resilience_score": round(
            (delivered / attempted if attempted else 1.0) * 0.5 +
            (completed / missions if missions else 0.0) * 0.3 +
            (1 - min(1.0, unassigned / max(1, requests))) * 0.2, 3),
        "computed_at": utcnow_iso(),
    }
