# CROS Phase 2 Capability Matrix

Status: **A implemented**, **B partial**, **C missing**, **D blocked/external**, **E simulator-only**, **F not validated**.

| Area | Capability | Status | Repository evidence |
|---|---|---:|---|
| Domain | Incident, hazard, request, report/provenance | A | `backend/cros/routers/incidents.py`, `requests.py`; `events.py`, `projections.py` |
| Domain | Resources, responders, missions | A | `routers/resources.py`, `missions.py`, `services/operations.py` |
| Domain | Hospitals, shelters, capacity CRDT | B | `routers/resources.py`, `services/capacity.py`, `projections.py`; matching is deterministic but facility reservation is not atomic |
| Geospatial | PostGIS, geometry validation, spatial indexes | D | PostGIS is an external prerequisite; current runtime has no installed extension/verified GiST objects |
| Geospatial | OSM graph extraction | C | No production OSM ingestion module found |
| Geospatial | Routing, hazard overlays, freshness, NetworkX fallback | A | `services/routing.py`; versioned graph and degraded-route metadata |
| Decisions | Prioritization | A | `services/prioritization.py`; weighted v1 factors and verification multiplier |
| Decisions | Allocation / optimization | B | `services/allocation.py`; exact SciPy assignment boundary with deterministic fallback, not OR-Tools |
| Decisions | Communication selection | A | `services/transport.py`; deterministic score/ranking and ACK eligibility |
| Decisions | Medical/shelter matching | B | `routers/resources.py` match endpoints; capacity/route/specialty explanations, no reservation workflow |
| Events | Envelope, ULID, HLC, parents, signatures, schema/versioning | B | `events.py`, `ulid.py`, `crypto.py`, `edge_keystore.py`; broad contract coverage, external interoperability not proven |
| Events | Idempotency, projections, audit | A | `events.py`, `projections.py`, `audit.py`, `postgres.py` |
| Offline | Edge SQLite, queues, store-and-forward, mesh relay, reconnect sync | B | `edge/gateway.py`, `routers/comm.py`, `services/sync.py`; acceptance relay baseline passes, full field network not validated |
| Fabric | Internet, cellular, local Wi-Fi, Bluetooth, mesh | B | `services/transport.py`; abstraction/simulator links, real provider adapters not present |
| Fabric | SMS, radio, satellite | E | `services/transport.py`; explicitly simulator-backed |
| Security | Identity, device keys, RBAC, scoped access, revocation, audit | B | `auth.py`, `routers/auth_router.py`, `routers/deps.py`, `audit.py`; offline token and full RLS evidence incomplete |
| AI | Orchestrator, verification, critic, structured bounded outputs | B | `services/agents.py`, `services/verification.py`, `services/operations.py`; deterministic fallback exists, live provider evaluation absent |
| HITL | Evidence, confidence, alternatives, risk, approval/rejection/override | A | `services/operations.py`, `routers/recommendations.py`, `routers/missions.py`; immutable decision audit path |
| Simulation | Scenario catalog, isolated storage, failure injection, resilience metrics | B | `services/simulation.py`, `routers/simulation.py`; scenarios exist, complete Scenario D report not independently recorded |
| UI | Citizen, responder, IC, medical, shelter, comms, gateway, analyst, agency, admin | B | Frontend role workflows plus backend route permissions; end-to-end role usability audit remains |
| Testing | Unit, contract, integration, sync convergence, security, simulation | B | `backend/tests/*.py`; fixed deterministic vectors added; AI evaluation and hardware/field tests remain |

## Phase 2 changes

- Added fixed-vector tests for prioritization, hazard-overlay routing, infeasible allocation, transport ranking, and medical allocation suitability.
- Tightened facility matching input bounds.
- Preserved the explicit PostGIS external prerequisite; no fake enablement or geometry index claim is made.

## External prerequisite

A Supabase project owner must enable the `postgis` extension, then create and verify production geometry columns and GiST indexes and run a real spatial query. Until that happens, routing remains the documented NetworkX fallback.

## Top ten MVP gaps

1. PostGIS enablement and verified spatial schema/indexes.
2. Production OSM graph ingestion and refresh pipeline.
3. Atomic facility reservation/release events.
4. OR-Tools deployment or formally approved equivalent.
5. Real Bluetooth/Wi-Fi/mesh adapter validation on field hardware.
6. Offline permission-token issuance, expiry, and revocation proof.
7. Scenario D recorded resilience report and thresholds.
8. AI provider contract/evaluation suite with outage fixtures.
9. Full role-by-role UI acceptance coverage.
10. Hardware/field transport and disaster-network interoperability tests.

## Recommended next phase

Enable and validate PostGIS first, then implement OSM graph ingestion and facility reservation events. In parallel, add field transport adapters and a recorded Scenario D acceptance harness; keep deterministic routing/allocation and visible degraded AI behavior as the safety baseline.
