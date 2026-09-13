# CROS Flood-Response MVP Acceptance Report

Date: 2026-09-13
Branch: `v0/crisis-response-os-96e14845`

## Verdict

**MVP REMEDIATION IN PROGRESS**

The duplicate-merge root cause is remediated: deduplication now requires spatial-temporal proximity plus semantic agreement. A fresh PostgreSQL acceptance request completed verification, prioritization, routing, allocation, recommendation, IC approval, and mission creation. The PostgreSQL simulator now executes real prioritization, allocation, and routing services per isolated tick; explicit A–E connectivity harnesses and PostGIS remain open validation work.

## Validation performed

- Deterministic service suite: **9 passed**.
- Full backend suite: **54 passed, 7 warnings** on the final rerun; the earlier mesh-relay failure was transient queue contamination and did not reproduce.
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
| Routing | PASS | Fresh request produced a deterministic `DEGRADED_ROUTE` with graph ID, ETA, hazard exposure, and route provenance. |
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
| A Normal connectivity | BLOCKED | No named A–E scenario exists; nearest `communication_degradation` run completed isolated ticks only. |
| B Degraded connectivity | PARTIAL | `communication_degradation` completed, but PostgreSQL runner emitted only tick events and did not exercise communication flush/transport metrics. |
| C Internet unavailable | BLOCKED | No explicit internet-unavailable scenario in the PostgreSQL simulator path. |
| D Internet + cellular unavailable, satellite gateway available | BLOCKED | No explicit satellite-gateway acceptance path; satellite is represented as simulated transport only. |
| E Fragmented network + gateway failure | PARTIAL/BLOCKED | `gateway_failure` completed isolated ticks, but PostgreSQL path did not execute queue drain, convergence, or delivery metrics. |

Machine-readable run data is in `docs/mvp-acceptance-results.json`.

## LLM failure result

**PASS for core fallback, NOT PASS for full acceptance.** The live request returned `provider: deterministic_fallback`, `model: rule_based_v1`, `fallback_used: true`, `error: LLM_PROVIDER_UNCONFIGURED`, and still produced verification and priority output. No fabricated LLM response was generated. Because the request was duplicate-merged, routing/allocation/mission continuation under LLM failure was not exercised in this run.

## Security and integrity result

**PASS for existing covered controls; incomplete for the requested matrix.** The complete suite passed signed envelope validation, modified-payload rejection/quarantine behavior, RBAC denials, authorization checks, duplicate sync behavior, and audit endpoint coverage. The requested explicit matrix for modified event ID, origin identity, HLC, replay, revoked/rotated keys, malicious geometry, oversized input, and prompt injection was not independently executed as a complete acceptance campaign.

## Final capability matrix

| Capability | Status |
|---|---|
| Core domain | B — implemented and tested for request/pipeline slices |
| Geospatial | C — routing exists; PostGIS geometry/GiST remains external |
| Prioritization | A — deterministic v2 with factor breakdown |
| Allocation | B — implemented, not reached in the acceptance request |
| Communication fabric | B — transport policy and mesh tests pass; A–E metrics incomplete |
| Offline edge | B — signed sync and local edge components exist; live zero-connectivity scenario incomplete |
| Mesh/gateway | B — focused relay test passes |
| Sync/convergence | B — focused convergence tests pass |
| AI agents | C — deterministic fallback exists; provider-outage full workflow not proven |
| HITL | B — approval gate exists; acceptance run produced no recommendation |
| Security | B — covered tests pass; requested complete adversarial matrix incomplete |
| Audit | B — audit mechanisms and tests exist; request-specific full lifecycle not proven |
| Simulation | B/C — PostgreSQL runs now execute real prioritization/allocation/routing per tick, but A–E connectivity metrics and end-to-end queue behavior remain incomplete |
| Role workflows | B — RBAC and role tests pass |
| Resilience behavior | C — partial simulator coverage; A–E operational metrics unavailable |

## PostGIS

**External prerequisite.** PostGIS is not enabled in the connected environment. No fake extension, geometry column, or GiST claim was made.

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
10. Enable PostGIS externally, then verify geometry columns, GiST indexes, and a real spatial query.
