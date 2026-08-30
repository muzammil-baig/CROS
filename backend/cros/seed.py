"""Realistic development seed data. Idempotent."""
import asyncio
import random
from datetime import datetime, timedelta, timezone

from . import edge_keystore
from .auth import hash_password
from .config import ADMIN_EMAIL, ADMIN_PASSWORD, SEED_PASSWORD
from .constants import EventType, Priority, Role
from .db import db
from .events import bus
from .models import utcnow_iso
from .services import capacity, operations
from .services.routing import build_base_graph
from .ulid import new_ulid

ORG = {"org_id": "ORG-NDRA", "name": "National Disaster Response Agency", "kind": "government"}
ORG2 = {"org_id": "ORG-CITYFIRE", "name": "Metro City Fire & Rescue", "kind": "responder"}
ORG3 = {"org_id": "ORG-MEDNET", "name": "Regional Health Network", "kind": "medical"}

USERS = [
    ("cmd.rahman@cros.gov", "Ayesha Rahman", Role.INCIDENT_COMMANDER, "ORG-NDRA"),
    ("med.okafor@cros.gov", "Dr. Chidi Okafor", Role.MEDICAL_COORDINATOR, "ORG-MEDNET"),
    ("shelter.nadeem@cros.gov", "Farah Nadeem", Role.SHELTER_COORDINATOR, "ORG-NDRA"),
    ("comms.silva@cros.gov", "Marcos Silva", Role.COMMUNICATIONS_OPERATOR, "ORG-NDRA"),
    ("gateway.tanaka@cros.gov", "Kenji Tanaka", Role.FIELD_GATEWAY_OPERATOR, "ORG-NDRA"),
    ("officer.mbeki@cros.gov", "Naledi Mbeki", Role.GOVERNMENT_OFFICER, "ORG-NDRA"),
    ("analyst.novak@cros.gov", "Petra Novak", Role.ANALYST_PLANNER, "ORG-NDRA"),
    ("citizen.hasan@example.com", "Imran Hasan", Role.CITIZEN, None),
]

RESPONDERS = [
    ("resp.khan@cros.gov", "Bilal Khan", "boat", "BOAT-01"),
    ("resp.mensah@cros.gov", "Akua Mensah", "boat", "BOAT-02"),
    ("resp.torres@cros.gov", "Luis Torres", "ambulance", "AMB-01"),
    ("resp.iqbal@cros.gov", "Sana Iqbal", "team", "TEAM-01"),
    ("resp.dube@cros.gov", "Thabo Dube", "helicopter", "HELI-01"),
]

AREA_LON = (90.36, 90.46)
AREA_Lat = (23.72, 23.82)


def pt(lon, lat):
    return {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}


def box(lon, lat, w, h):
    return {"type": "Polygon", "coordinates": [[
        [round(lon - w, 6), round(lat - h, 6)], [round(lon + w, 6), round(lat - h, 6)],
        [round(lon + w, 6), round(lat + h, 6)], [round(lon - w, 6), round(lat + h, 6)],
        [round(lon - w, 6), round(lat - h, 6)]]]}


async def _user(email, name, role, org_id, password):
    existing = await db.users.find_one({"email": email})
    user_id = existing["user_id"] if existing else f"U-{new_ulid()}"
    await db.users.update_one(
        {"email": email},
        {"$set": {"user_id": user_id, "email": email, "name": name, "role": role.value,
                  "org_id": org_id, "disabled": False,
                  "password_hash": hash_password(password)},
         "$setOnInsert": {"created_at": utcnow_iso()}}, upsert=True)
    device_id = f"DEV-{user_id}"
    if not edge_keystore.has_key(device_id) or \
            not await db.devices.find_one({"device_id": device_id}):
        pub = edge_keystore.provision(device_id)
        await bus.emit_system(EventType.DEVICE_REGISTERED.value,
                              {"device_id": device_id, "public_key": pub,
                               "owner_user_id": user_id, "device_type": "seed_device",
                               "signing_mode": "server_keystore",
                               "trust_level": "trusted"})
    return user_id, device_id


async def seed() -> dict:
    if await db.seed_state.find_one({"key": "seeded_v1"}):
        return {"seeded": False, "reason": "already_seeded"}

    for org in (ORG, ORG2, ORG3):
        await db.organizations.update_one({"org_id": org["org_id"]},
                                         {"$set": org}, upsert=True)

    admin_id, admin_dev = await _user(ADMIN_EMAIL, "Muzammil Baig",
                                      Role.SYSTEM_ADMINISTRATOR, "ORG-NDRA", ADMIN_PASSWORD)
    ids = {}
    for email, name, role, org in USERS:
        uid, dev = await _user(email, name, role, org, SEED_PASSWORD)
        ids[role.value] = {"user_id": uid, "device_id": dev, "email": email}
    responder_ids = []
    for email, name, kind, label in RESPONDERS:
        uid, dev = await _user(email, name, Role.FIELD_RESPONDER, "ORG-CITYFIRE", SEED_PASSWORD)
        responder_ids.append({"user_id": uid, "device_id": dev, "email": email,
                              "name": name, "kind": kind, "label": label})

    commander = ids[Role.INCIDENT_COMMANDER.value]["user_id"]

    # ---- base road graph
    if not await db.road_graph.find_one({"graph_id": "base-road-graph-v1"}):
        await db.road_graph.insert_one(build_base_graph())

    # ---- incidents
    inc1, inc2 = f"INC-{new_ulid()}", f"INC-{new_ulid()}"
    await bus.emit_system(EventType.INCIDENT_CREATED.value, {
        "incident_id": inc1, "name": "Buriganga Delta Flood — Sector North",
        "hazard_type": "flood", "status": "active", "severity": "severe",
        "commander_user_id": commander, "location": pt(90.40, 23.79),
        "area": box(90.40, 23.79, 0.03, 0.02)}, priority=Priority.HIGH.value)
    await bus.emit_system(EventType.INCIDENT_CREATED.value, {
        "incident_id": inc2, "name": "Buriganga Delta Flood — Sector South",
        "hazard_type": "flood", "status": "active", "severity": "moderate",
        "commander_user_id": commander, "location": pt(90.40, 23.74),
        "area": box(90.40, 23.74, 0.03, 0.02)})

    # ---- hazards / flood zones
    zones = [
        (inc1, box(90.395, 23.795, 0.018, 0.010), 0.85, 1.3, 0.22, True,
         "Riverbank overtopping, main road submerged"),
        (inc1, box(90.435, 23.785, 0.014, 0.008), 0.62, 0.7, 0.15, False,
         "Low-lying residential blocks flooding"),
        (inc2, box(90.378, 23.742, 0.016, 0.009), 0.48, 0.5, 0.10, False,
         "Standing water across market district"),
        (inc2, box(90.428, 23.735, 0.012, 0.007), 0.93, 1.6, 0.30, True,
         "Embankment breach, fast-moving water"),
    ]
    for incident_id, geom, sev, level, rate, blocked, desc in zones:
        await bus.emit_system(EventType.HAZARD_REPORTED.value, {
            "hazard_id": f"HZ-{new_ulid()}", "incident_id": incident_id,
            "hazard_type": "flood", "geometry": geom, "description": desc,
            "severity": sev, "water_level_m": level, "rise_rate_m_per_hour": rate,
            "rainfall_mm_per_hour": 18.0, "road_blocked": blocked,
            "source_provenance": "field_responder_survey",
            "verification": {"status": "VERIFIED", "confidence": 0.9}, "active": True},
            priority=Priority.HIGH.value)
    await bus.emit_system(EventType.HAZARD_REPORTED.value, {
        "hazard_id": f"HZ-{new_ulid()}", "incident_id": inc1, "hazard_type": "bridge_damage",
        "geometry": box(90.418, 23.762, 0.005, 0.003),
        "description": "North bridge deck damaged — closed to vehicles",
        "severity": 0.95, "water_level_m": 1.1, "road_blocked": True,
        "source_provenance": "government_inspection",
        "verification": {"status": "VERIFIED", "confidence": 0.95}, "active": True},
        priority=Priority.CRITICAL.value)

    # ---- facilities (hospitals + shelters) with CRDT counters
    facilities = [
        ("hospital", "Delta General Hospital", 90.372, 23.808, 240, 18,
         ["trauma", "surgery", "icu", "pediatrics"]),
        ("hospital", "Riverside Medical Centre", 90.443, 23.802, 120, 8,
         ["trauma", "dialysis", "obstetrics"]),
        ("hospital", "Southgate Community Hospital", 90.381, 23.727, 80, 4,
         ["general", "orthopedics"]),
        ("shelter", "Ward 12 Primary School Shelter", 90.404, 23.812, 450, 0,
         ["family", "accessible"]),
        ("shelter", "Central Stadium Shelter", 90.425, 23.748, 1200, 0,
         ["family", "livestock", "accessible"]),
        ("shelter", "Southgate Community Hall", 90.366, 23.733, 300, 0, ["family"]),
    ]
    facility_ids = []
    for ftype, name, lon, lat, total, icu, specialties in facilities:
        fid = f"FAC-{new_ulid()}"
        facility_ids.append((fid, ftype))
        await db.facilities.update_one({"facility_id": fid}, {"$set": {
            "facility_id": fid, "facility_type": ftype, "name": name,
            "location": pt(lon, lat), "capacity_total": total,
            "icu_capacity_total": icu, "specialties": specialties,
            "status": "operational", "counters": {}, "created_at": utcnow_iso()}}, upsert=True)
        if ftype == "hospital":
            await bus.emit_system(EventType.HOSPITAL_CAPACITY_CHANGED.value, {
                "facility_id": fid, "operation": "increment",
                "amount": int(total * random.uniform(0.55, 0.85)),
                "counter_kind": "occupied_beds", "operation_id": new_ulid()})
            if icu:
                await bus.emit_system(EventType.HOSPITAL_CAPACITY_CHANGED.value, {
                    "facility_id": fid, "operation": "increment",
                    "amount": max(1, int(icu * random.uniform(0.4, 0.9))),
                    "counter_kind": "occupied_icu_beds", "operation_id": new_ulid()})
        else:
            await bus.emit_system(EventType.SHELTER_CAPACITY_CHANGED.value, {
                "facility_id": fid, "operation": "increment",
                "amount": int(total * random.uniform(0.2, 0.75)),
                "counter_kind": "occupancy", "operation_id": new_ulid()})

    # ---- resources
    resources = []
    for r in responder_ids:
        rid = f"RES-{new_ulid()}"
        cap = {"boat": 8, "ambulance": 2, "team": 6, "helicopter": 4}[r["kind"]]
        resources.append({
            "resource_id": rid, "kind": r["kind"], "label": r["label"],
            "capacity": cap, "status": "available", "org_id": "ORG-CITYFIRE",
            "responder_user_id": r["user_id"], "responder_name": r["name"],
            "capabilities": {"boat": ["swiftwater", "rescue"],
                             "ambulance": ["als", "transport"],
                             "team": ["search", "extraction"],
                             "helicopter": ["hoist", "aerial"]}[r["kind"]],
            "location": pt(random.uniform(*AREA_LON), random.uniform(*AREA_Lat)),
            "created_at": utcnow_iso()})
    for kind, label, cap in [("boat", "BOAT-03", 10), ("boat", "BOAT-04", 6),
                             ("ambulance", "AMB-02", 2), ("ambulance", "AMB-03", 2),
                             ("truck", "TRK-01", 25), ("truck", "TRK-02", 25),
                             ("team", "TEAM-02", 6)]:
        resources.append({
            "resource_id": f"RES-{new_ulid()}", "kind": kind, "label": label,
            "capacity": cap, "status": "available", "org_id": "ORG-CITYFIRE",
            "responder_user_id": None, "responder_name": None,
            "capabilities": ["general"],
            "location": pt(random.uniform(*AREA_LON), random.uniform(*AREA_Lat)),
            "created_at": utcnow_iso()})
    for r in resources:
        await db.resources.update_one({"resource_id": r["resource_id"]},
                                      {"$set": r}, upsert=True)
    for r in responder_ids:
        await db.responders.update_one({"user_id": r["user_id"]}, {"$set": {
            "user_id": r["user_id"], "name": r["name"], "availability": "available",
            "skills": [r["kind"]], "org_id": "ORG-CITYFIRE",
            "location": pt(random.uniform(*AREA_LON), random.uniform(*AREA_Lat)),
            "last_location_at": utcnow_iso()}}, upsert=True)

    # ---- gateways
    gateways = [
        ("GW-NORTH-01", "North Embankment Gateway", 90.398, 23.799, "online", "CONNECTED"),
        ("GW-SOUTH-02", "Southgate School Gateway", 90.376, 23.736, "online",
         "SATELLITE_BACKHAUL"),
        ("GW-EAST-03", "East Market Gateway", 90.441, 23.771, "offline", "ISOLATED"),
    ]
    for gid, name, lon, lat, status, conn in gateways:
        await db.gateways.update_one({"gateway_id": gid}, {"$set": {
            "gateway_id": gid, "name": name, "location": pt(lon, lat),
            "status": status, "connectivity_state": conn, "battery_pct": random.randint(35, 100),
            "hardware": "Raspberry Pi 4B / SQLite edge node",
            "mesh_roster": [f"DEV-MESH-{i}" for i in range(1, random.randint(3, 7))],
            "operator_user_id": ids[Role.FIELD_GATEWAY_OPERATOR.value]["user_id"],
            "last_seen": utcnow_iso(), "created_at": utcnow_iso()}}, upsert=True)
        await bus.emit_system(
            EventType.GATEWAY_ONLINE.value if status == "online"
            else EventType.GATEWAY_OFFLINE.value,
            {"gateway_id": gid, "connectivity_state": conn})
    await bus.emit_system(EventType.SATELLITE_CONNECTED.value,
                          {"provider": "simulator", "simulated": True,
                           "gateway_id": "GW-SOUTH-02"})

    # ---- emergency requests (incl. conflicting reports)
    citizen = ids[Role.CITIZEN.value]
    request_specs = [
        ("rescue", "Family of five on rooftop, water at first floor", 90.396, 23.797, 5,
         ["child", "elderly"], True),
        ("person_trapped", "Elderly man trapped in ground-floor room", 90.399, 23.794, 1,
         ["elderly", "disabled"], True),
        ("medical", "Diabetic patient out of insulin for 20 hours", 90.437, 23.786, 1,
         ["injured"], False),
        ("rescue", "Twelve people stranded at bus depot", 90.430, 23.737, 12, [], True),
        ("medical", "Pregnant woman in labour, road submerged", 90.427, 23.733, 2,
         ["pregnant"], True),
        ("hazard", "Live power line down in flood water", 90.412, 23.768, 0, [], False),
        ("rescue", "Two fishermen clinging to embankment", 90.433, 23.734, 2, [], True),
        ("evacuation", "Ward 9 residents requesting evacuation transport", 90.380, 23.744, 40,
         [], False),
        ("person_trapped", "Child trapped in flooded stairwell", 90.397, 23.796, 1,
         ["child"], True),
        ("welfare_check", "No contact with grandmother since last night", 90.371, 23.730, 1,
         ["elderly"], False),
        ("medical", "Snake bite casualty needs antivenom", 90.445, 23.780, 1, ["injured"], False),
        ("rescue", "Three on roof of submerged rickshaw garage", 90.418, 23.755, 3, [], True),
        ("hazard", "Fuel slick spreading from ruptured tank", 90.404, 23.760, 0, [], False),
        ("evacuation", "School with 60 children needs relocation", 90.406, 23.810, 60,
         ["child"], False),
        ("rescue", "Man swept downstream near bridge pier", 90.419, 23.763, 1, ["injured"], True),
        ("safe_report", "Household confirms all members safe", 90.386, 23.752, 4, [], False),
    ]
    request_ids = []
    now = datetime.now(timezone.utc)
    for i, (cat, desc, lon, lat, people, vulns, rising) in enumerate(request_specs):
        rid = f"REQ-{new_ulid()}"
        request_ids.append(rid)
        env = bus.build_envelope(
            EventType.RESCUE_REQUEST_CREATED.value,
            {"request_id": rid, "incident_id": inc1 if lat > 23.76 else inc2,
             "location": pt(lon, lat), "category": cat, "description": desc,
             "people_count": people, "vulnerabilities": vulns, "water_rising": rising,
             "source_type": "citizen" if i % 3 else "field_responder",
             "reporter_name": "Imran Hasan" if i % 3 else "Field Team",
             "reporter_phone": "+8801700000%02d" % i},
            origin_device_id=citizen["device_id"], origin_actor_id=citizen["user_id"],
            priority=Priority.CRITICAL.value if cat in ("rescue", "person_trapped", "medical")
            else Priority.NORMAL.value)
        env["wall_clock_timestamp"] = (now - timedelta(minutes=(len(request_specs) - i) * 7)) \
            .isoformat()
        env["signature"] = edge_keystore.sign_envelope(citizen["device_id"], env)
        await bus.publish(env)

    # conflicting / corroborating provenance reports (append-only)
    for i, rid in enumerate(request_ids[:6]):
        req = await db.emergency_requests.find_one({"request_id": rid}, {"_id": 0}) or {}
        contradicts = i in (2, 5)
        report_id = f"R-SEED-{new_ulid()}"
        await db.reports.update_one({"report_id": report_id}, {"$setOnInsert": {
            "report_id": report_id, "request_id": rid, "kind": "corroboration",
            "source_type": "citizen" if contradicts else "field_responder",
            "device_id": citizen["device_id"],
            "assertion": "no_emergency_present" if contradicts else "confirms",
            "contradicts": contradicts,
            "text": ("Neighbour states the house was already evacuated yesterday"
                     if contradicts else
                     "Second caller confirms people visible on the roof"),
            "location": req.get("location"), "created_at": utcnow_iso()}}, upsert=True)

    # ---- a completed mission for history
    boat = next(r for r in resources if r["kind"] == "boat" and r["responder_user_id"])
    mission_id = f"MSN-{new_ulid()}"
    done_req = request_ids[-1]
    graph = await operations.get_graph(db)
    hazards = await operations.active_hazards(db)
    from .services.routing import compute_route
    route = compute_route(graph, hazards, boat["location"]["coordinates"],
                          [90.386, 23.752], "boat")
    base = {"mission_id": mission_id, "emergency_request_id": done_req,
            "incident_id": inc2, "resource_id": boat["resource_id"],
            "responder_user_id": boat["responder_user_id"], "priority": "high",
            "objective": "Extract household from flooded lane and transport to shelter",
            "route": route}
    await bus.emit_system(EventType.MISSION_PROPOSED.value, base,
                          origin_actor_id=commander)
    for ev, prev, nxt in [(EventType.MISSION_APPROVED, "proposed", "approved"),
                          (EventType.MISSION_DISPATCHED, "approved", "dispatched"),
                          (EventType.MISSION_ACCEPTED, "dispatched",
                           "accepted_by_responder"),
                          (EventType.MISSION_EN_ROUTE, "accepted_by_responder", "en_route"),
                          (EventType.MISSION_ON_SCENE, "en_route", "on_scene"),
                          (EventType.MISSION_COMPLETED, "on_scene", "completed")]:
        await bus.emit_system(ev.value, {
            "mission_id": mission_id, "emergency_request_id": done_req,
            "responder_user_id": boat["responder_user_id"],
            "previous_status": prev, "new_status": nxt, "reason": None,
            "outcome": {"people_rescued": 4} if nxt == "completed" else None},
            origin_actor_id=commander if prev in ("proposed", "approved") else
            boat["responder_user_id"])

    await db.seed_state.update_one({"key": "seeded_v1"},
                                   {"$set": {"key": "seeded_v1", "at": utcnow_iso()}},
                                   upsert=True)
    asyncio.create_task(_seed_pipelines(request_ids[:4], commander))
    return {"seeded": True, "requests": len(request_ids), "incidents": 2,
            "resources": len(resources), "facilities": len(facility_ids),
            "gateways": len(gateways)}


async def _seed_pipelines(request_ids, commander_id):
    """Run the real pipeline on a few requests so pending approvals exist."""
    await asyncio.sleep(2)
    for rid in request_ids:
        try:
            await operations.run_pipeline(db, rid, actor_id=commander_id)
        except Exception:
            pass


async def ensure_gateway_identities():
    """Gateways are edge signing nodes: provision keystore keys + register devices."""
    from . import edge_keystore
    from .constants import EventType
    from .events import bus
    gateways = await db.gateways.find({}, {"_id": 0, "gateway_id": 1}).to_list(200)
    for g in gateways:
        gid = g["gateway_id"]
        device = await db.devices.find_one({"device_id": gid})
        if device and edge_keystore.has_key(gid):
            continue
        pub = edge_keystore.provision(gid)
        await bus.emit_system(EventType.DEVICE_REGISTERED.value, {
            "device_id": gid, "public_key": pub, "owner_user_id": None,
            "device_type": "field_gateway", "signing_mode": "server_keystore",
            "trust_level": "trusted"})


async def credentials_markdown() -> str:
    users = await db.users.find({}, {"_id": 0, "email": 1, "role": 1, "name": 1}).to_list(100)
    lines = ["# CROS Test Credentials", "",
             f"Admin: `{ADMIN_EMAIL}` / `{ADMIN_PASSWORD}` (system_administrator)", "",
             f"All other seeded accounts use password: `{SEED_PASSWORD}`", "",
             "| Email | Name | Role |", "|---|---|---|"]
    for u in sorted(users, key=lambda x: x["role"]):
        lines.append(f"| {u['email']} | {u.get('name','')} | {u['role']} |")
    lines += ["", "## Auth endpoints", "- POST /api/v1/auth/login",
              "- POST /api/v1/auth/token/refresh", "- GET /api/v1/auth/me",
              "- POST /api/v1/auth/device/register", "- GET /api/v1/auth/credential-status"]
    return "\n".join(lines)
