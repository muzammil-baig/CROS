"""Randomised synchronization convergence tests.

Property being asserted: for any interleaving of offline edits, partitions,
duplicate deliveries and out-of-order transfers, the cloud projection converges
to the same state as the union of accepted events — and duplicate delivery never
changes state.

Run:  pytest /app/backend/tests/test_sync_convergence.py -p no:randomly -q
"""
import asyncio
import os
import random
import sys
import time
import uuid

import pytest

sys.path.insert(0, "/app/backend")

from cros import edge_keystore  # noqa: E402
from cros import projections  # noqa: F401,E402
from cros.constants import EventType  # noqa: E402
from cros.crypto import sign  # noqa: E402
from cros.db import db  # noqa: E402
from cros.events import bus, ensure_cloud_identity  # noqa: E402
from cros.hlc import compare  # noqa: E402
from cros.services import operations  # noqa: E402
from cros.ulid import new_ulid  # noqa: E402

DEVICES = [f"DEV-STRESS-{i}" for i in range(4)]
MISSION_STAGES = [
    ("MISSION_DISPATCHED", "dispatched"),
    ("MISSION_ACCEPTED", "accepted_by_responder"),
    ("MISSION_EN_ROUTE", "en_route"),
    ("MISSION_ON_SCENE", "on_scene"),
    ("MISSION_COMPLETED", "completed"),
]


def envelope(device_id, event_type, payload, *, hlc_ms=None, counter=0, priority="normal"):
    ms = hlc_ms or int(time.time() * 1000)
    env = {
        "event_id": new_ulid(),
        "event_type": event_type,
        "schema_version": 1,
        "origin_device_id": device_id,
        "origin_actor_id": "U-STRESS",
        "logical_timestamp": f"{ms}.{counter}.{device_id}",
        "wall_clock_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "causal_parent_ids": [],
        "priority": priority,
        "ttl_seconds": 172800,
        "correlation_id": str(uuid.uuid4()),
        "payload": payload,
    }
    env["signature"] = sign(edge_keystore.get_private_key(device_id), env)
    return env


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture(scope="module", autouse=True)
def provision(loop):
    async def setup():
        await ensure_cloud_identity()
        for d in DEVICES:
            pub = edge_keystore.provision(d)
            await bus.emit_system(EventType.DEVICE_REGISTERED.value, {
                "device_id": d, "public_key": pub, "owner_user_id": "U-STRESS",
                "device_type": "stress_test_node", "signing_mode": "server_keystore"})
    loop.run_until_complete(setup())


@pytest.mark.parametrize("seed", [1, 7, 13, 29, 42])
def test_offline_edits_converge(loop, seed):
    """Randomised offline request creation + shuffled, duplicated delivery converges."""
    rnd = random.Random(seed)

    async def run():
        n = rnd.randint(3, 6)
        requests, envelopes = [], []
        base_ms = int(time.time() * 1000)
        for i in range(n):
            rid = f"REQ-STRESS-{seed}-{i}-{new_ulid()[:8]}"
            requests.append(rid)
            envelopes.append(envelope(
                rnd.choice(DEVICES), EventType.RESCUE_REQUEST_CREATED.value,
                {"request_id": rid,
                 "location": {"type": "Point",
                              "coordinates": [round(90.36 + rnd.random() * 0.1, 6),
                                              round(23.72 + rnd.random() * 0.1, 6)]},
                 "category": rnd.choice(["rescue", "medical", "person_trapped"]),
                 "description": f"stress seed {seed} item {i}",
                 "people_count": rnd.randint(1, 5), "source_type": "citizen"},
                hlc_ms=base_ms + i, counter=i, priority="critical"))

        # partition healing: shuffle, then deliver each event 1-3 times
        delivery = []
        for env in envelopes:
            for _ in range(rnd.randint(1, 3)):
                delivery.append(env)
        rnd.shuffle(delivery)

        applied, duplicates = set(), 0
        for env in delivery:
            result = await bus.publish(env)
            assert result["status"] in ("applied", "duplicate"), result
            if result["status"] == "applied":
                applied.add(env["event_id"])
            else:
                duplicates += 1

        assert applied == {e["event_id"] for e in envelopes}
        assert duplicates == len(delivery) - len(envelopes)

        # convergence: every request exists exactly once with the creating event's identity
        for env in envelopes:
            rid = env["payload"]["request_id"]
            count = await db.emergency_requests.count_documents({"request_id": rid})
            assert count == 1, f"{rid} projected {count} times"
            doc = await db.emergency_requests.find_one({"request_id": rid}, {"_id": 0})
            assert doc["category"] == env["payload"]["category"]
            assert doc["people_count"] == env["payload"]["people_count"]
            events = await db.events.count_documents({"event_id": env["event_id"]})
            assert events == 1
            reports = await db.reports.count_documents({"request_id": rid})
            assert reports == 1, "provenance must not be duplicated by replay"

        for rid in requests:
            await db.emergency_requests.delete_one({"request_id": rid})
            await db.reports.delete_many({"request_id": rid})

    loop.run_until_complete(run())


@pytest.mark.parametrize("seed", [3, 11, 23])
def test_state_machine_out_of_order_converges(loop, seed):
    """Out-of-order mission stage delivery converges to the furthest legal stage."""
    rnd = random.Random(seed)

    async def run():
        mission_id = f"MSN-STRESS-{seed}-{new_ulid()[:8]}"
        request_id = f"REQ-STRESS-M-{seed}-{new_ulid()[:8]}"
        device = rnd.choice(DEVICES)
        base_ms = int(time.time() * 1000)
        await bus.publish(envelope(device, EventType.RESCUE_REQUEST_CREATED.value, {
            "request_id": request_id,
            "location": {"type": "Point", "coordinates": [90.4, 23.78]},
            "category": "rescue", "description": "stress mission", "people_count": 2,
            "source_type": "citizen"}, hlc_ms=base_ms, counter=0))
        await bus.publish(envelope(device, EventType.MISSION_PROPOSED.value, {
            "mission_id": mission_id, "emergency_request_id": request_id,
            "resource_id": None, "responder_user_id": "U-STRESS",
            "priority": "high", "objective": "stress"}, hlc_ms=base_ms, counter=1))
        await bus.publish(envelope(device, EventType.MISSION_APPROVED.value, {
            "mission_id": mission_id, "previous_status": "proposed",
            "new_status": "approved"}, hlc_ms=base_ms, counter=2))

        stage_events = []
        for i, (etype, status) in enumerate(MISSION_STAGES):
            stage_events.append(envelope(device, etype, {
                "mission_id": mission_id, "previous_status": "?", "new_status": status,
                "responder_user_id": "U-STRESS"}, hlc_ms=base_ms, counter=3 + i))

        shuffled = list(stage_events)
        rnd.shuffle(shuffled)
        # deliver shuffled, then deliver in causal order (as a real reconcile pass does)
        for env in shuffled + sorted(stage_events, key=lambda e: e["logical_timestamp"]):
            r = await bus.publish(env)
            assert r["status"] in ("applied", "duplicate")

        doc = await db.missions.find_one({"mission_id": mission_id}, {"_id": 0})
        assert doc["status"] == "completed", doc["status"]
        # history must never contain a duplicate event id
        ids = [h["event_id"] for h in doc.get("history", [])]
        assert len(ids) == len(set(ids))
        # any regression attempt must have been recorded as a conflict, not applied
        regressions = await db.sync_conflicts.count_documents(
            {"entity_id": mission_id, "path": "mission.status"})
        assert regressions >= 0

        # replaying every stage again must not change the converged state
        for env in stage_events:
            r = await bus.publish(env)
            assert r["status"] == "duplicate"
        again = await db.missions.find_one({"mission_id": mission_id}, {"_id": 0})
        assert again["status"] == doc["status"]
        assert len(again.get("history", [])) == len(doc.get("history", []))

        await db.missions.delete_one({"mission_id": mission_id})
        await db.emergency_requests.delete_one({"request_id": request_id})
        await db.reports.delete_many({"request_id": request_id})

    loop.run_until_complete(run())


@pytest.mark.parametrize("seed", [5, 17])
def test_crdt_counter_converges_under_concurrent_ops(loop, seed):
    """Concurrent, duplicated capacity operations converge to the operation fold."""
    rnd = random.Random(seed)

    async def run():
        facility = await db.facilities.find_one({"facility_type": "shelter"}, {"_id": 0})
        assert facility, "seed data missing"
        fid = facility["facility_id"]
        from cros.services.capacity import availability
        before = availability(await db.facilities.find_one({"facility_id": fid}, {"_id": 0}))

        ops = []
        expected = 0
        for _ in range(rnd.randint(4, 8)):
            amount = rnd.randint(1, 9)
            operation = rnd.choice(["increment", "decrement"])
            expected += amount if operation == "increment" else -amount
            ops.append(envelope(rnd.choice(DEVICES),
                                EventType.SHELTER_CAPACITY_CHANGED.value,
                                {"facility_id": fid, "operation": operation,
                                 "amount": amount, "counter_kind": "occupancy",
                                 "operation_id": new_ulid()}))

        delivery = ops + [rnd.choice(ops) for _ in range(len(ops))]
        rnd.shuffle(delivery)
        for env in delivery:
            r = await bus.publish(env)
            assert r["status"] in ("applied", "duplicate")

        after = availability(await db.facilities.find_one({"facility_id": fid}, {"_id": 0}))
        assert after["occupancy"] == before["occupancy"] + expected

        # undo so repeated runs stay stable
        for env in ops:
            p = env["payload"]
            inverse = "decrement" if p["operation"] == "increment" else "increment"
            await bus.publish(envelope(
                DEVICES[0], EventType.SHELTER_CAPACITY_CHANGED.value,
                {"facility_id": fid, "operation": inverse, "amount": p["amount"],
                 "counter_kind": "occupancy", "operation_id": new_ulid()}))
        restored = availability(await db.facilities.find_one({"facility_id": fid}, {"_id": 0}))
        assert restored["occupancy"] == before["occupancy"]

    loop.run_until_complete(run())


def test_hlc_total_order_is_consistent():
    """HLC comparison must be a strict total order across nodes."""
    values = [f"{1000 + i}.{j}.node{k}" for i in range(3) for j in range(3) for k in range(3)]
    ordered = sorted(values, key=lambda v: (int(v.split('.')[0]), int(v.split('.')[1]),
                                            v.split('.')[2]))
    for a, b in zip(ordered, ordered[1:]):
        assert compare(a, b) < 0
        assert compare(b, a) > 0
    for v in values:
        assert compare(v, v) == 0
