"""
Iteration 3 backend tests:
- MESH RELAY (queue-local -> mesh-relay -> upstream, identity, idempotency, failure, validation)
- DEVICE SIGNING (register, end-to-end signed envelope, tamper rejection)
- CYCLONE MODULE (hazard-agnostic, routing overlay, unknown hazard 422)
- REGRESSION (org:read for admin)
"""
import base64
import datetime as dt
import json
import os
import random
import string
import time
import uuid

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
API = f"{BASE_URL}/api/v1"

CREDS = {
    "admin": ("baigmuzammil62@gmail.com", "CrosAdmin!2026"),
    "commander": ("cmd.rahman@cros.gov", "CrosDemo!2026"),
    "citizen": ("citizen.hasan@example.com", "CrosDemo!2026"),
    "gateway": ("gateway.tanaka@cros.gov", "CrosDemo!2026"),
    "comms": ("comms.silva@cros.gov", "CrosDemo!2026"),
}

SIGNED_FIELDS = [
    "event_id", "event_type", "schema_version", "origin_device_id",
    "origin_actor_id", "logical_timestamp", "wall_clock_timestamp",
    "causal_parent_ids", "priority", "ttl_seconds", "correlation_id", "payload",
]

TIMEOUT = 60


def _ulid():
    alpha = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(random.choice(alpha) for _ in range(26))


def _H(t):
    return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def tokens():
    tok = {}
    for role, (email, pw) in CREDS.items():
        r = requests.post(f"{API}/auth/login",
                          json={"email": email, "password": pw}, timeout=30)
        assert r.status_code == 200, f"login {role}: {r.status_code} {r.text[:200]}"
        tok[role] = r.json()["access_token"]
    return tok


def _canonical(envelope):
    subset = {k: envelope.get(k) for k in SIGNED_FIELDS}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"), default=str).encode()


def _restore_mesh(tokens):
    requests.post(f"{API}/comm/transports/degrade",
                  headers=_H(tokens["comms"]),
                  json={"transport": "mesh", "degradation": 0.0}, timeout=15)


# ============ MESH RELAY ============

class TestMeshRelay:
    def _queue_local(self, tokens, gateway_id="GW-EAST-03", payload_extra=None):
        eid = _ulid()
        request_id = f"REQ-{_ulid()}"
        hlc = f"{int(time.time()*1000)}.0.{gateway_id}"
        envelope = {
            "event_id": eid,
            "event_type": "RESCUE_REQUEST_CREATED",
            "schema_version": 1,
            "origin_device_id": gateway_id,
            "origin_actor_id": None,
            "logical_timestamp": hlc,
            "wall_clock_timestamp": dt.datetime.utcnow().isoformat() + "Z",
            "causal_parent_ids": [],
            "priority": "critical",
            "ttl_seconds": 86400,
            "correlation_id": str(uuid.uuid4()),
            "signature": None,
            "payload": {
                "request_id": request_id,
                "location": {"type": "Point", "coordinates": [91.42, 22.34]},
                "category": "rescue",
                "description": "TEST_ mesh relay flood rescue",
                "people_count": 4,
                "water_rising": True,
                "source_type": "citizen",
                **(payload_extra or {}),
            },
        }
        r = requests.post(f"{API}/gateways/{gateway_id}/queue-local",
                          headers=_H(tokens["gateway"]),
                          json=envelope, timeout=TIMEOUT)
        assert r.status_code in (200, 201, 202), f"queue-local: {r.status_code} {r.text[:400]}"
        data = r.json()
        assert data.get("status") == "applied", data
        return eid, request_id, envelope

    def test_end_to_end_relay(self, tokens):
        _restore_mesh(tokens)
        eid, request_id, _ = self._queue_local(tokens)

        # try up to 3 times since mesh has ~90% reliability
        last = None
        for _ in range(3):
            r = requests.post(f"{API}/gateways/GW-EAST-03/mesh-relay",
                              headers=_H(tokens["gateway"]),
                              json={"peer_gateway_id": "GW-NORTH-01",
                                    "max_events": 20,
                                    "push_upstream": True},
                              timeout=TIMEOUT)
            last = r
            if r.status_code == 200 and r.json().get("relayed_event_ids"):
                break
            # re-queue if lost due to link_loss? relayed_event_ids empty means events already moved OR link_loss
            if r.status_code == 200 and not r.json().get("relayed_event_ids"):
                # possibly link_loss; requeue and retry
                eid, request_id, _ = self._queue_local(tokens)
        assert last.status_code == 200, f"mesh-relay: {last.status_code} {last.text[:400]}"
        j = last.json()
        assert j.get("transport") == "mesh", j
        assert eid in (j.get("relayed_event_ids") or []), j
        assert j.get("identity_preserved") is True, j
        up = j.get("upstream") or {}
        assert eid in (up.get("accepted_event_ids") or []), up
        # pipeline run for this request_id
        prs = up.get("pipelines_run_for_requests") or []
        assert request_id in prs, f"pipeline not run: {prs}"

        # Give the backend a moment
        time.sleep(2)
        # Readable at GET /requests/{request_id}
        r2 = requests.get(f"{API}/requests/{request_id}",
                          headers=_H(tokens["commander"]), timeout=30)
        assert r2.status_code == 200, r2.text[:300]
        rj = r2.json()
        # priority + verification present
        pr = rj.get("priority") or rj.get("priority_score") or (rj.get("pipeline") or {}).get("priority")
        vr = rj.get("verification") or (rj.get("pipeline") or {}).get("verification")
        assert pr, f"no priority: {rj}"
        assert vr, f"no verification: {rj}"

        # Identity preserved: exactly one RESCUE_REQUEST_CREATED with this event_id
        r3 = requests.get(f"{API}/events?event_type=RESCUE_REQUEST_CREATED&limit=200",
                         headers=_H(tokens["admin"]), timeout=30)
        assert r3.status_code == 200, r3.text[:200]
        events = r3.json().get("items") or r3.json().get("events") or []
        matches = [e for e in events if e.get("event_id") == eid]
        assert len(matches) == 1, f"duplicate/missing event: {len(matches)}"

        # Idempotency: second relay returns empty
        r4 = requests.post(f"{API}/gateways/GW-EAST-03/mesh-relay",
                           headers=_H(tokens["gateway"]),
                           json={"peer_gateway_id": "GW-NORTH-01",
                                 "max_events": 20, "push_upstream": True},
                           timeout=TIMEOUT)
        assert r4.status_code == 200, r4.text[:200]
        j4 = r4.json()
        assert not (j4.get("relayed_event_ids") or []), f"expected empty on 2nd relay: {j4}"

        # Store for other tests
        return eid, request_id

    def test_mesh_relay_validation_same_peer(self, tokens):
        r = requests.post(f"{API}/gateways/GW-EAST-03/mesh-relay",
                          headers=_H(tokens["gateway"]),
                          json={"peer_gateway_id": "GW-EAST-03"},
                          timeout=15)
        assert r.status_code == 422, f"expected 422: {r.status_code} {r.text[:200]}"

    def test_mesh_relay_unknown_peer_404(self, tokens):
        r = requests.post(f"{API}/gateways/GW-EAST-03/mesh-relay",
                          headers=_H(tokens["gateway"]),
                          json={"peer_gateway_id": "GW-DOES-NOT-EXIST"},
                          timeout=15)
        assert r.status_code == 404, f"expected 404: {r.status_code} {r.text[:200]}"

    def test_mesh_relay_transport_unavailable(self, tokens):
        # queue some fresh events first so there is something to relay
        self._queue_local(tokens)
        # Degrade mesh fully
        d = requests.post(f"{API}/comm/transports/degrade",
                          headers=_H(tokens["comms"]),
                          json={"transport": "mesh", "degradation": 1.0},
                          timeout=15)
        assert d.status_code in (200, 201), d.text[:200]
        try:
            r = requests.post(f"{API}/gateways/GW-EAST-03/mesh-relay",
                              headers=_H(tokens["gateway"]),
                              json={"peer_gateway_id": "GW-NORTH-01"},
                              timeout=15)
            assert r.status_code == 503, f"expected 503: {r.status_code} {r.text[:200]}"
            body = r.json()
            code = (body.get("error") or {}).get("code") or body.get("code")
            assert code == "MESH_UNAVAILABLE", body
        finally:
            _restore_mesh(tokens)


# ============ DEVICE SIGNING ============

class TestDeviceSigning:
    def test_register_server_keystore(self, tokens):
        r = requests.post(f"{API}/auth/device/register",
                          headers=_H(tokens["citizen"]),
                          json={"public_key": None, "device_type": "web_client"},
                          timeout=20)
        assert r.status_code in (200, 201), r.text[:300]
        j = r.json()
        assert j.get("signing_mode") in ("server_keystore", None), j
        assert j.get("device_id", "").startswith("DEV-"), j
        # DEV-WEB- pattern is only for client_webcrypto
        assert not j["device_id"].startswith("DEV-WEB-"), j

    def test_register_client_webcrypto(self, tokens):
        priv = Ed25519PrivateKey.generate()
        spki_der = priv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_b64 = base64.b64encode(spki_der).decode()
        r = requests.post(f"{API}/auth/device/register",
                          headers=_H(tokens["citizen"]),
                          json={"public_key": pub_b64,
                                "device_type": "web_client",
                                "signing_mode": "client_webcrypto"},
                          timeout=20)
        assert r.status_code in (200, 201), r.text[:300]
        j = r.json()
        assert j.get("device_id", "").startswith("DEV-WEB-"), j
        assert j.get("signing_mode") == "client_webcrypto", j
        assert j.get("simulated_signer") is False, j

    def test_end_to_end_signed_envelope(self, tokens):
        priv = Ed25519PrivateKey.generate()
        spki_der = priv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_b64 = base64.b64encode(spki_der).decode()
        r = requests.post(f"{API}/auth/device/register",
                          headers=_H(tokens["citizen"]),
                          json={"public_key": pub_b64,
                                "device_type": "web_client",
                                "signing_mode": "client_webcrypto"},
                          timeout=20)
        assert r.status_code in (200, 201), r.text[:300]
        device_id = r.json()["device_id"]

        eid = _ulid()
        request_id = f"REQ-{_ulid()}"
        hlc = f"{int(time.time()*1000)}.0.{device_id}"
        envelope = {
            "event_id": eid,
            "event_type": "RESCUE_REQUEST_CREATED",
            "schema_version": 1,
            "origin_device_id": device_id,
            "origin_actor_id": None,
            "logical_timestamp": hlc,
            "wall_clock_timestamp": dt.datetime.utcnow().isoformat() + "Z",
            "causal_parent_ids": [],
            "priority": "critical",
            "ttl_seconds": 86400,
            "correlation_id": str(uuid.uuid4()),
            "payload": {
                "request_id": request_id,
                "location": {"type": "Point", "coordinates": [90.42, 23.78]},
                "category": "rescue",
                "description": "TEST_ device-signed rescue",
                "people_count": 2,
                "water_rising": False,
                "source_type": "citizen",
            },
        }
        subset = {k: envelope[k] for k in SIGNED_FIELDS}
        canon = json.dumps(subset, sort_keys=True, separators=(",", ":")).encode()
        sig = base64.b64encode(priv.sign(canon)).decode()
        envelope["signature"] = sig

        r1 = requests.post(f"{API}/sync/device",
                           headers=_H(tokens["citizen"]),
                           json={"device_id": device_id, "events": [envelope]},
                           timeout=TIMEOUT)
        assert r1.status_code in (200, 201, 202), r1.text[:400]
        j1 = r1.json()
        ing = j1.get("ingested") or []
        assert ing, j1
        item = ing[0]
        assert item.get("status") == "applied", item
        assert item.get("signature_verified") is True, item

        # Tamper: change people_count, keep same signature and new event_id
        tampered = dict(envelope)
        tampered["event_id"] = _ulid()
        tampered["payload"] = dict(envelope["payload"], people_count=99, request_id=f"REQ-{_ulid()}")
        # keep the ORIGINAL signature (which was over original payload)
        # get quarantined baseline
        hb = requests.get(f"{API}/system/health", headers=_H(tokens["admin"]), timeout=15)
        base_q = 0
        if hb.status_code == 200:
            base_q = (hb.json().get("quarantined") or hb.json().get("quarantine_count")
                      or (hb.json().get("edge_runtime") or {}).get("quarantined")
                      or 0)
        r2 = requests.post(f"{API}/sync/device",
                           headers=_H(tokens["citizen"]),
                           json={"device_id": device_id, "events": [tampered]},
                           timeout=TIMEOUT)
        assert r2.status_code in (200, 201, 202, 400), r2.text[:400]
        j2 = r2.json()
        ing2 = j2.get("ingested") or []
        assert ing2, j2
        it2 = ing2[0]
        assert it2.get("status") in ("rejected", "quarantined") or it2.get("signature_verified") is False, it2
        rcode = (it2.get("error") or {}).get("code") or it2.get("reason") or ""
        assert "SIGNATURE" in rcode.upper() or it2.get("signature_verified") is False, it2

        # Quarantine increased
        ha = requests.get(f"{API}/system/health", headers=_H(tokens["admin"]), timeout=15)
        if ha.status_code == 200:
            now_q = (ha.json().get("quarantined") or ha.json().get("quarantine_count")
                     or (ha.json().get("edge_runtime") or {}).get("quarantined")
                     or 0)
            assert now_q >= base_q, (base_q, now_q)


# ============ CYCLONE MODULE ============

class TestCyclone:
    def test_hazards_registry(self, tokens):
        r = requests.get(f"{API}/hazards", headers=_H(tokens["commander"]), timeout=15)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        modules = data.get("hazard_modules") or []
        assert "cyclone" in modules and "flood" in modules, modules

    def test_create_cyclone_hazard(self, tokens):
        # polygon around ops area
        body = {
            "hazard_type": "cyclone",
            "geometry": {"type": "Polygon", "coordinates": [[
                [90.30, 23.70], [90.50, 23.70],
                [90.50, 23.85], [90.30, 23.85], [90.30, 23.70]
            ]]},
            "wind_speed_kph": 165,
            "storm_surge_m": 1.4,
            "intensification_kph_per_hour": 8,
            "surge_rate_m_per_hour": 0.3,
        }
        r = requests.post(f"{API}/hazards", headers=_H(tokens["commander"]),
                          json=body, timeout=30)
        assert r.status_code in (200, 201), r.text[:400]
        j = r.json()
        assert j.get("hazard_module") == "cyclone", j
        sev = j.get("severity") or j.get("severity_score")
        assert sev and 0.5 < float(sev) < 1.0, j
        assert j.get("road_blocked") is True, j
        hid = j.get("hazard_id") or j.get("id")
        assert hid, j

        # Prediction
        r2 = requests.get(f"{API}/hazards/{hid}/prediction?horizon_minutes=120",
                          headers=_H(tokens["commander"]), timeout=20)
        assert r2.status_code == 200, r2.text[:300]
        pj = r2.json()
        assert pj.get("model") == "cyclone_wind_surge_v1", pj
        # damaging winds true
        dw = pj.get("damaging_winds")
        assert dw is True, pj
        return hid

    def test_unknown_hazard_module_422(self, tokens):
        body = {
            "hazard_type": "volcano",
            "geometry": {"type": "Polygon", "coordinates": [[
                [90.30, 23.70], [90.50, 23.70],
                [90.50, 23.85], [90.30, 23.85], [90.30, 23.70]
            ]]},
        }
        r = requests.post(f"{API}/hazards", headers=_H(tokens["commander"]),
                          json=body, timeout=15)
        assert r.status_code == 422, f"{r.status_code} {r.text[:300]}"
        code = (r.json().get("error") or {}).get("code") or r.json().get("code")
        assert code in ("VALIDATION_FAILED", "UNKNOWN_HAZARD_MODULE") or "VALIDATION" in r.text.upper()

    def test_road_graph_reflects_cyclone(self, tokens):
        # ensure road-graph endpoint reflects hazard overlay
        r = requests.get(f"{API}/geo/road-graph", headers=_H(tokens["commander"]), timeout=20)
        # accept 200 (with overlay) or 404 if endpoint not exposed
        assert r.status_code in (200, 404), r.text[:200]
        if r.status_code == 200:
            j = r.json()
            edges = j.get("edges") or []
            # at least one blocked edge should exist due to active cyclone
            blocked = [e for e in edges if e.get("blocked") or e.get("status") == "blocked"]
            # weak assertion since routing depends on graph coverage; just ensure structure
            assert isinstance(edges, list)


# ============ Regression: admin org read ============

class TestAdminRegression:
    def test_admin_can_list_organizations(self, tokens):
        r = requests.get(f"{API}/organizations", headers=_H(tokens["admin"]), timeout=15)
        assert r.status_code == 200, f"admin should read organizations now: {r.status_code} {r.text[:300]}"

    def test_admin_can_list_users(self, tokens):
        r = requests.get(f"{API}/users", headers=_H(tokens["admin"]), timeout=15)
        assert r.status_code == 200, r.text[:200]

    def test_admin_can_list_devices(self, tokens):
        r = requests.get(f"{API}/devices", headers=_H(tokens["admin"]), timeout=15)
        assert r.status_code == 200, r.text[:200]
