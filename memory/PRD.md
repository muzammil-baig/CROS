# CROS — Crisis Response OS · Product Requirements & Implementation Record

## 1. Original problem statement (verbatim intent)
Implement the attached CROS specification set (`01_PROJECT_REQUIREMENTS_DOCUMENT.md`,
`02_SYSTEM_ARCHITECTURE_CONSOLIDATED.md`, `03_DATABASE_DESIGN.md`,
`04_API_SPECIFICATION.md`, `05_EVENT_SCHEMAS.md`) as a **real, working, testable
application** — a resilient, offline-first disaster coordination platform for flood
response. Not a mockup: real frontend, backend, database, events, state transitions,
resilience mechanisms, AI workflows, simulation and security boundaries. Deterministic
algorithms where deterministic; bounded LLM agents elsewhere; human approval for
high-risk actions; faithful simulators (clearly labelled) where hardware/providers are
unavailable.

## 2. User-approved choices (intake)
- MongoDB + in-database append-only event bus instead of PostgreSQL/PostGIS + NATS JetStream.
- First slice: the full end-to-end flood demo flow.
- AI layer: Claude Sonnet 4.6 via the Emergent LLM key.
- Auth: JWT email/password with seeded accounts for all 10 roles.
- Rich seed data.

## 3. Declared architecture substitutions (documented discrepancies)
| Spec | Implemented | Reason |
|---|---|---|
| PostgreSQL + PostGIS | MongoDB (GeoJSON + 2dsphere) + Shapely predicates | Postgres/PostGIS not available in runtime |
| NATS JetStream | Append-only `events` collection + in-process consumer registry (at-least-once, idempotent by `event_id`) | NATS not available |
| OR-Tools | `scipy.optimize.linear_sum_assignment` (exact assignment) + deterministic greedy fallback | OR-Tools not installable; same service boundary, still no LLM |
| SQLite + SpatiaLite at edge | SQLite edge node; geometry as GeoJSON evaluated with Shapely | SpatiaLite binaries unavailable |
| Device-held private keys (secure element) | Filesystem keystore outside the database; browser clients use server-delegated signing labelled `SIMULATED_SIGNER` | No hardware secure element / inconsistent WebCrypto Ed25519 support |
| Satellite / radio / SMS transports | Real transport abstraction + provider-neutral simulator adapters, `simulated=true` and `SIMULATED` in UI | No provider/hardware |

## 4. Personas / roles (all backend-enforced, not UI hiding)
citizen · field_responder · incident_commander · medical_coordinator ·
shelter_coordinator · communications_operator · field_gateway_operator ·
government_officer · analyst_planner · system_administrator.
Platform administration deliberately grants **no** operational authority
(admin cannot approve recommendations or read the command-center aggregate).

## 5. Core requirements (static)
Observe → ingest → world state → analyze → predict → generate options → verify/critique
→ recommend → human approval → execute → observe. Offline-first field operation, real
synchronization with HLC causality and conflict classes, transport abstraction with
degraded-state visibility, immutable audit, simulation isolated from production.

## 6. Implemented — 2026-06 (verified)
**Platform**: FastAPI modular backend (`/app/backend/cros/**`), React command app,
MongoDB (production DB) + physically separate simulation DB, `/api/v1` versioned API,
spec error envelope (`code/message/details/request_id`), OpenTelemetry-style spans and
counters, WebSocket realtime with replay buffer + reconnect.

**Events**: ULID event ids, HLC logical clocks, origin device/actor, schema version,
causal parents, priority, TTL, Ed25519 signatures, quarantine of malformed/invalid
events, duplicate `event_id` = safe no-op, idempotent projections, per-event audit.

**Domain**: incidents, emergency requests (with append-only provenance reports, never
deleted on dedupe), hazards behind a pluggable `HazardModule` (`FloodModule` numerical
model + prediction), resources, responders, hospitals/shelters, gateways.

**Deterministic intelligence**: explicit weighted prioritization with factor breakdown;
corroboration/contradiction/freshness verification; NetworkX Dijkstra routing over a
persistent base road graph with a *separate* dynamic hazard overlay (base graph never
rewritten) and exposed graph freshness; exact-assignment resource allocation with
availability/capacity/suitability/travel-time/route-feasibility/priority constraints;
deterministic transport scoring policy.

**AI (bounded)**: situation synthesis, verification reasoning, safety critic — schema
validated structured output only, untrusted text delimited and never in system prompts,
no tools/SQL/shell, deterministic guardrail critic always runs first, deterministic
fallbacks + `AI_FALLBACK_ACTIVATED` events. LLMs never mutate state.

**HITL**: recommendations persist agent, evidence refs, evidence summary, confidence,
alternatives, risk tier, critic verdict, model/trace metadata; approvals are insert-only
and immutable; second decision → `409 ALREADY_DECIDED`; critic-blocked actions need an
explicit emergency override; missions born from recommendations cannot be approved
through the mission endpoint (`409 APPROVAL_REQUIRED`).

**Mission state machine**: proposed → approved/rejected → dispatched →
accepted/declined → en_route → on_scene → completed/failed, explicit command endpoints
only, invalid transitions rejected, ownership enforced, every transition emits an event
and audit record.

**Offline & sync**: browser local event log, local projections, outbound queue and local
HLC; `/sync/device` and `/gateways/{id}/sync` preserve event identity, dedupe, apply in
causal order, priority-aware partial transfer (`more_available`), conflict field classes
(state machine / CRDT counter / HLC LWW / append-only) with recorded conflicts and human
resolution.

**Communication**: 8 transports (internet, cellular, sms, local wifi, bluetooth, mesh,
radio, satellite) on one contract with capability values; priority-aware queue with real
delivery/ack/failure events; connectivity states CONNECTED/DEGRADED/LOCAL_ONLY/
MESH_ONLY/SATELLITE_BACKHAUL/ISOLATED derived from actual transport state.

**Edge runtime**: SQLite edge node per gateway with local event log, projections,
outbound/inbound queues, device registry cache, sync metadata, health stats.

**Capacity**: operation-based CRDT counters for hospitals and shelters (idempotent by
`operation_id`, value = fold of operations).

**Simulation**: 6 scenarios driving the real services; defence-in-depth isolation
(separate database, simulation event namespace, dedicated transport registry,
`assert_isolated()` guard); resilience metrics.

**Security/privacy**: JWT + refresh + offline permission token, bcrypt, brute-force
lockout, rate limiting, RBAC permission matrix, device registration/revocation with
signature rejection afterwards, role-scoped PII redaction and location coarsening on the
backend, append-only audit.

**Frontend**: tactical SVG map (hazards, requests, assets, facilities, gateways, routes,
road graph with blocked edges), command center with 10 tabs, citizen one-tap emergency
app with real offline queueing, responder mission app with offline-capable stage
commands, medical/shelter capacity consoles, comms + gateway consoles, analyst
resilience console, admin console. Explicit stale/offline/degraded/verification badges;
simulator-backed capabilities labelled SIMULATED.

**Test status**: backend suite 20/20 (`/app/backend/tests/backend_test.py`); frontend
deep E2E 12/13 in iteration 2, remaining failures (admin RBAC 403, mobile overflow) fixed
and re-verified by the main agent.

## 7. Backlog (prioritised)
**P0** — none blocking.
**P1**
- Re-run the full frontend E2E suite after the admin/mobile fixes.
- Property-based tests for sync convergence; replay/version-compatibility event tests.
- Inbound-queue-driven mesh relay between two edge nodes (currently gateway↔cloud only).
**P2**
- Real WebCrypto Ed25519 client signing path (removing `SIMULATED_SIGNER` for browsers).
- Real satellite provider adapter behind the existing boundary.
- Docker Compose dev environment; PostGIS/NATS adapters if the runtime allows.
- Additional hazard modules (cyclone, earthquake) to prove the module boundary.
- Richer hospital specialty matching and multi-casualty transport planning.

## 8. Next tasks
1. Frontend regression pass on /admin and mobile breakpoints.
2. Sync property tests + mesh multi-node relay.
3. Client-side Ed25519 signing.
