import React, { useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, ago } from "../components/kit";
import { Shell } from "../components/Shell";
import TacticalMap from "../components/TacticalMap";
import { useAuth } from "../context/AuthContext";
import { useLive } from "../context/LiveContext";
import {
  appendLocalEvent,
  buildEnvelope,
  localProjections,
  outboundQueue,
} from "../lib/localstore";

const ACTIONS = [
  { key: "rescue", label: "NEED RESCUE", color: "#FF3B30", category: "rescue" },
  { key: "medical", label: "MEDICAL HELP", color: "#FF4500", category: "medical" },
  { key: "trapped", label: "PERSON TRAPPED", color: "#FF3B30", category: "person_trapped" },
  { key: "hazard", label: "REPORT HAZARD", color: "#FFB020", category: "hazard" },
  { key: "safe", label: "I AM SAFE", color: "#00FF66", category: "safe_report" },
];

export default function Citizen() {
  const { user } = useAuth();
  const { browserOnline, connectivity, syncNow, localSync } = useLive();
  const [selected, setSelected] = useState(null);
  const [desc, setDesc] = useState("");
  const [people, setPeople] = useState(1);
  const [vulns, setVulns] = useState([]);
  const [coords, setCoords] = useState([90.4, 23.78]);
  const [mine, setMine] = useState([]);
  const [local, setLocal] = useState(localProjections());
  const [evac, setEvac] = useState(null);
  const [msg, setMsg] = useState(null);
  const [forceOffline, setForceOffline] = useState(false);
  const [busy, setBusy] = useState(false);

  const loadMine = async () => {
    try {
      const { data } = await api.get("/requests/mine");
      setMine(data.items || []);
    } catch {
      /* offline: local projections still render */
    }
  };

  useEffect(() => {
    loadMine();
    navigator.geolocation?.getCurrentPosition(
      (p) => setCoords([p.coords.longitude, p.coords.latitude]),
      () => {}
    );
  }, []);

  useEffect(() => {
    api
      .get(`/citizen/evacuation-info?lat=${coords[1]}&lon=${coords[0]}`)
      .then(({ data }) => setEvac(data))
      .catch(() => {});
  }, [coords]);

  const offline = forceOffline || !browserOnline;

  const submit = async () => {
    if (!selected) return;
    setBusy(true);
    setMsg(null);
    const payload = {
      request_id: `REQ-LOCAL-${Date.now()}`,
      category: selected.category,
      description: desc || selected.label,
      location: { type: "Point", coordinates: coords },
      people_count: Number(people) || 1,
      vulnerabilities: vulns,
      water_rising: selected.category !== "safe_report",
      source_type: "citizen",
      reporter_name: user?.name,
    };
    if (offline) {
      const env = buildEnvelope({
        eventType: "RESCUE_REQUEST_CREATED",
        payload,
        actorId: user?.user_id,
        priority: ["rescue", "medical", "person_trapped"].includes(selected.category)
          ? "critical"
          : "normal",
      });
      appendLocalEvent(env);
      setLocal(localProjections());
      setMsg({
        tone: "#FFB020",
        text: `SAVED LOCALLY · EVENT ${env.event_id} QUEUED FOR MESH / SATELLITE RELAY`,
      });
      setBusy(false);
      setSelected(null);
      setDesc("");
      return;
    }
    try {
      const { data } = await api.post("/requests", {
        category: payload.category,
        description: payload.description,
        location: payload.location,
        people_count: payload.people_count,
        vulnerabilities: payload.vulnerabilities,
        water_rising: payload.water_rising,
        reporter_name: payload.reporter_name,
      });
      setMsg({
        tone: "#00FF66",
        text: `TRANSMITTED · REQUEST ${data.request_id} · PRIORITY ${
          data.pipeline?.priority?.tier?.toUpperCase() || "PENDING"
        } · VERIFICATION ${data.pipeline?.verification?.status || "PENDING"}`,
      });
      setSelected(null);
      setDesc("");
      loadMine();
    } catch (e) {
      const env = buildEnvelope({
        eventType: "RESCUE_REQUEST_CREATED",
        payload,
        actorId: user?.user_id,
        priority: "critical",
      });
      appendLocalEvent(env);
      setLocal(localProjections());
      setMsg({ tone: "#FFB020", text: `TRANSMIT FAILED (${apiError(e)}) · QUEUED LOCALLY` });
    }
    setBusy(false);
  };

  const queue = outboundQueue();

  return (
    <Shell>
      <div className="h-full overflow-y-auto">
        <div className="max-w-3xl mx-auto p-4 space-y-4">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <h1 className="font-macro text-3xl text-white">EMERGENCY</h1>
            <div className="flex items-center gap-2">
              <Badge value={offline ? "LOCAL_ONLY" : connectivity} testid="citizen-connectivity" />
              <Badge
                value={queue.length ? "QUEUED" : localSync.sync_state}
                label={queue.length ? `QUEUED_${queue.length}` : localSync.sync_state}
                testid="citizen-sync"
              />
              <Btn
                variant={forceOffline ? "amber" : "ghost"}
                testid="btn-toggle-offline"
                onClick={() => setForceOffline((v) => !v)}
              >
                {forceOffline ? "OFFLINE MODE ON" : "SIMULATE OFFLINE"}
              </Btn>
            </div>
          </div>

          {msg && (
            <div
              data-testid="citizen-message"
              className="panel px-3 py-2 font-micro text-[10px]"
              style={{ color: msg.tone, borderColor: msg.tone }}
            >
              {msg.text}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {ACTIONS.map((a) => (
              <button
                key={a.key}
                data-testid={`btn-citizen-${a.key}`}
                onClick={() => setSelected(a)}
                className="h-16 font-macro text-lg border-2 transition-colors duration-75"
                style={{
                  background: selected?.key === a.key ? a.color : "transparent",
                  color: selected?.key === a.key ? "#0A0A0A" : a.color,
                  borderColor: a.color,
                }}
              >
                {a.label}
              </button>
            ))}
            <button
              data-testid="btn-citizen-evacuation"
              onClick={() => document.getElementById("evac-panel")?.scrollIntoView({ behavior: "smooth" })}
              className="h-16 font-macro text-lg border-2 border-signal-cyan text-signal-cyan hover:bg-signal-cyan hover:text-black transition-colors duration-75"
            >
              EVACUATION INFO
            </button>
          </div>

          {selected && (
            <Panel title={`REPORT · ${selected.label}`} testid="citizen-form">
              <div className="p-3 space-y-3">
                <textarea
                  data-testid="input-description"
                  value={desc}
                  onChange={(e) => setDesc(e.target.value)}
                  placeholder="What is happening? Where exactly are you?"
                  rows={3}
                  className="w-full bg-void border border-line px-2 py-2 text-sm text-white outline-none focus:border-signal-cyan"
                />
                <div className="grid grid-cols-2 gap-3">
                  <label>
                    <span className="font-micro text-[9px] text-neutral-500">PEOPLE</span>
                    <input
                      data-testid="input-people"
                      type="number"
                      min="0"
                      value={people}
                      onChange={(e) => setPeople(e.target.value)}
                      className="w-full mt-1 bg-void border border-line px-2 py-1.5 font-micro text-[11px] text-white"
                    />
                  </label>
                  <div>
                    <span className="font-micro text-[9px] text-neutral-500">LOCATION</span>
                    <div className="font-micro text-[10px] text-signal-cyan mt-2">
                      {coords[1].toFixed(4)}, {coords[0].toFixed(4)}
                    </div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1">
                  {["infant", "child", "elderly", "disabled", "pregnant", "injured", "unconscious"].map(
                    (v) => (
                      <button
                        key={v}
                        data-testid={`chip-${v}`}
                        onClick={() =>
                          setVulns((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]))
                        }
                        className={`font-micro text-[9px] px-2 py-1 border ${
                          vulns.includes(v)
                            ? "bg-signal-amber text-black border-signal-amber"
                            : "text-neutral-400 border-line"
                        }`}
                      >
                        {v.toUpperCase()}
                      </button>
                    )
                  )}
                </div>
                <Btn
                  variant="red"
                  testid="btn-submit-request"
                  disabled={busy}
                  onClick={submit}
                  className="w-full py-3 text-sm"
                >
                  {busy ? "[///...] SENDING" : offline ? "SAVE + QUEUE LOCALLY" : "SEND EMERGENCY REQUEST"}
                </Btn>
              </div>
            </Panel>
          )}

          <Panel
            title="MY REQUESTS"
            testid="citizen-requests"
            right={
              <Btn testid="btn-citizen-sync" variant="cyan" onClick={async () => { await syncNow(); loadMine(); setLocal(localProjections()); }}>
                SYNC NOW
              </Btn>
            }
          >
            {local.filter((l) => l.local_only).map((l) => (
              <Row key={l.event_id} testid={`local-request-${l.event_id}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="font-micro text-[10px] text-white">
                    {(l.category || "").toUpperCase()} · {l.description?.slice(0, 40)}
                  </span>
                  <Badge value="QUEUED" testid={`local-badge-${l.event_id}`} />
                </div>
                <div className="font-micro text-[9px] text-neutral-500">
                  LOCAL EVENT {l.event_id} · HLC {l.hlc} · NOT YET ACKNOWLEDGED BY CLOUD
                </div>
              </Row>
            ))}
            {mine.length === 0 && local.length === 0 && <Empty label="NO REQUESTS" />}
            {mine.map((r) => (
              <Row key={r.request_id} testid={`request-${r.request_id}`}>
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <span className="font-micro text-[10px] text-white">
                    {r.category.toUpperCase()} · {r.description?.slice(0, 44)}
                  </span>
                  <div className="flex gap-1">
                    <Badge value={r.verification?.status} />
                    <Badge value={r.priority?.tier} label={`P_${r.priority?.tier}`} />
                    <Badge value="SYNCED" label={(r.status || "").toUpperCase()} />
                  </div>
                </div>
                <div className="font-micro text-[9px] text-neutral-500">
                  {ago(r.created_at)} · SCORE {r.priority?.score ?? "—"}
                  {r.mission_id ? ` · MISSION ${r.mission_id}` : ""}
                </div>
              </Row>
            ))}
          </Panel>

          <div id="evac-panel">
            <Panel title="EVACUATION INFORMATION" testid="citizen-evacuation">
              {!evac ? (
                <Loader label="LOADING SHELTERS" />
              ) : (
                <div className="p-3 space-y-3">
                  <div className="h-64 border border-line">
                    <TacticalMap
                      hazards={evac.hazards}
                      requests={[]}
                      resources={[]}
                      facilities={[]}
                      gateways={[]}
                      showLayers={{ roads: false, resources: false, facilities: false, gateways: false }}
                    />
                  </div>
                  {evac.shelters.slice(0, 5).map((s) => (
                    <div key={s.facility_id} className="border border-line px-2 py-1.5">
                      <div className="flex justify-between">
                        <span className="font-micro text-[10px] text-white">{s.name}</span>
                        <Badge
                          value={s.available > 0 ? "VERIFIED" : "CONTRADICTORY"}
                          label={s.available > 0 ? "SPACE_AVAILABLE" : "FULL"}
                        />
                      </div>
                      <KV k="DISTANCE" v={`${(s.distance_m / 1000).toFixed(2)} km`} />
                      <KV k="SPACE" v={`${s.available} / ${s.capacity_total}`} />
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>
        </div>
      </div>
    </Shell>
  );
}
