import React, { useCallback, useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, Stat, ago } from "../components/kit";
import { Shell } from "../components/Shell";
import { useLive } from "../context/LiveContext";
import { useAuth } from "../context/AuthContext";

/** Communications Operator + Field Gateway Operator console. */
export default function Comms({ mode }) {
  const gatewayMode = mode === "gateway";
  const { snapshot } = useLive();
  const { can } = useAuth();
  const [transports, setTransports] = useState(null);
  const [queue, setQueue] = useState(null);
  const [gateways, setGateways] = useState([]);
  const [sync, setSync] = useState(null);
  const [note, setNote] = useState(null);
  const [peers, setPeers] = useState({});
  const [relay, setRelay] = useState(null);

  const load = useCallback(async () => {
    try {
      const [t, q, g, s] = await Promise.all([
        api.get("/comm/transports"),
        api.get("/comm/queue"),
        api.get("/gateways"),
        api.get("/sync/status"),
      ]);
      setTransports(t.data);
      setQueue(q.data);
      setGateways(g.data.items);
      setSync(s.data);
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  }, []);

  useEffect(() => {
    load();
    const i = setInterval(load, 15000);
    return () => clearInterval(i);
  }, [load]);

  const run = async (fn, msg) => {
    try {
      await fn();
      setNote({ tone: "#00FF66", text: msg });
      load();
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  };

  if (!transports) return <Shell><Loader label="LOADING COMMUNICATION STATE" /></Shell>;

  return (
    <Shell>
      <div className="h-full overflow-y-auto p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h1 className="font-macro text-3xl text-white">
            {gatewayMode ? "FIELD GATEWAY" : "COMMUNICATIONS"}
          </h1>
          <div className="flex items-center gap-2">
            <Badge value={transports.connectivity_state} testid="comms-connectivity" />
            <Badge value="SIMULATED" label={`SIM:${transports.simulated_transports.join("/")}`} testid="comms-simulated" />
          </div>
        </div>

        {note && (
          <div className="panel px-3 py-2 font-micro text-[10px]" data-testid="comms-note" style={{ color: note.tone, borderColor: note.tone }}>
            {note.text}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
          <Stat label="QUEUED" value={queue?.depth?.QUEUED ?? 0} tone="#FFB020" testid="stat-queued" />
          <Stat label="DELIVERED" value={queue?.depth?.DELIVERED ?? 0} tone="#00FF66" testid="stat-delivered" />
          <Stat label="FAILED" value={queue?.depth?.FAILED ?? 0} tone="#FF3B30" testid="stat-failed" />
          <Stat label="CONFLICTS" value={sync?.pending_conflicts ?? 0} tone="#FF4500" testid="stat-conflicts" />
          <Stat label="QUARANTINED" value={sync?.quarantined_events ?? 0} tone="#FF3B30" testid="stat-quarantined" />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <Panel title="TRANSPORT LAYER" testid="panel-transport-layer">
            <div className="p-3">
              <div className="font-micro text-[9px] text-neutral-500 mb-2">
                SELECTION FOR CRITICAL 2KB → {transports.selection_for_critical_2kb.chosen?.toUpperCase() || "NONE"}
              </div>
              {transports.items.map((t) => {
                const score = transports.selection_for_critical_2kb.scoring.find((s) => s.transport === t.name);
                return (
                  <div key={t.name} className="border-b border-line/60 py-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-micro text-[10px] text-white w-24">{t.name.toUpperCase()}</span>
                      {t.simulated && <Badge value="SIMULATED" label="SIM" />}
                      <Badge value={t.available ? (t.reliability > 0.85 ? "CONNECTED" : "DEGRADED") : "ISOLATED"} label={t.available ? "UP" : "DOWN"} />
                      <span className="font-micro text-[9px] text-signal-cyan">
                        SCORE {score?.score ?? "INELIGIBLE"}
                      </span>
                      {can("comm:write") && (
                        <div className="ml-auto flex gap-1">
                          <Btn testid={`comms-fail-${t.name}`} variant="amber" onClick={() => run(() => api.post("/comm/transports/degrade", { transport: t.name, degradation: 1 }), `${t.name.toUpperCase()} DOWN`)}>
                            FAIL
                          </Btn>
                          <Btn testid={`comms-degrade-${t.name}`} onClick={() => run(() => api.post("/comm/transports/degrade", { transport: t.name, degradation: 0.5 }), `${t.name.toUpperCase()} DEGRADED 50%`)}>
                            50%
                          </Btn>
                          <Btn testid={`comms-restore-${t.name}`} variant="green" onClick={() => run(() => api.post("/comm/transports/degrade", { transport: t.name, degradation: 0 }), `${t.name.toUpperCase()} RESTORED`)}>
                            RESTORE
                          </Btn>
                        </div>
                      )}
                    </div>
                    <div className="font-micro text-[9px] text-neutral-500">
                      LAT {t.latency_ms}ms · BW {t.bandwidth_kbps}kbps · REL {Math.round(t.reliability * 100)}% ·
                      COST {t.cost_per_kb}/kb · ENERGY {t.energy_cost} · MAX {t.max_payload_kb}kb
                    </div>
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel
            title="DELIVERY QUEUE"
            testid="panel-delivery-queue"
            right={
              can("comm:write") && (
                <>
                  <Btn testid="comms-probe" onClick={() => run(() => api.post("/comm/enqueue-test"), "PROBE QUEUED")}>PROBE</Btn>
                  <Btn variant="cyan" testid="comms-flush" onClick={() => run(() => api.post("/comm/queue/flush"), "FLUSH ATTEMPTED")}>FLUSH</Btn>
                </>
              )
            }
          >
            {!queue?.items?.length && <Empty label="QUEUE EMPTY" />}
            {(queue?.items || []).slice(0, 30).map((m) => (
              <Row key={m.message_id} testid={`comms-msg-${m.message_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge value={m.state} />
                  <Badge value={m.priority} label={m.priority} />
                  <span className="font-micro text-[9px] text-neutral-400">{m.event_type}</span>
                  <span className="ml-auto font-micro text-[9px] text-neutral-600">
                    {m.transport || "—"} · {m.size_kb}kb · {ago(m.queued_at)}
                  </span>
                </div>
                {m.last_error && <div className="font-micro text-[9px] text-signal-red">{m.last_error}</div>}
              </Row>
            ))}
          </Panel>

          <Panel title="GATEWAYS · EDGE RUNTIME (SQLITE)" testid="panel-gateway-runtime" className="lg:col-span-2">
            {gateways.map((g) => (
              <div key={g.gateway_id} className="px-3 py-2 border-b border-line/70" data-testid={`gateway-${g.gateway_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge value={g.status} label={g.status} />
                  <Badge value={g.connectivity_state} />
                  <span className="font-micro text-[10px] text-white">{g.name}</span>
                  <span className="font-micro text-[9px] text-neutral-500">{g.hardware}</span>
                  {can("gateway:admin") && (
                    <div className="ml-auto flex gap-1">
                      <Btn testid={`gw-sync-${g.gateway_id}`} variant="cyan" onClick={() => run(() => api.post(`/gateways/${g.gateway_id}/sync`, { device_id: g.gateway_id, known_event_ids: [], events: [], max_events: 25 }), `GATEWAY ${g.gateway_id} SYNC ROUND COMPLETE`)}>
                        SYNC
                      </Btn>
                      <select
                        data-testid={`gw-peer-${g.gateway_id}`}
                        value={peers[g.gateway_id] || ""}
                        onChange={(e) => setPeers((p) => ({ ...p, [g.gateway_id]: e.target.value }))}
                        className="font-micro text-[10px] bg-void border border-line text-neutral-300 px-1 py-1"
                      >
                        <option value="">PEER…</option>
                        {gateways
                          .filter((o) => o.gateway_id !== g.gateway_id)
                          .map((o) => (
                            <option key={o.gateway_id} value={o.gateway_id}>
                              {o.gateway_id}
                            </option>
                          ))}
                      </select>
                      <Btn
                        testid={`gw-mesh-relay-${g.gateway_id}`}
                        variant="green"
                        disabled={!peers[g.gateway_id]}
                        onClick={() =>
                          run(async () => {
                            const { data } = await api.post(`/gateways/${g.gateway_id}/mesh-relay`, {
                              peer_gateway_id: peers[g.gateway_id],
                              max_events: 25,
                              push_upstream: true,
                            });
                            setRelay(data);
                          }, `MESH RELAY ${g.gateway_id} → ${peers[g.gateway_id]} COMPLETE`)
                        }
                      >
                        MESH RELAY
                      </Btn>
                      <Btn testid={`gw-restart-${g.gateway_id}`} variant="amber" onClick={() => run(() => api.post(`/gateways/${g.gateway_id}/restart`), `GATEWAY ${g.gateway_id} RESTARTED`)}>
                        RESTART
                      </Btn>
                    </div>
                  )}
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4">
                  <KV k="LOCAL EVENTS" v={g.edge_runtime?.local_events} />
                  <KV k="UNSYNCED" v={g.edge_runtime?.unsynced_events} tone="#FFB020" />
                  <KV k="OUTBOUND QUEUE" v={g.edge_runtime?.outbound_queued} />
                  <KV k="PROJECTIONS" v={g.edge_runtime?.local_projections} />
                  <KV k="CACHED DEVICES" v={g.edge_runtime?.cached_devices} />
                  <KV k="SQLITE SIZE" v={`${Math.round((g.edge_runtime?.db_size_bytes || 0) / 1024)} kb`} />
                  <KV k="SPATIAL ENGINE" v={g.edge_runtime?.spatial_engine} />
                  <KV k="LAST SYNC" v={ago(g.edge_runtime?.last_sync)} />
                </div>
                <div className="font-micro text-[9px] text-neutral-600">
                  MESH ROSTER: {(g.mesh_roster || []).join(", ") || "—"} · BATTERY {g.battery_pct}%
                </div>
              </div>
            ))}
          </Panel>
          {relay && (
            <Panel title="MESH RELAY RESULT · TRANSPORT HOP (IDENTITY PRESERVED)" testid="panel-mesh-relay" className="lg:col-span-2">
              <div className="p-3">
                <div className="flex flex-wrap gap-2 mb-2">
                  <Badge value="MESH_ONLY" label={`${relay.source_gateway_id} → ${relay.peer_gateway_id}`} />
                  <Badge value={relay.relayed_event_ids.length ? "SYNCED" : "QUEUED"} label={`RELAYED_${relay.relayed_event_ids.length}`} />
                  {relay.simulated && <Badge value="SIMULATED" label="MESH_SIM" />}
                  {relay.upstream && <Badge value={relay.upstream.sync_state} label={`UPSTREAM_${relay.upstream.sync_state}`} />}
                </div>
                <KV k="ATTEMPTED" v={relay.attempted} />
                <KV k="RELAYED" v={relay.relayed_event_ids.length} tone="#00FF66" />
                <KV k="FAILED" v={relay.failed.length} tone="#FF3B30" />
                <KV k="NEW EVENT IDS MINTED" v="0 (identity preserved)" tone="#00E5FF" />
                {relay.upstream && (
                  <>
                    <KV k="UPSTREAM ACCEPTED" v={relay.upstream.accepted_event_ids.length} />
                    <KV k="UPSTREAM DUPLICATES" v={relay.upstream.duplicate_event_ids.length} />
                  </>
                )}
                {relay.hops.slice(0, 8).map((h) => (
                  <div key={h.event_id} className="font-micro text-[9px] text-neutral-500">
                    · {h.event_id} via {h.transport} {h.delivered ? "DELIVERED" : `FAILED (${h.error})`} {h.latency_ms ? `${h.latency_ms}ms` : ""}
                  </div>
                ))}
              </div>
            </Panel>
          )}
        </div>
      </div>
    </Shell>
  );
}
