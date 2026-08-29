import React, { useCallback, useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, ago } from "../components/kit";
import { Shell } from "../components/Shell";
import TacticalMap from "../components/TacticalMap";
import { useAuth } from "../context/AuthContext";
import { useLive } from "../context/LiveContext";
import { appendLocalEvent, buildEnvelope, outboundQueue, localProjections } from "../lib/localstore";

export default function Responder() {
  const { user } = useAuth();
  const { connectivity, syncNow, browserOnline } = useLive();
  const [me, setMe] = useState(null);
  const [hazards, setHazards] = useState([]);
  const [note, setNote] = useState(null);
  const [forceOffline, setForceOffline] = useState(false);
  const [local, setLocal] = useState(localProjections());

  const load = useCallback(async () => {
    try {
      const [{ data: mine }, { data: hz }] = await Promise.all([
        api.get("/responders/me"),
        api.get("/hazards"),
      ]);
      setMe(mine);
      setHazards(hz.items);
    } catch (e) {
      setNote({ tone: "#FFB020", text: `CLOUD UNREACHABLE · USING LOCAL STATE (${apiError(e)})` });
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const offline = forceOffline || !browserOnline;

  const command = async (mission, path, body, label) => {
    if (offline) {
      const eventType = {
        accept: "MISSION_ACCEPTED",
        decline: "MISSION_DECLINED",
        en_route: "MISSION_EN_ROUTE",
        on_scene: "MISSION_ON_SCENE",
        completed: "MISSION_COMPLETED",
        failed: "MISSION_FAILED",
      }[path] || "MISSION_ACCEPTED";
      const env = buildEnvelope({
        eventType,
        payload: {
          mission_id: mission.mission_id,
          emergency_request_id: mission.emergency_request_id,
          responder_user_id: user.user_id,
          previous_status: mission.status,
          new_status: {
            accept: "accepted_by_responder",
            decline: "declined",
            en_route: "en_route",
            on_scene: "on_scene",
            completed: "completed",
            failed: "failed",
          }[path],
          reason: body?.reason || null,
          outcome: body?.outcome || null,
        },
        actorId: user.user_id,
        priority: "critical",
      });
      appendLocalEvent(env);
      setLocal(localProjections());
      setNote({ tone: "#FFB020", text: `${label} SAVED LOCALLY · QUEUED (EVENT ${env.event_id})` });
      return;
    }
    try {
      const isStatus = ["en_route", "on_scene", "completed", "failed"].includes(path);
      if (isStatus) await api.post(`/missions/${mission.mission_id}/status`, { status: path, ...body });
      else await api.post(`/missions/${mission.mission_id}/${path}`, body || {});
      setNote({ tone: "#00FF66", text: `${label} CONFIRMED BY CLOUD` });
      load();
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  };

  const pushLocation = async () => {
    navigator.geolocation?.getCurrentPosition(async (p) => {
      try {
        await api.post("/responders/me/location", {
          location: { type: "Point", coordinates: [p.coords.longitude, p.coords.latitude] },
        });
        setNote({ tone: "#00FF66", text: "LOCATION UPDATED" });
        load();
      } catch (e) {
        setNote({ tone: "#FF3B30", text: apiError(e) });
      }
    }, () => setNote({ tone: "#FFB020", text: "GEOLOCATION UNAVAILABLE" }));
  };

  if (!me) return <Shell><Loader label="LOADING FIELD ASSIGNMENT" /></Shell>;

  const active = (me.missions || []).filter(
    (m) => !["completed", "failed", "rejected"].includes(m.status)
  );

  return (
    <Shell>
      <div className="h-full overflow-y-auto">
        <div className="max-w-4xl mx-auto p-4 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <h1 className="font-macro text-3xl text-white">FIELD RESPONDER</h1>
            <div className="flex items-center gap-2">
              <Badge value={offline ? "LOCAL_ONLY" : connectivity} testid="responder-connectivity" />
              <Badge
                value={outboundQueue().length ? "QUEUED" : "SYNCED"}
                label={outboundQueue().length ? `QUEUED_${outboundQueue().length}` : "SYNCED"}
                testid="responder-sync"
              />
              <Btn variant={forceOffline ? "amber" : "ghost"} testid="btn-responder-offline" onClick={() => setForceOffline((v) => !v)}>
                {forceOffline ? "OFFLINE ON" : "SIMULATE OFFLINE"}
              </Btn>
              <Btn variant="cyan" testid="btn-responder-sync" onClick={async () => { await syncNow(); load(); setLocal(localProjections()); }}>
                SYNC
              </Btn>
            </div>
          </div>

          {note && (
            <div className="panel px-3 py-2 font-micro text-[10px]" data-testid="responder-note" style={{ color: note.tone, borderColor: note.tone }}>
              {note.text}
            </div>
          )}

          <Panel
            title="STATUS"
            testid="panel-responder-status"
            right={<Btn testid="btn-push-location" onClick={pushLocation}>PUSH LOCATION</Btn>}
          >
            <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-2">
              <KV k="RESPONDER" v={me.responder?.name} />
              <KV k="AVAILABILITY" v={me.responder?.availability} />
              <KV k="ASSET" v={me.resource ? `${me.resource.label} (${me.resource.kind})` : "NONE"} />
              <KV k="LAST FIX" v={ago(me.responder?.last_location_at)} />
            </div>
            <div className="px-3 pb-3 flex gap-2">
              {["available", "busy", "off_duty"].map((a) => (
                <Btn
                  key={a}
                  testid={`btn-avail-${a}`}
                  variant={me.responder?.availability === a ? "green" : "ghost"}
                  onClick={async () => {
                    await api.patch("/responders/me/availability", { availability: a });
                    load();
                  }}
                >
                  {a.toUpperCase()}
                </Btn>
              ))}
            </div>
          </Panel>

          {local.filter((l) => l.local_only && l.mission_id).map((l) => (
            <div key={l.event_id} className="panel px-3 py-2" data-testid={`local-mission-event-${l.event_id}`}>
              <div className="flex items-center gap-2">
                <Badge value="QUEUED" />
                <span className="font-micro text-[10px] text-white">{l.event_type}</span>
                <span className="font-micro text-[9px] text-neutral-500 ml-auto">{l.event_id}</span>
              </div>
            </div>
          ))}

          {!active.length && <Empty label="NO ACTIVE MISSION" />}

          {active.map((m) => (
            <Panel key={m.mission_id} title={`MISSION ${m.mission_id}`} testid={`responder-mission-${m.mission_id}`}>
              <div className="p-3 space-y-3">
                <div className="flex flex-wrap gap-2 items-center">
                  <Badge value={m.status} label={m.status} testid={`mission-status-${m.mission_id}`} />
                  <Badge value={m.priority} label={`P_${m.priority}`} />
                  <Badge value={m.route?.status === "DEGRADED_ROUTE" ? "DEGRADED" : "CONNECTED"} label={m.route?.status || "NO_ROUTE"} />
                  <span className="font-micro text-[9px] text-neutral-500">
                    ETA {m.route?.eta_seconds ? `${Math.round(m.route.eta_seconds / 60)} MIN` : "—"} ·{" "}
                    {m.route?.distance_m ? `${(m.route.distance_m / 1000).toFixed(1)} KM` : ""}
                  </span>
                </div>
                <p className="text-sm text-neutral-200">{m.objective}</p>
                {m.request && (
                  <div className="border border-line p-2">
                    <div className="font-micro text-[9px] text-neutral-500">TASK DETAIL</div>
                    <KV k="CATEGORY" v={m.request.category} />
                    <KV k="PEOPLE" v={m.request.people_count} />
                    <KV k="VULNERABILITIES" v={(m.request.vulnerabilities || []).join(", ") || "none"} />
                    <KV k="VERIFICATION" v={m.request.verification?.status} />
                    <KV k="DESCRIPTION" v={m.request.description} />
                  </div>
                )}
                <div className="h-56 border border-line">
                  <TacticalMap
                    hazards={hazards}
                    requests={m.request ? [m.request] : []}
                    resources={me.resource ? [me.resource] : []}
                    routes={m.route ? [m.route] : []}
                    facilities={[]}
                    gateways={[]}
                    showLayers={{ roads: false, gateways: false, facilities: false }}
                  />
                </div>
                {(m.route?.hazard_exposure || []).length > 0 && (
                  <div className="border border-signal-amber p-2">
                    <div className="font-micro text-[9px] text-signal-amber">
                      ROUTE HAZARD EXPOSURE · {m.route.hazard_exposure.length} SEGMENTS
                    </div>
                    {m.route.hazard_exposure.slice(0, 4).map((h) => (
                      <div key={h.edge_id} className="font-micro text-[9px] text-neutral-400">
                        {h.edge_id} · SEV {h.severity} · WATER {h.water_level_m}m{h.blocked ? " · BLOCKED" : ""}
                      </div>
                    ))}
                  </div>
                )}
                <div className="flex flex-wrap gap-2">
                  {m.status === "dispatched" && (
                    <>
                      <Btn variant="green" testid={`btn-accept-${m.mission_id}`} onClick={() => command(m, "accept", {}, "ACCEPT")}>
                        ACCEPT
                      </Btn>
                      <Btn variant="red" testid={`btn-decline-${m.mission_id}`} onClick={() => command(m, "decline", { reason: "Asset committed elsewhere" }, "DECLINE")}>
                        DECLINE
                      </Btn>
                    </>
                  )}
                  {m.status === "accepted_by_responder" && (
                    <Btn variant="cyan" testid={`btn-enroute-${m.mission_id}`} onClick={() => command(m, "en_route", {}, "EN ROUTE")}>
                      EN ROUTE
                    </Btn>
                  )}
                  {m.status === "en_route" && (
                    <Btn variant="cyan" testid={`btn-onscene-${m.mission_id}`} onClick={() => command(m, "on_scene", {}, "ON SCENE")}>
                      ON SCENE
                    </Btn>
                  )}
                  {m.status === "on_scene" && (
                    <Btn variant="green" testid={`btn-complete-${m.mission_id}`} onClick={() => command(m, "completed", { outcome: { people_rescued: m.request?.people_count || 1 } }, "COMPLETE")}>
                      COMPLETE
                    </Btn>
                  )}
                  {!["proposed", "approved"].includes(m.status) && (
                    <Btn variant="red" testid={`btn-fail-${m.mission_id}`} onClick={() => command(m, "failed", { reason: "Route impassable, asset withdrawing" }, "FAIL")}>
                      REPORT FAILURE
                    </Btn>
                  )}
                  <Btn testid={`btn-recompute-${m.mission_id}`} onClick={async () => { await api.post(`/missions/${m.mission_id}/route/recompute`); load(); }}>
                    RECOMPUTE ROUTE
                  </Btn>
                </div>
                <div className="border-t border-line pt-2">
                  <div className="font-micro text-[9px] text-neutral-500">STAGE HISTORY</div>
                  {(m.history || []).map((h, i) => (
                    <div key={i} className="font-micro text-[9px] text-neutral-400">
                      · {h.status} @ {ago(h.at)} {h.reason ? `· ${h.reason}` : ""}
                    </div>
                  ))}
                </div>
              </div>
            </Panel>
          ))}

          <Panel title="HAZARD INFORMATION" testid="panel-responder-hazards">
            {hazards.slice(0, 8).map((h) => (
              <Row key={h.hazard_id} testid={`hazard-${h.hazard_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge value={h.verification?.status} />
                  <span className="font-micro text-[10px] text-white">{h.hazard_type.toUpperCase()}</span>
                  <span className="font-micro text-[9px] text-neutral-500">{h.description}</span>
                  <span className="ml-auto font-micro text-[9px] text-neutral-600">
                    SEV {h.severity} · {h.water_level_m ?? "—"}m {h.road_blocked ? "· ROAD BLOCKED" : ""}
                  </span>
                </div>
              </Row>
            ))}
          </Panel>

          <Panel title="REPORT NEW HAZARD" testid="panel-report-hazard">
            <div className="p-3">
              <Btn
                variant="amber"
                testid="btn-report-hazard"
                onClick={async () => {
                  navigator.geolocation?.getCurrentPosition(async (p) => {
                    const lon = p.coords.longitude, lat = p.coords.latitude;
                    try {
                      await api.post("/hazards", {
                        hazard_type: "flood",
                        geometry: { type: "Polygon", coordinates: [[[lon - 0.003, lat - 0.002], [lon + 0.003, lat - 0.002], [lon + 0.003, lat + 0.002], [lon - 0.003, lat + 0.002], [lon - 0.003, lat - 0.002]]] },
                        description: "Field responder observed rising water blocking access",
                        severity: 0.7,
                        water_level_m: 0.9,
                        rise_rate_m_per_hour: 0.2,
                        road_blocked: true,
                      });
                      setNote({ tone: "#00FF66", text: "HAZARD REPORTED · ROUTING OVERLAY UPDATED" });
                      load();
                    } catch (e) {
                      setNote({ tone: "#FF3B30", text: apiError(e) });
                    }
                  }, () => setNote({ tone: "#FFB020", text: "GEOLOCATION UNAVAILABLE" }));
                }}
              >
                REPORT FLOOD HAZARD AT MY LOCATION
              </Btn>
            </div>
          </Panel>
        </div>
      </div>
    </Shell>
  );
}
