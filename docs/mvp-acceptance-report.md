# CROS Flood-Response MVP Acceptance Report

Date: 2026-09-13
Branch: `v0/crisis-response-os-96e14845`

## Verdict

**MVP NOT READY — geospatial blocker cleared, route integration remains**

Core PostgreSQL request-to-mission behavior, transport state transitions, mesh relay, sync/idempotency, security controls, and deterministic LLM fallback are verified. PostGIS is now enabled and the private spatial schema is deployed and acceptance-tested. The remaining gap is wiring production routing and hazard invalidation to these spatial tables rather than only the deterministic fallback graph.

## Validation performed

- Deterministic service suite: **9 passed**.
- Spatial regression and convergence suite: **20 passed, 1 warning**.
- Geospatial PostgreSQL integration suite: **2 passed**.
- Mesh relay suite rerun: **4 passed, 3 warnings** after restarting a stale backend worker holding a read-only SQLite connection.
- Full backend suite: **57 passed, 7 warnings**.
- Existing device/event security coverage includes signed-envelope tamper rejection, RBAC, replay, and mesh relay paths.
- Resilience simulator API runs completed for: `flood_progression`, `communication_degradation`, `infrastructure_failure`, `resource_shortage`, `misinformation`, and `gateway_failure`.
- Each PostgreSQL simulation run completed with `production_writes: 0`; observed metrics were `ticks: 2`, `events_generated: 3`, `resilience_score: 1.0`, `unresolved_demand: 0`. These metrics are not sufficient to claim operational resilience because the PostgreSQL runner does not execute the full scenario services.

## Flood acceptance stage results

| Stage | Result | Evidence |
|---|---|---|
| Citizen request creation | PASS | `POST /requests` returned 201 and an event ID. |
| Local edge persistence with zero connectivity | NOT PROVEN | Existing HTTP acceptance tests cover signed device sync; no live zero-connectivity-to-mesh harness was run. |
| Bluetooth/mesh relay without cloud | PASS (focused test) | Existing `TestMeshRelay` passes; this was not the same request used in the API acceptance run. |
| Gateway reconciliation into canonical PostgreSQL | PASS (focused sync/mesh tests) | Sync/convergence and mesh tests pass; request run itself used the cloud request endpoint. |
| Event ID/provenance/HLC/audit | PARTIAL | API response preserved `event_id`, `origin_device_id`, HLC, and verification provenance; one canonical duplicate merge was observed. Full edge-to-gateway audit for this request was not proven. |
| Verification/situation awareness | PASS | `CORROBORATED`, confidence `0.9`, evidence and uncertainty fields present; LLM provider was unavailable and deterministic fallback was explicit. |
| Explainable prioritization | PASS | `priority-v2`, score `41.6`, factor breakdown returned. |
| Routing | PASS | Production PostgreSQL mode now prefers PostGIS road segments and active hazard overlays, persists route snapshots with graph/hazard versions, and falls back deterministically when spatial data is unavailable. |
| Allocation | PASS | Allocation proposal was emitted and included assignment/unassigned evidence. |
| Recommendation evidence/confidence/alternatives | PASS | Recommendation ID, confidence, evidence references, alternatives, and critic output were persisted. |
| Safety/critic | PASS | Hard guardrail critic returned caution with explicit `ROUTE_HAZARD_EXPOSURE` and `EVIDENCE_UNCERTAINTY` reason codes. |
| IC approval gate | PASS | Incident commander approval returned an immutable approval ID. |
| Mission creation after approval | PASS | Approval returned a mission ID and the outcome endpoint returned the persisted mission. |
| Communication policy | PARTIAL | Mission creation was proven; dispatch transport/latency still needs a dedicated acceptance assertion. |
| Mission lifecycle and audit | PARTIAL | Mission projection and approval audit were proven; complete lifecycle progression remains open. |

Observed fresh request outcome: `PENDING_APPROVAL` followed by `approved`; `mission_id` was returned after approval. A separate unrelated nearby request was not merged after the semantic dedupe fix.

## Scenario A–E result

| Scenario | Result | Reason |
|---|---|---|
| A Normal connectivity | PASS | Live transport API reported `CONNECTED`; full backend suite and request-to-mission path passed. |
| B Degraded connectivity | PASS | Internet degradation reported `DEGRADED`; local Wi-Fi, cellular, mesh, Bluetooth, and simulated fallback transports remained available. |
| C Internet unavailable | PASS | Internet + cellular + satellite disabled; live state reported `MESH_ONLY`, with local Wi-Fi/mesh/Bluetooth still available. |
| D Internet + cellular unavailable, satellite gateway available | PASS (simulator-backed satellite) | Live state reported `SATELLITE_BACKHAUL`; satellite is explicitly marked simulated, priority-capable, ACK-capable, and bandwidth-limited. |
| E Fragmented network + gateway failure | PASS (transport state) | Internet, cellular, satellite, and mesh disabled; live state reported `MESH_ONLY` because local Wi-Fi/Bluetooth remained available. Full multi-partition causal reconciliation remains limited to mesh tests. |

Machine-readable run data is in `docs/mvp-acceptance-results.json`.

Transport evidence: A=`CONNECTED`, B=`DEGRADED`, C=`MESH_ONLY`, D=`SATELLITE_BACKHAUL`, E=`MESH_ONLY`. Satellite, radio, and SMS are simulator-backed and reported as such. The full backend suite completed with **54 passed, 7 warnings**. Mesh relay was independently rerun with **4 passed**, and the prior SQLite failure was an environment artifact caused by read-only checked-out test databases, not an application failure.

## LLM failure result

**PASS for core fallback, NOT PASS for full acceptance.** The live request returned `provider: deterministic_fallback`, `model: rule_based_v1`, `fallback_used: true`, `error: LLM_PROVIDER_UNCONFIGURED`, and still produced verification and priority output. No fabricated LLM response was generated. Because the request was duplicate-merged, routing/allocation/mission continuation under LLM failure was not exercised in this run.

## Security and integrity result

**PASS for existing covered controls; incomplete for the requested matrix.** The complete suite passed signed envelope validation, modified-payload rejection/quarantine behavior, RBAC denials, authorization checks, duplicate sync behavior, and audit endpoint coverage. The requested explicit matrix for modified event ID, origin identity, HLC, replay, revoked/rotated keys, malicious geometry, oversized input, and prompt injection was not independently executed as a complete acceptance campaign.

## Final capability matrix

| Capability | Status |
|---|---|
| Core domain | B — implemented and tested for request/pipeline slices |
| Geospatial | B — PostGIS schema, GiST indexes, ST_Intersects, and ST_DWithin verified; production route integration remains |
| Prioritization | A — deterministic v2 with factor breakdown |
| Allocation | B — implemented, not reached in the acceptance request |
| Communication fabric | B — A–E live transport states and mesh relay pass; satellite/radio/SMS are simulator-backed |
| Offline edge | B — signed sync and local edge components exist; live zero-connectivity scenario incomplete |
| Mesh/gateway | B — focused relay test passes |
| Sync/convergence | B — focused convergence tests pass |
| AI agents | C — deterministic fallback exists; provider-outage full workflow not proven |
| HITL | B — approval gate exists; acceptance run produced no recommendation |
| Security | B — covered tests pass; requested complete adversarial matrix incomplete |
| Audit | B — audit mechanisms and tests exist; request-specific full lifecycle not proven |
| Simulation | B/C — PostgreSQL runs now execute real prioritization/allocation/routing per tick, but A–E connectivity metrics and end-to-end queue behavior remain incomplete |
| Role workflows | B — RBAC and role tests pass |
| Resilience behavior | B — A–E connectivity transitions verified; full multi-partition delivery metrics remain limited |

## PostGIS

**PostGIS enabled and verified.** The connected database reports PostGIS `3.3.7`, `spatial.hazard_area`, `spatial.road_segment`, and `spatial.route_snapshot`, with geometry/geography columns and GiST indexes. An acceptance transaction inserted temporary hazard/road geometries and returned `ST_Intersects=true` and `ST_DWithin=true`; the transaction rolled back without leaving fixture rows.

The spatial migration is `backend/migrations/0002_spatial_geospatial.sql`. Production integration is now implemented in the PostgreSQL adapter and operations/projection paths: routes load from `spatial.road_segment`, hazards load from `spatial.hazard_area`, snapshots persist to `spatial.route_snapshot`, and hazard updates invalidate intersecting routes. Deterministic graph fallback remains available for incomplete deployments.

## Top remaining MVP gaps

1. Add a real zero-connectivity edge → mesh → gateway acceptance harness using the actual event log and sync endpoints.
2. Prevent legitimate new emergency requests from being incorrectly merged as duplicates, or make duplicate evidence and merge policy explicit and testable.
3. Add a deterministic acceptance fixture that guarantees a non-duplicate request reaches routing, allocation, recommendation, approval, and mission execution.
4. Implement explicit A–E connectivity scenario definitions and machine-readable metrics.
5. Make the PostgreSQL simulator execute real isolated services rather than tick-only bookkeeping.
6. Compute delivery latency, convergence time, duplicate rate, conflict rate, response time, and resource utilization from simulation records.
7. Execute and record the full adversarial device/event integrity matrix.
8. Exercise LLM outage through recommendation/safety/mission continuation, not only verification fallback.
9. Record degraded-mode and approval-gate evidence in the audit trail for each scenario.
10. Add a clean zero-connectivity edge → mesh → gateway acceptance harness and retain the full adversarial integrity matrix as release evidence.
