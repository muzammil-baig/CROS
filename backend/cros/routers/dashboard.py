"""Aggregate command-center view + realtime websocket."""
import json

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from ..auth import decode_token, require
from ..db import db
from ..models import utcnow_iso
from ..realtime import manager
from ..services import capacity
from ..services.communication import queue_depth
from ..services.transport import registry
from .deps import scope_request, scope_responder, stale_flag

router = APIRouter(tags=["command-center"])


@router.get("/command-center")
async def command_center(user: dict = Depends(require("incident:read"))):
    incidents = await db.incidents.find({}, {"_id": 0}).sort("created_at", -1).to_list(20)
    requests = await db.emergency_requests.find(
        {"status": {"$nin": ["cancelled", "duplicate_merged"]}}, {"_id": 0}).sort(
        [("priority.score", -1)]).to_list(200)
    hazards = await db.hazards.find({"active": True}, {"_id": 0}).to_list(200)
    missions = await db.missions.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    resources = await db.resources.find({}, {"_id": 0}).to_list(200)
    responders = await db.responders.find({}, {"_id": 0}).to_list(200)
    facilities = await db.facilities.find({}, {"_id": 0}).to_list(100)
    gateways = await db.gateways.find({}, {"_id": 0}).to_list(50)
    recommendations = await db.recommendations.find({}, {"_id": 0}).sort(
        "created_at", -1).to_list(60)
    conflicts = await db.sync_conflicts.find({}, {"_id": 0}).sort(
        "created_at", -1).to_list(50)
    sync_nodes = await db.sync_state.find({}, {"_id": 0}).to_list(50)

    pending = [r for r in recommendations
               if r.get("approval_status") in ("pending_approval", "blocked")]
    counts_by_tier = {}
    for r in requests:
        t = (r.get("priority") or {}).get("tier", "normal")
        counts_by_tier[t] = counts_by_tier.get(t, 0) + 1
    verification_counts = {}
    for r in requests:
        v = (r.get("verification") or {}).get("status", "UNVERIFIED")
        verification_counts[v] = verification_counts.get(v, 0) + 1

    return {
        "generated_at": utcnow_iso(),
        "connectivity_state": registry.connectivity_state(),
        "transports": registry.states(),
        "queue": await queue_depth(db),
        "incidents": incidents,
        "requests": [{**scope_request(r, user), "stale": stale_flag(r.get("updated_at"))}
                     for r in requests],
        "hazards": hazards,
        "missions": missions,
        "resources": resources,
        "responders": [scope_responder(r, user) for r in responders],
        "facilities": [{**f, "availability": capacity.availability(f)} for f in facilities],
        "gateways": gateways,
        "recommendations": recommendations,
        "approval_queue": pending,
        "sync": {"nodes": sync_nodes, "conflicts": conflicts,
                 "pending_conflicts": len([c for c in conflicts
                                           if c.get("status") == "pending_review"])},
        "stats": {
            "open_requests": len([r for r in requests
                                  if r.get("status") in ("new", "triaged")]),
            "requests_by_tier": counts_by_tier,
            "verification_counts": verification_counts,
            "active_missions": len([m for m in missions if m["status"] in
                                    ("approved", "dispatched", "accepted_by_responder",
                                     "en_route", "on_scene")]),
            "completed_missions": len([m for m in missions if m["status"] == "completed"]),
            "available_resources": len([r for r in resources if r["status"] == "available"]),
            "pending_approvals": len(pending),
            "total_events": await db.events.count_documents({}),
            "quarantined_events": await db.quarantine_events.count_documents({}),
        },
    }


@router.get("/citizen/evacuation-info")
async def evacuation_info(lat: float = Query(...), lon: float = Query(...)):
    """Public-safety information available to any authenticated citizen role."""
    shelters = await db.facilities.find({"facility_type": "shelter"}, {"_id": 0}).to_list(50)
    hazards = await db.hazards.find({"active": True},
                                     {"_id": 0, "geometry": 1, "severity": 1,
                                      "hazard_type": 1, "description": 1,
                                      "hazard_id": 1, "verification": 1}).to_list(100)
    from ..services.routing import haversine_m
    out = []
    for s in shelters:
        avail = capacity.availability(s)
        out.append({"facility_id": s["facility_id"], "name": s["name"],
                    "location": s["location"], "available": avail["available"],
                    "capacity_total": avail["capacity_total"],
                    "utilization_pct": avail["utilization_pct"],
                    "distance_m": round(haversine_m([lon, lat],
                                                     s["location"]["coordinates"]))})
    out.sort(key=lambda x: x["distance_m"])
    return {"shelters": out, "hazards": hazards, "generated_at": utcnow_iso()}


@router.websocket("/realtime")
async def realtime(ws: WebSocket):
    token = ws.query_params.get("token")
    since = ws.query_params.get("since_seq")
    try:
        if not token:
            await ws.close(code=4401)
            return
        payload = decode_token(token)
        if payload.get("type") != "access":
            await ws.close(code=4401)
            return
    except Exception:
        await ws.close(code=4401)
        return

    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps({"topic": "hello", "seq": manager.latest_seq,
                                       "data": {"role": payload.get("role"),
                                                "connectivity_state":
                                                    registry.connectivity_state()}}))
        if since is not None:
            try:
                for msg in manager.replay(int(since), {"*"}):
                    await ws.send_text(json.dumps(msg, default=str))
            except ValueError:
                pass
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "subscribe" in msg:
                manager.subscribe(ws, msg["subscribe"])
                await ws.send_text(json.dumps({"topic": "subscribed",
                                               "seq": manager.latest_seq,
                                               "data": msg["subscribe"]}))
            elif "replay_since" in msg:
                for m in manager.replay(int(msg["replay_since"]), {"*"}):
                    await ws.send_text(json.dumps(m, default=str))
            elif "ping" in msg:
                await ws.send_text(json.dumps({"topic": "pong",
                                               "seq": manager.latest_seq, "data": {}}))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
