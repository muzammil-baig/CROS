import React, { useEffect, useMemo, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, Stat, ago } from "../components/kit";
import { Shell } from "../components/Shell";
import TacticalMap from "../components/TacticalMap";
import { useLive } from "../context/LiveContext";
import { useAuth } from "../context/AuthContext";

const TABS = ["MAP", "QUEUE", "MISSIONS", "AI", "APPROVALS", "COMMS", "SYNC", "AUDIT", "SIM", "SYSTEM"];

export default function CommandCenter() {
  const { snapshot, refresh, connectivity, lastEvent } = useLive();
  const { can } = useAuth();
  const [tab, setTab] = useState("MAP");
  const [selected, setSelected] = useState(null);
  const [graph, setGraph] = useState(null);
  const [detail, setDetail] = useState(null);
  const [note, setNote] = useState(null);

  useEffect(() => {
    api.get("/geo/road-graph").then(({ data }) => setGraph(data)).catch(() => {});
  }, []);

  const s = snapshot;
  const routes = useMemo(
    () => (s?.missions || []).filter((m) => ["approved", "dispatched", "accepted_by_responder", "en_route"].includes(m.status)).map((m) => m.route).filter(Boolean),
    [s]
  );

  const openSelection = async (sel) => {
    setSelected(sel);
    setDetail(null);
    if (sel.type === "request") {
      try {
        const [{ data: ver }, { data: hist }] = await Promise.all([
          api.get(`/requests/${sel.id}/verification`),
          api.get(`/requests/${sel.id}/history`),
        ]);
        setDetail({ verification: ver, history: hist });
      } catch {
        /* detail is optional */
      }
    }
  };

  const act = async (fn, successMsg) => {
    try {
      await fn();
      setNote({ tone: "#00FF66", text: successMsg });
      await refresh();
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  };

  if (!s) return <Shell><Loader label="ACQUIRING OPERATIONAL PICTURE" /></Shell>;

  return (
    <Shell>
      <div className="h-full flex flex-col">
        <div className="flex items-center gap-1 px-3 py-2 border-b border-line bg-surface overflow-x-auto">
          {TABS.map((t) => (
            <button
              key={t}
              data-testid={`tab-${t.toLowerCase()}`}
              onClick={() => setTab(t)}
              className={`font-micro text-[10px] px-2.5 py-1 border ${
                tab === t
                  ? "bg-signal-cyan text-black border-signal-cyan"
                  : "text-neutral-400 border-line hover:text-white"
              }`}
            >
              {t}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-2">
            {lastEvent && (
              <span className="font-micro text-[9px] text-signal-green pulse-signal" data-testid="last-event">
                ◉ {lastEvent.data?.event_type || lastEvent.topic}
              </span>
            )}
            <Badge value={connectivity} />
          </div>
        </div>

        {note && (
          <div
            data-testid="cc-note"
            className="px-3 py-1.5 font-micro text-[10px] border-b border-line"
            style={{ color: note.tone }}
          >
            {note.text}
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-hidden">
          {tab === "MAP" && (
            <div className="h-full grid grid-cols-1 lg:grid-cols-[360px_1fr_360px]">
              <div className="border-r border-line overflow-y-auto">
                <div className="grid grid-cols-2 gap-2 p-2">
                  <Stat label="OPEN REQUESTS" value={s.stats.open_requests} tone="#FFB020" testid="stat-open" />
                  <Stat label="CRITICAL" value={s.stats.requests_by_tier?.critical || 0} tone="#FF3B30" testid="stat-critical" />
                  <Stat label="ACTIVE MISSIONS" value={s.stats.active_missions} tone="#00E5FF" testid="stat-missions" />
                  <Stat label="APPROVALS" value={s.stats.pending_approvals} tone="#FFB020" testid="stat-approvals" />
                </div>
                <Panel title="EMERGENCY QUEUE" testid="panel-queue">
                  {s.requests.slice(0, 40).map((r) => (
                    <Row
                      key={r.request_id}
                      testid={`queue-row-${r.request_id}`}
                      active={selected?.id === r.request_id}
                      onClick={() => openSelection({ type: "request", id: r.request_id, doc: r })}
                    >
                      <div className="flex items-center justify-between gap-1 flex-wrap">
                        <span className="font-micro text-[10px] text-white">
                          {r.category.toUpperCase()}
                        </span>
                        <div className="flex gap-1">
                          <Badge value={r.priority?.tier} label={`${r.priority?.tier}·${r.priority?.score}`} />
                          <Badge value={r.verification?.status} />
                          {r.stale && <Badge value="STALE" />}
                        </div>
                      </div>
                      <div className="font-micro text-[9px] text-neutral-500 truncate">
                        {r.description}
                      </div>
                      <div className="font-micro text-[9px] text-neutral-600">
                        {ago(r.created_at)} · {r.status?.toUpperCase()} · {r.location_precision}
                      </div>
                    </Row>
                  ))}
                  {!s.requests.length && <Empty label="QUEUE EMPTY" />}
                </Panel>
              </div>

              <div className="relative min-h-[300px]">
                <TacticalMap
                  hazards={s.hazards}
                  requests={s.requests}
                  resources={s.resources}
                  facilities={s.facilities}
                  gateways={s.gateways}
                  routes={routes}
                  roadGraph={graph}
                  onSelect={openSelection}
                  selectedId={selected?.id}
                />
                <div className="absolute top-3 right-3 panel-elevated px-3 py-2">
                  <div className="font-micro text-[9px] text-neutral-500">GRAPH FRESHNESS</div>
                  <div className="font-micro text-[10px] text-signal-cyan">
                    {graph ? `${graph.node_count} NODES · ${graph.blocked_edges} BLOCKED` : "—"}
                  </div>
                  <div className="font-micro text-[9px] text-neutral-600">
                    {graph ? ago(graph.graph_updated_at) : ""}
                  </div>
                </div>
              </div>

              <div className="border-l border-line overflow-y-auto">
                <Panel title="SELECTION" testid="panel-selection">
                  {!selected ? (
                    <Empty label="SELECT A MAP OBJECT" />
                  ) : (
                    <div className="p-3 space-y-2">
                      <div className="font-macro text-lg text-white">
                        {selected.type.toUpperCase()}
                      </div>
                      <div className="font-micro text-[9px] text-neutral-500 break-all">
                        {selected.id}
                      </div>
                      {Object.entries(flatten(selected.doc)).slice(0, 26).map(([k, v]) => (
                        <KV key={k} k={k} v={String(v).slice(0, 60)} />
                      ))}
                      {selected.type === "request" && detail?.verification && (
                        <div className="pt-2 border-t border-line">
                          <div className="font-micro text-[9px] text-neutral-500 mb-1">
                            PROVENANCE · {detail.verification.contributing_reports.length} REPORTS
                          </div>
                          {detail.verification.contributing_reports.map((rep) => (
                            <div key={rep.report_id} className="mb-1">
                              <Badge
                                value={rep.contradicts ? "CONTRADICTORY" : "CORROBORATED"}
                                label={rep.source_type}
                              />
                              <div className="font-micro text-[9px] text-neutral-500">
                                {rep.text}
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                      {selected.type === "request" && can("allocation:propose") && (
                        <Btn
                          variant="cyan"
                          testid="btn-propose-allocation"
                          onClick={() =>
                            act(
                              () => api.post("/allocations/propose", { request_ids: [selected.id] }),
                              "ALLOCATION PROPOSAL COMPUTED (OR-EQUIVALENT SOLVER)"
                            )
                          }
                        >
                          RUN ALLOCATION
                        </Btn>
                      )}
                    </div>
                  )}
                </Panel>
                <Panel title="COMMUNICATION HEALTH" testid="panel-comm-health">
                  <div className="p-3">
                    {s.transports.map((t) => (
                      <div key={t.name} className="flex items-center justify-between py-0.5">
                        <span className="font-micro text-[10px] text-neutral-300">
                          {t.name.toUpperCase()}
                        </span>
                        <div className="flex items-center gap-1">
                          {t.simulated && <Badge value="SIMULATED" label="SIM" />}
                          <Badge
                            value={t.available ? (t.reliability > 0.85 ? "CONNECTED" : "DEGRADED") : "ISOLATED"}
                            label={t.available ? `${Math.round(t.reliability * 100)}%` : "DOWN"}
                          />
                        </div>
                      </div>
                    ))}
                    <div className="mt-2 pt-2 border-t border-line">
                      <KV k="QUEUED" v={s.queue.QUEUED} />
                      <KV k="DELIVERED" v={s.queue.DELIVERED} />
                      <KV k="FAILED" v={s.queue.FAILED} tone="#FF3B30" />
                    </div>
                  </div>
                </Panel>
              </div>
            </div>
          )}

          {tab === "QUEUE" && (
            <div className="h-full overflow-y-auto p-3">
              <Panel title={`EMERGENCY REQUESTS · ${s.requests.length}`} testid="panel-queue-full">
                {s.requests.map((r) => (
                  <Row key={r.request_id} testid={`full-queue-${r.request_id}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge value={r.priority?.tier} label={`${r.priority?.tier} ${r.priority?.score}`} />
                      <Badge value={r.verification?.status} />
                      <span className="font-micro text-[10px] text-white">{r.category.toUpperCase()}</span>
                      <span className="font-micro text-[9px] text-neutral-500">{r.description}</span>
                      <span className="ml-auto font-micro text-[9px] text-neutral-600">
                        {ago(r.created_at)} · {r.status?.toUpperCase()}
                      </span>
                      {can("request:prioritize_override") && (
                        <Btn
                          testid={`btn-escalate-${r.request_id}`}
                          variant="amber"
                          onClick={() =>
                            act(
                              () =>
                                api.post(`/requests/${r.request_id}/priority/override`, {
                                  tier: "critical",
                                  reason: "Commander escalation from queue review",
                                }),
                              `PRIORITY OVERRIDDEN → CRITICAL (${r.request_id})`
                            )
                          }
                        >
                          ESCALATE
                        </Btn>
                      )}
                    </div>
                    <div className="font-micro text-[9px] text-neutral-600 mt-1">
                      FACTORS {JSON.stringify(r.priority?.factors || {})}
                    </div>
                  </Row>
                ))}
              </Panel>
            </div>
          )}

          {tab === "MISSIONS" && (
            <div className="h-full overflow-y-auto p-3 space-y-3">
              {["proposed", "approved", "dispatched", "accepted_by_responder", "en_route", "on_scene", "completed", "failed", "rejected", "declined"].map((st) => {
                const items = s.missions.filter((m) => m.status === st);
                if (!items.length) return null;
                return (
                  <Panel key={st} title={`${st.toUpperCase()} · ${items.length}`} testid={`missions-${st}`}>
                    {items.map((m) => (
                      <Row key={m.mission_id} testid={`mission-${m.mission_id}`}>
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge value={m.status} label={m.status} />
                          <Badge value={m.priority} label={`P_${m.priority}`} />
                          <span className="font-micro text-[9px] text-neutral-400 break-all">
                            {m.mission_id}
                          </span>
                          <span className="font-micro text-[9px] text-neutral-500">
                            {m.objective?.slice(0, 70)}
                          </span>
                          <div className="ml-auto flex gap-1">
                            {m.status === "approved" && can("mission:dispatch") && (
                              <Btn
                                variant="cyan"
                                testid={`btn-dispatch-${m.mission_id}`}
                                onClick={() => act(() => api.post(`/missions/${m.mission_id}/dispatch`, {}), `MISSION DISPATCHED ${m.mission_id}`)}
                              >
                                DISPATCH
                              </Btn>
                            )}
                            <Btn
                              testid={`btn-route-${m.mission_id}`}
                              onClick={() => act(() => api.post(`/missions/${m.mission_id}/route/recompute`), "ROUTE RECOMPUTED WITH CURRENT HAZARD OVERLAY")}
                            >
                              RECOMPUTE ROUTE
                            </Btn>
                          </div>
                        </div>
                        <div className="font-micro text-[9px] text-neutral-600 mt-1">
                          ETA {m.route?.eta_seconds ? `${Math.round(m.route.eta_seconds / 60)} min` : "—"} ·
                          ROUTE {m.route?.status || "—"} · STAGES {m.history?.length || 0}
                        </div>
                      </Row>
                    ))}
                  </Panel>
                );
              })}
              {!s.missions.length && <Empty label="NO MISSIONS" />}
            </div>
          )}

          {(tab === "AI" || tab === "APPROVALS") && (
            <div className="h-full overflow-y-auto p-3 space-y-3">
              {(tab === "APPROVALS" ? s.approval_queue : s.recommendations).map((r) => (
                <div
                  key={r.recommendation_id}
                  data-testid={`rec-${r.recommendation_id}`}
                  className="panel border-dashed"
                >
                  <div className="px-3 py-2 border-b border-line bg-elevated flex flex-wrap items-center gap-2">
                    <span className="font-micro text-[10px] text-signal-cyan">{r.agent_name}</span>
                    <Badge value={r.risk_tier === "high" ? "critical" : r.risk_tier} label={`RISK_${r.risk_tier}`} />
                    <Badge value={r.critic?.result} label={`CRITIC_${r.critic?.result}`} />
                    <Badge value="SIMULATED" label={`CONF_${Math.round((r.confidence || 0) * 100)}%`} />
                    <Badge value={r.approval_status} label={r.approval_status} />
                    {r.fallback_used && <Badge value="DEGRADED" label="LLM_FALLBACK" />}
                    <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(r.created_at)}</span>
                  </div>
                  <div className="p-3 grid grid-cols-1 lg:grid-cols-3 gap-3">
                    <div className="lg:col-span-2 space-y-2">
                      <p className="text-sm text-neutral-200 leading-relaxed">{r.summary}</p>
                      <div>
                        <div className="font-micro text-[9px] text-neutral-500">KEY RISKS</div>
                        {(r.key_risks || []).map((k, i) => (
                          <div key={i} className="font-micro text-[10px] text-signal-amber">· {k}</div>
                        ))}
                      </div>
                      <div>
                        <div className="font-micro text-[9px] text-neutral-500">INFORMATION GAPS</div>
                        {(r.information_gaps || []).length ? (
                          r.information_gaps.map((k, i) => (
                            <div key={i} className="font-micro text-[10px] text-neutral-400">· {k}</div>
                          ))
                        ) : (
                          <div className="font-micro text-[10px] text-neutral-600">NONE DECLARED</div>
                        )}
                      </div>
                      <div>
                        <div className="font-micro text-[9px] text-neutral-500">CRITIC RATIONALE</div>
                        <div className="font-micro text-[10px] text-neutral-300">{r.critic?.rationale}</div>
                        <div className="font-micro text-[9px] text-neutral-600">
                          MODE {r.critic?.mode} · CODES {(r.critic?.reason_codes || []).join(", ") || "NONE"}
                        </div>
                      </div>
                      <div>
                        <div className="font-micro text-[9px] text-neutral-500">ALTERNATIVES</div>
                        {(r.alternatives || []).map((a) => (
                          <div key={a.alternative_id} className="font-micro text-[10px] text-neutral-400">
                            · {a.resource_label} ({a.resource_kind}) ETA{" "}
                            {a.eta_seconds ? Math.round(a.eta_seconds / 60) : "—"}m · COST {a.cost}
                            {a.feasible ? "" : " · INFEASIBLE"}
                          </div>
                        ))}
                      </div>
                    </div>
                    <div className="space-y-1">
                      <div className="font-micro text-[9px] text-neutral-500">EVIDENCE</div>
                      {(r.evidence_refs || []).map((e) => (
                        <div key={e} className="font-micro text-[9px] text-signal-cyan break-all">{e}</div>
                      ))}
                      <div className="pt-2 border-t border-line mt-2">
                        {Object.entries(r.evidence_summary || {}).map(([k, v]) => (
                          <KV key={k} k={k} v={String(v)} />
                        ))}
                      </div>
                      <div className="pt-2 border-t border-line mt-2">
                        <KV k="ASSET" v={r.proposed_action?.resource_label} />
                        <KV k="MODEL" v={`${r.model?.provider}/${r.model?.model}`} />
                        <KV k="TRACE" v={(r.trace_id || "").slice(0, 12)} />
                      </div>
                      {r.human_decision && (
                        <div className="pt-2 border-t border-line mt-2">
                          <Badge value={r.human_decision.decision} label={`HUMAN_${r.human_decision.decision}`} />
                          <div className="font-micro text-[9px] text-neutral-500">
                            {r.human_decision.reason || "no reason recorded"} · {ago(r.human_decision.at)}
                          </div>
                        </div>
                      )}
                      {can("recommendation:decide") &&
                        ["pending_approval", "blocked"].includes(r.approval_status) && (
                          <div className="pt-3 space-y-2">
                            {r.approval_status === "blocked" ? (
                              <Btn
                                variant="red"
                                testid={`btn-override-${r.recommendation_id}`}
                                className="w-full"
                                onClick={() =>
                                  act(
                                    () =>
                                      api.post(`/recommendations/${r.recommendation_id}/emergency-override`, {
                                        reason: "Commander accepts residual risk after review of evidence",
                                        acknowledge_risk: true,
                                      }),
                                    "EMERGENCY OVERRIDE RECORDED · MISSION CREATED"
                                  )
                                }
                              >
                                EMERGENCY OVERRIDE
                              </Btn>
                            ) : (
                              <Btn
                                variant="green"
                                testid={`btn-approve-${r.recommendation_id}`}
                                className="w-full"
                                onClick={() =>
                                  act(
                                    () =>
                                      api.post(`/recommendations/${r.recommendation_id}/approve`, {
                                        reason: "Evidence sufficient; asset suitable",
                                      }),
                                    "APPROVED · MISSION CREATED AND AUDITED"
                                  )
                                }
                              >
                                APPROVE
                              </Btn>
                            )}
                            <Btn
                              variant="red"
                              testid={`btn-reject-${r.recommendation_id}`}
                              className="w-full"
                              onClick={() =>
                                act(
                                  () =>
                                    api.post(`/recommendations/${r.recommendation_id}/reject`, {
                                      reason: "Insufficient corroboration",
                                    }),
                                  "REJECTED · DECISION IS IMMUTABLE"
                                )
                              }
                            >
                              REJECT
                            </Btn>
                          </div>
                        )}
                    </div>
                  </div>
                </div>
              ))}
              {!(tab === "APPROVALS" ? s.approval_queue : s.recommendations).length && (
                <Empty label="NO RECOMMENDATIONS" />
              )}
            </div>
          )}

          {tab === "COMMS" && <CommsTab snapshot={s} act={act} can={can} />}
          {tab === "SYNC" && <SyncTab snapshot={s} act={act} />}
          {tab === "AUDIT" && <AuditTab />}
          {tab === "SIM" && <SimTab act={act} />}
          {tab === "SYSTEM" && <SystemTab />}
        </div>
      </div>
    </Shell>
  );
}

function flatten(obj, prefix = "") {
  const out = {};
  Object.entries(obj || {}).forEach(([k, v]) => {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      Object.assign(out, flatten(v, `${prefix}${k}.`));
    } else {
      out[`${prefix}${k}`] = Array.isArray(v) ? v.join(",") : v;
    }
  });
  return out;
}

function CommsTab({ snapshot, act, can }) {
  const [queue, setQueue] = useState(null);
  const load = () => api.get("/comm/queue").then(({ data }) => setQueue(data)).catch(() => {});
  useEffect(() => {
    load();
  }, []);
  return (
    <div className="h-full overflow-y-auto p-3 grid grid-cols-1 lg:grid-cols-2 gap-3">
      <Panel title="TRANSPORTS · DETERMINISTIC SELECTION POLICY" testid="panel-transports">
        <div className="p-3">
          {snapshot.transports.map((t) => (
            <div key={t.name} className="border-b border-line/60 py-1.5">
              <div className="flex items-center gap-2">
                <span className="font-micro text-[10px] text-white w-24">{t.name.toUpperCase()}</span>
                {t.simulated && <Badge value="SIMULATED" label="SIM" />}
                <Badge value={t.available ? "CONNECTED" : "ISOLATED"} label={t.available ? "UP" : "DOWN"} />
                {can("comm:write") && (
                  <div className="ml-auto flex gap-1">
                    <Btn
                      testid={`btn-degrade-${t.name}`}
                      variant="amber"
                      onClick={() => act(() => api.post("/comm/transports/degrade", { transport: t.name, degradation: 1.0 }).then(load), `${t.name.toUpperCase()} FORCED DOWN`)}
                    >
                      FAIL
                    </Btn>
                    <Btn
                      testid={`btn-restore-${t.name}`}
                      variant="green"
                      onClick={() => act(() => api.post("/comm/transports/degrade", { transport: t.name, degradation: 0 }).then(load), `${t.name.toUpperCase()} RESTORED`)}
                    >
                      RESTORE
                    </Btn>
                  </div>
                )}
              </div>
              <div className="font-micro text-[9px] text-neutral-500">
                LAT {t.latency_ms}ms · BW {t.bandwidth_kbps}kbps · REL{" "}
                {Math.round(t.reliability * 100)}% · COST {t.cost_per_kb}/kb · ACK{" "}
                {t.supports_ack ? "YES" : "NO"} · PRIO {t.supports_priority ? "YES" : "NO"}
              </div>
            </div>
          ))}
        </div>
      </Panel>
      <Panel
        title="OUTBOUND QUEUE"
        testid="panel-comm-queue"
        right={
          can("comm:write") && (
            <>
              <Btn testid="btn-enqueue-test" onClick={() => act(() => api.post("/comm/enqueue-test").then(load), "PROBE MESSAGE QUEUED")}>
                ENQUEUE PROBE
              </Btn>
              <Btn variant="cyan" testid="btn-flush-queue" onClick={() => act(() => api.post("/comm/queue/flush").then(load), "QUEUE FLUSH ATTEMPTED")}>
                FLUSH
              </Btn>
            </>
          )
        }
      >
        {!queue ? (
          <Loader />
        ) : (
          <div>
            <div className="p-3 grid grid-cols-4 gap-2">
              {["QUEUED", "DELIVERED", "FAILED", "EXPIRED"].map((k) => (
                <Stat key={k} label={k} value={queue.depth[k]} testid={`queue-stat-${k}`} />
              ))}
            </div>
            {queue.items.slice(0, 25).map((m) => (
              <Row key={m.message_id} testid={`msg-${m.message_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge value={m.state} />
                  <Badge value={m.priority} label={m.priority} />
                  <span className="font-micro text-[9px] text-neutral-400">{m.event_type}</span>
                  <span className="ml-auto font-micro text-[9px] text-neutral-600">
                    {m.transport || "—"} · {m.attempts} ATT · {m.size_kb}kb
                  </span>
                </div>
                {m.last_error && (
                  <div className="font-micro text-[9px] text-signal-red">{m.last_error}</div>
                )}
              </Row>
            ))}
          </div>
        )}
      </Panel>
      <Panel title="GATEWAYS" testid="panel-gateways" className="lg:col-span-2">
        {snapshot.gateways.map((g) => (
          <Row key={g.gateway_id} testid={`gw-${g.gateway_id}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge value={g.status} label={g.status} />
              <Badge value={g.connectivity_state} />
              <span className="font-micro text-[10px] text-white">{g.name}</span>
              <span className="font-micro text-[9px] text-neutral-500">
                BATTERY {g.battery_pct}% · MESH {g.mesh_roster?.length} PEERS
              </span>
              <span className="ml-auto font-micro text-[9px] text-neutral-600">
                UNSYNCED {g.edge_runtime?.unsynced_events} · OUTBOUND{" "}
                {g.edge_runtime?.outbound_queued} · SQLITE{" "}
                {Math.round((g.edge_runtime?.db_size_bytes || 0) / 1024)}kb
              </span>
            </div>
          </Row>
        ))}
      </Panel>
    </div>
  );
}

function SyncTab({ snapshot, act }) {
  const [status, setStatus] = useState(null);
  const load = () => api.get("/sync/status").then(({ data }) => setStatus(data)).catch(() => {});
  useEffect(() => {
    load();
  }, []);
  return (
    <div className="h-full overflow-y-auto p-3 grid grid-cols-1 lg:grid-cols-2 gap-3">
      <Panel title="SYNCHRONIZATION STATE" testid="panel-sync-state">
        {!status ? (
          <Loader />
        ) : (
          <div className="p-3">
            <KV k="SERVER EVENTS" v={status.server_digest.event_count} />
            <KV k="LATEST HLC" v={status.server_digest.latest_hlc} />
            <KV k="UNAPPLIED" v={status.unapplied_events} />
            <KV k="QUARANTINED" v={status.quarantined_events} tone="#FF3B30" />
            <KV k="PENDING CONFLICTS" v={status.pending_conflicts} tone="#FFB020" />
            <div className="mt-3 font-micro text-[9px] text-neutral-500">NODES</div>
            {status.nodes.map((n) => (
              <div key={n.device_id} className="flex items-center gap-2 py-0.5">
                <Badge value={n.state} />
                <span className="font-micro text-[9px] text-neutral-400 break-all">{n.device_id}</span>
                <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(n.last_sync_at)}</span>
              </div>
            ))}
            <div className="mt-3 font-micro text-[9px] text-neutral-500">CONFLICT FIELD CLASSES</div>
            {Object.entries(status.field_classes).map(([k, v]) => (
              <KV key={k} k={k} v={v} />
            ))}
          </div>
        )}
      </Panel>
      <Panel title="CONFLICTS" testid="panel-conflicts">
        {!snapshot.sync.conflicts.length && <Empty label="NO CONFLICTS DETECTED" />}
        {snapshot.sync.conflicts.map((c) => (
          <Row key={c.conflict_id} testid={`conflict-${c.conflict_id}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge value={c.status === "resolved" ? "SYNCED" : "CONFLICT"} label={c.status} />
              <span className="font-micro text-[10px] text-white">{c.path}</span>
              <span className="font-micro text-[9px] text-neutral-500">{c.field_class}</span>
              {c.status === "pending_review" && (
                <div className="ml-auto flex gap-1">
                  <Btn
                    testid={`btn-keep-local-${c.conflict_id}`}
                    onClick={() => act(() => api.post(`/sync/conflicts/${c.conflict_id}/resolve`, { resolution: "keep_local", reason: "Commander confirms local state" }).then(load), "CONFLICT RESOLVED · KEEP LOCAL")}
                  >
                    KEEP LOCAL
                  </Btn>
                  <Btn
                    variant="cyan"
                    testid={`btn-accept-remote-${c.conflict_id}`}
                    onClick={() => act(() => api.post(`/sync/conflicts/${c.conflict_id}/resolve`, { resolution: "accept_remote", reason: "Field report supersedes" }).then(load), "CONFLICT RESOLVED · ACCEPT REMOTE")}
                  >
                    ACCEPT REMOTE
                  </Btn>
                </div>
              )}
            </div>
            <div className="font-micro text-[9px] text-neutral-500">
              LOCAL {JSON.stringify(c.local)} → REMOTE {JSON.stringify(c.remote)} ·{" "}
              {c.resolution?.reason}
            </div>
          </Row>
        ))}
      </Panel>
    </div>
  );
}

function AuditTab() {
  const [items, setItems] = useState(null);
  useEffect(() => {
    api.get("/audit?limit=250").then(({ data }) => setItems(data.items)).catch(() => setItems([]));
  }, []);
  return (
    <div className="h-full overflow-y-auto p-3">
      <Panel title="AUDIT TRAIL · APPEND-ONLY" testid="panel-audit">
        {!items ? (
          <Loader />
        ) : (
          items.map((a) => (
            <Row key={a.entry_id} testid={`audit-${a.entry_id}`}>
              <div className="flex flex-wrap items-center gap-2">
                <Badge value={a.outcome === "denied" ? "block" : "pass"} label={a.outcome} />
                <span className="font-micro text-[10px] text-signal-cyan">{a.action}</span>
                <span className="font-micro text-[9px] text-neutral-400 break-all">
                  {a.entity_type}:{a.entity_id || "—"}
                </span>
                {a.decision && <Badge value={a.decision} label={a.decision} />}
                <span className="ml-auto font-micro text-[9px] text-neutral-600">
                  {a.actor_id || "system"} · {a.device_id || "—"} · {ago(a.created_at)}
                </span>
              </div>
            </Row>
          ))
        )}
      </Panel>
    </div>
  );
}

function SimTab({ act }) {
  const [scenarios, setScenarios] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const load = async () => {
    const [{ data: sc }, { data: r }] = await Promise.all([
      api.get("/simulations/scenarios"),
      api.get("/simulations"),
    ]);
    setScenarios(sc);
    setRuns(r.items);
  };
  useEffect(() => {
    load().catch(() => setScenarios({ scenarios: [] }));
  }, []);
  const open = async (id) => {
    const { data } = await api.get(`/simulations/${id}`);
    setSelected(data);
  };
  return (
    <div className="h-full overflow-y-auto p-3 grid grid-cols-1 lg:grid-cols-2 gap-3">
      <Panel title="SCENARIOS" testid="panel-scenarios">
        {!scenarios ? (
          <Loader />
        ) : (
          <div>
            <div className="p-3 border-b border-line">
              <div className="font-micro text-[9px] text-neutral-500">ISOLATION</div>
              <KV k="SIM DATABASE" v={scenarios.isolation?.database} tone="#00FF66" />
              <KV k="PROD DATABASE" v={scenarios.isolation?.production_database} />
              <KV k="EVENT NAMESPACE" v={scenarios.isolation?.event_namespace} />
              <KV k="TRANSPORTS" v={scenarios.isolation?.transport_registry} />
            </div>
            {scenarios.scenarios.map((sc) => (
              <Row key={sc.key} testid={`scenario-${sc.key}`}>
                <div className="flex items-center gap-2">
                  <span className="font-micro text-[10px] text-white">{sc.key.toUpperCase()}</span>
                  <Btn
                    className="ml-auto"
                    variant="cyan"
                    testid={`btn-run-${sc.key}`}
                    onClick={() => act(() => api.post("/simulations", { scenario: sc.key, ticks: 3 }).then(load), `SIMULATION STARTED · ${sc.key}`)}
                  >
                    RUN
                  </Btn>
                </div>
                <div className="font-micro text-[9px] text-neutral-500">{sc.description}</div>
              </Row>
            ))}
          </div>
        )}
      </Panel>
      <Panel title="RUNS" testid="panel-sim-runs" right={<Btn testid="btn-refresh-sims" onClick={load}>REFRESH</Btn>}>
        {!runs.length && <Empty label="NO SIMULATION RUNS" />}
        {runs.map((r) => (
          <Row key={r.simulation_id} testid={`sim-${r.simulation_id}`} onClick={() => open(r.simulation_id)}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge value={r.status === "completed" ? "SYNCED" : r.status === "running" ? "SYNCING" : "DEGRADED"} label={r.status} />
              <span className="font-micro text-[10px] text-white">{r.scenario.toUpperCase()}</span>
              <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(r.started_at)}</span>
            </div>
            {r.metrics && (
              <div className="font-micro text-[9px] text-neutral-500">
                RESILIENCE {r.metrics.resilience_score} · EVENTS {r.metrics.events_generated} ·
                DELIVERY {r.metrics.delivery_success_rate ?? "—"} · BLOCKED{" "}
                {r.metrics.recommendations_blocked_by_critic}
              </div>
            )}
          </Row>
        ))}
        {selected && (
          <div className="p-3 border-t border-line">
            <div className="font-micro text-[10px] text-signal-cyan mb-1">
              {selected.scenario.toUpperCase()} · {selected.status}
            </div>
            {Object.entries(selected.metrics || {}).filter(([k]) => k !== "transport_states").map(([k, v]) => (
              <KV key={k} k={k} v={typeof v === "object" ? JSON.stringify(v) : String(v)} />
            ))}
            <div className="font-micro text-[9px] text-neutral-500 mt-2">STEPS</div>
            {(selected.steps || []).slice(-12).map((st, i) => (
              <div key={i} className="font-micro text-[9px] text-neutral-400">
                · {st.step}: {JSON.stringify(st.detail).slice(0, 150)}
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}

function SystemTab() {
  const [health, setHealth] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [agents, setAgents] = useState([]);
  useEffect(() => {
    api.get("/system/health").then(({ data }) => setHealth(data)).catch(() => {});
    api.get("/observability/metrics").then(({ data }) => setMetrics(data)).catch(() => {});
    api.get("/observability/agent-invocations").then(({ data }) => setAgents(data.items)).catch(() => {});
  }, []);
  return (
    <div className="h-full overflow-y-auto p-3 grid grid-cols-1 lg:grid-cols-3 gap-3">
      <Panel title="SYSTEM HEALTH" testid="panel-health">
        {!health ? <Loader /> : (
          <div className="p-3">
            {Object.entries(health.checks).map(([k, v]) => (
              <div key={k} className="mb-2">
                <div className="font-micro text-[10px] text-signal-cyan">{k.toUpperCase()}</div>
                {Object.entries(v).map(([kk, vv]) => (
                  <KV key={kk} k={kk} v={typeof vv === "object" ? JSON.stringify(vv) : String(vv)} />
                ))}
              </div>
            ))}
          </div>
        )}
      </Panel>
      <Panel title="TELEMETRY COUNTERS" testid="panel-metrics">
        {!metrics ? <Loader /> : (
          <div className="p-3 max-h-[70vh] overflow-y-auto">
            {Object.entries(metrics.counters).map(([k, v]) => (
              <KV key={k} k={k} v={typeof v === "number" ? v.toFixed(0) : v} />
            ))}
          </div>
        )}
      </Panel>
      <Panel title="AGENT INVOCATIONS" testid="panel-agents">
        {!agents.length && <Empty label="NO AGENT CALLS YET" />}
        {agents.map((a, i) => (
          <Row key={i} testid={`agent-${i}`}>
            <div className="flex items-center gap-2">
              <Badge value={a.status === "ok" ? "pass" : "block"} label={a.status} />
              <span className="font-micro text-[10px] text-white">{a.agent}</span>
              <span className="ml-auto font-micro text-[9px] text-neutral-600">
                {a.model} · {a.latency_ms ?? "—"}ms
              </span>
            </div>
            {a.error && <div className="font-micro text-[9px] text-signal-red">{String(a.error).slice(0, 120)}</div>}
          </Row>
        ))}
      </Panel>
    </div>
  );
}
