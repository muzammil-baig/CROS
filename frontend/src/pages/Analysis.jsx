import React, { useCallback, useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, Stat, ago } from "../components/kit";
import { Shell } from "../components/Shell";
import { useLive } from "../context/LiveContext";

export default function Analysis() {
  const { snapshot } = useLive();
  const [runs, setRuns] = useState([]);
  const [scenarios, setScenarios] = useState([]);
  const [selected, setSelected] = useState(null);
  const [note, setNote] = useState(null);
  const [audit, setAudit] = useState([]);

  const load = useCallback(async () => {
    try {
      const [sc, r, a] = await Promise.all([
        api.get("/simulations/scenarios"),
        api.get("/simulations"),
        api.get("/audit?limit=120"),
      ]);
      setScenarios(sc.data.scenarios);
      setRuns(r.data.items);
      setAudit(a.data.items);
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  }, []);

  useEffect(() => {
    load();
    const i = setInterval(load, 12000);
    return () => clearInterval(i);
  }, [load]);

  const start = async (key) => {
    try {
      const { data } = await api.post("/simulations", { scenario: key, ticks: 3 });
      setNote({ tone: "#00FF66", text: `SIMULATION ${data.simulation_id} RUNNING IN ISOLATED DB ${data.isolation.database}` });
      load();
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  };

  const s = snapshot;
  const missionStats = (s?.missions || []).reduce((acc, m) => {
    acc[m.status] = (acc[m.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <Shell>
      <div className="h-full overflow-y-auto p-4 space-y-3">
        <h1 className="font-macro text-3xl text-white">ANALYSIS &amp; RESILIENCE</h1>
        {note && (
          <div className="panel px-3 py-2 font-micro text-[10px]" data-testid="analysis-note" style={{ color: note.tone, borderColor: note.tone }}>
            {note.text}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
          <Stat label="TOTAL EVENTS" value={s?.stats?.total_events ?? "—"} testid="stat-events" />
          <Stat label="OPEN DEMAND" value={s?.stats?.open_requests ?? "—"} tone="#FFB020" testid="stat-demand" />
          <Stat label="COMPLETED MISSIONS" value={s?.stats?.completed_missions ?? "—"} tone="#00FF66" testid="stat-completed" />
          <Stat label="PENDING APPROVALS" value={s?.stats?.pending_approvals ?? "—"} tone="#FF4500" testid="stat-pending" />
          <Stat label="QUARANTINED" value={s?.stats?.quarantined_events ?? "—"} tone="#FF3B30" testid="stat-quar" />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <Panel title="SCENARIO LIBRARY" testid="panel-analysis-scenarios">
            {!scenarios.length && <Loader />}
            {scenarios.map((sc) => (
              <Row key={sc.key} testid={`analysis-scenario-${sc.key}`}>
                <div className="flex items-center gap-2">
                  <span className="font-micro text-[10px] text-white">{sc.key.toUpperCase()}</span>
                  <Btn className="ml-auto" variant="cyan" testid={`analysis-run-${sc.key}`} onClick={() => start(sc.key)}>
                    RUN
                  </Btn>
                </div>
                <div className="font-micro text-[9px] text-neutral-500">{sc.description}</div>
              </Row>
            ))}
          </Panel>

          <Panel title="RESILIENCE METRICS" testid="panel-resilience">
            {!runs.length && <Empty label="NO RUNS YET" />}
            {runs.map((r) => (
              <Row key={r.simulation_id} testid={`analysis-run-row-${r.simulation_id}`} onClick={() => api.get(`/simulations/${r.simulation_id}`).then(({ data }) => setSelected(data))}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge value={r.status === "completed" ? "SYNCED" : r.status === "running" ? "SYNCING" : "DEGRADED"} label={r.status} />
                  <span className="font-micro text-[10px] text-white">{r.scenario.toUpperCase()}</span>
                  <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(r.started_at)}</span>
                </div>
                {r.metrics && (
                  <div className="font-micro text-[9px] text-neutral-500">
                    RESILIENCE {r.metrics.resilience_score} · EVENTS {r.metrics.events_generated} · DELIVERY{" "}
                    {r.metrics.delivery_success_rate ?? "—"} · UNRESOLVED {r.metrics.unresolved_demand} ·
                    FINAL LINK {r.metrics.final_connectivity_state}
                  </div>
                )}
              </Row>
            ))}
            {selected && (
              <div className="p-3 border-t border-line" data-testid="analysis-selected">
                <div className="font-micro text-[10px] text-signal-cyan mb-1">
                  {selected.scenario.toUpperCase()} · {selected.status}
                </div>
                {Object.entries(selected.metrics || {})
                  .filter(([k]) => k !== "transport_states")
                  .map(([k, v]) => (
                    <KV key={k} k={k} v={typeof v === "object" ? JSON.stringify(v) : String(v)} />
                  ))}
              </div>
            )}
          </Panel>

          <Panel title="MISSION LIFECYCLE DISTRIBUTION" testid="panel-lifecycle">
            <div className="p-3">
              {Object.entries(missionStats).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 py-0.5">
                  <span className="font-micro text-[10px] w-44 text-neutral-300">{k.toUpperCase()}</span>
                  <div className="flex-1 h-2 bg-void border border-line">
                    <div className="h-full bg-signal-cyan" style={{ width: `${Math.min(100, v * 12)}%` }} />
                  </div>
                  <span className="font-micro text-[10px] text-white w-6 text-right">{v}</span>
                </div>
              ))}
              {!Object.keys(missionStats).length && <Empty label="NO MISSIONS" />}
            </div>
          </Panel>

          <Panel title="AUDIT ANALYSIS · DECISIONS" testid="panel-audit-analysis">
            {audit
              .filter((a) => a.decision || a.outcome === "denied")
              .slice(0, 25)
              .map((a) => (
                <Row key={a.entry_id} testid={`audit-decision-${a.entry_id}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge value={a.outcome === "denied" ? "block" : a.decision} label={a.decision || a.outcome} />
                    <span className="font-micro text-[10px] text-signal-cyan">{a.action}</span>
                    <span className="font-micro text-[9px] text-neutral-500 break-all">{a.entity_id}</span>
                    <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(a.created_at)}</span>
                  </div>
                </Row>
              ))}
            {!audit.length && <Empty label="NO AUDIT DATA" />}
          </Panel>
        </div>
      </div>
    </Shell>
  );
}
