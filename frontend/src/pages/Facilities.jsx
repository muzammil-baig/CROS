import React, { useCallback, useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, Stat, ago } from "../components/kit";
import { Shell } from "../components/Shell";
import { useLive } from "../context/LiveContext";

/** Shared facility coordinator view for hospitals (medical) and shelters. */
export default function Facilities({ kind }) {
  const isHospital = kind === "hospital";
  const path = isHospital ? "hospitals" : "shelters";
  const { snapshot } = useLive();
  const [items, setItems] = useState(null);
  const [note, setNote] = useState(null);
  const [matches, setMatches] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get(`/${path}`);
      setItems(data.items);
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  }, [path]);

  useEffect(() => {
    load();
  }, [load]);

  const op = async (facility, counterKind, operation, amount) => {
    try {
      const { data } = await api.patch(`/${path}/${facility.facility_id}/capacity`, {
        counter_kind: counterKind,
        operation,
        amount,
      });
      setNote({
        tone: "#00FF66",
        text: `CRDT ${operation.toUpperCase()} ${amount} ${counterKind} · OP ${data.operation_id} · ${
          data.idempotent ? "DUPLICATE (NO-OP)" : "APPLIED"
        }`,
      });
      load();
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  };

  const runMatch = async () => {
    try {
      const { data } = await api.post(`/${path}/match`, {
        location: { type: "Point", coordinates: [90.4, 23.78] },
        people_count: isHospital ? 1 : 25,
        required_specialty: isHospital ? "trauma" : null,
      });
      setMatches(data.matches);
    } catch (e) {
      setNote({ tone: "#FF3B30", text: apiError(e) });
    }
  };

  const medicalRequests = (snapshot?.requests || []).filter((r) => r.category === "medical");
  const evacRequests = (snapshot?.requests || []).filter((r) => r.category === "evacuation");

  if (!items) return <Shell><Loader label={`LOADING ${path.toUpperCase()}`} /></Shell>;

  return (
    <Shell>
      <div className="h-full overflow-y-auto p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h1 className="font-macro text-3xl text-white">
            {isHospital ? "MEDICAL COORDINATION" : "SHELTER COORDINATION"}
          </h1>
          <Btn variant="cyan" testid="btn-run-match" onClick={runMatch}>
            {isHospital ? "MATCH TRAUMA CAPACITY" : "MATCH EVACUATION CAPACITY"}
          </Btn>
        </div>

        {note && (
          <div className="panel px-3 py-2 font-micro text-[10px]" data-testid="facility-note" style={{ color: note.tone, borderColor: note.tone }}>
            {note.text}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <Stat label="FACILITIES" value={items.length} testid="stat-facilities" />
          <Stat
            label={isHospital ? "BEDS FREE" : "SPACES FREE"}
            value={items.reduce((a, f) => a + (isHospital ? f.availability.beds_available : f.availability.available), 0)}
            tone="#00FF66"
            testid="stat-free"
          />
          {isHospital && (
            <Stat label="ICU FREE" value={items.reduce((a, f) => a + f.availability.icu_available, 0)} tone="#FFB020" testid="stat-icu" />
          )}
          <Stat
            label={isHospital ? "MEDICAL REQUESTS" : "EVAC REQUESTS"}
            value={(isHospital ? medicalRequests : evacRequests).length}
            tone="#FF3B30"
            testid="stat-requests"
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <Panel title="CAPACITY · OPERATION-BASED CRDT COUNTERS" testid="panel-capacity">
            {items.map((f) => (
              <div key={f.facility_id} className="px-3 py-2 border-b border-line/70" data-testid={`facility-${f.facility_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-micro text-[10px] text-white">{f.name}</span>
                  <Badge value={f.status === "operational" ? "VERIFIED" : "DEGRADED"} label={f.status} />
                  <span className="ml-auto font-micro text-[9px] text-neutral-600">
                    {f.op_count || 0} OPS · {ago(f.counters_updated_at)}
                  </span>
                </div>
                <div className="font-micro text-[9px] text-neutral-500 mb-1">
                  {(f.specialties || []).join(" · ")}
                </div>
                {isHospital ? (
                  <>
                    <KV k="BEDS" v={`${f.availability.beds_available} free / ${f.availability.beds_total}`} tone={f.availability.beds_available ? "#00FF66" : "#FF3B30"} />
                    <KV k="ICU" v={`${f.availability.icu_available} free / ${f.availability.icu_total}`} />
                    <KV k="INCOMING" v={f.availability.incoming} />
                  </>
                ) : (
                  <>
                    <KV k="SPACE" v={`${f.availability.available} free / ${f.availability.capacity_total}`} tone={f.availability.available ? "#00FF66" : "#FF3B30"} />
                    <KV k="OCCUPANCY" v={`${f.availability.occupancy} (${f.availability.utilization_pct}%)`} />
                    <KV k="RESERVED" v={f.availability.reserved} />
                  </>
                )}
                <div className="flex flex-wrap gap-1 mt-2">
                  {(isHospital
                    ? [["occupied_beds", 1], ["occupied_icu_beds", 1], ["incoming_patients", 1]]
                    : [["occupancy", 10], ["reserved", 5]]
                  ).map(([ck, amt]) => (
                    <React.Fragment key={ck}>
                      <Btn testid={`btn-inc-${f.facility_id}-${ck}`} variant="amber" onClick={() => op(f, ck, "increment", amt)}>
                        +{amt} {ck.replace(/_/g, " ").toUpperCase()}
                      </Btn>
                      <Btn testid={`btn-dec-${f.facility_id}-${ck}`} variant="green" onClick={() => op(f, ck, "decrement", amt)}>
                        -{amt}
                      </Btn>
                    </React.Fragment>
                  ))}
                </div>
              </div>
            ))}
          </Panel>

          <div className="space-y-3">
            <Panel title={isHospital ? "MEDICAL REQUESTS" : "EVACUATION REQUESTS"} testid="panel-role-requests">
              {!(isHospital ? medicalRequests : evacRequests).length && <Empty label="NONE OPEN" />}
              {(isHospital ? medicalRequests : evacRequests).map((r) => (
                <Row key={r.request_id} testid={`role-request-${r.request_id}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge value={r.priority?.tier} label={`${r.priority?.tier} ${r.priority?.score}`} />
                    <Badge value={r.verification?.status} />
                    <span className="font-micro text-[9px] text-neutral-400">{r.description}</span>
                    <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(r.created_at)}</span>
                  </div>
                </Row>
              ))}
            </Panel>

            {matches && (
              <Panel title="DETERMINISTIC MATCH RESULT" testid="panel-matches">
                {matches.map((m) => (
                  <Row key={m.facility_id} testid={`match-${m.facility_id}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge value={m.sufficient ? "VERIFIED" : "CONTRADICTORY"} label={m.sufficient ? "SUFFICIENT" : "INSUFFICIENT"} />
                      <span className="font-micro text-[10px] text-white">{m.name}</span>
                      <span className="ml-auto font-micro text-[9px] text-neutral-500">
                        ETA {m.eta_seconds ? Math.round(m.eta_seconds / 60) : "—"}m · {(m.distance_m / 1000).toFixed(1)}km · {m.route_status}
                      </span>
                    </div>
                  </Row>
                ))}
              </Panel>
            )}

            {isHospital && (
              <Panel title="AMBULANCE / ASSET AVAILABILITY" testid="panel-assets">
                {(snapshot?.resources || [])
                  .filter((r) => ["ambulance", "helicopter"].includes(r.kind))
                  .map((r) => (
                    <Row key={r.resource_id} testid={`asset-${r.resource_id}`}>
                      <div className="flex items-center gap-2">
                        <Badge value={r.status} label={r.status} />
                        <span className="font-micro text-[10px] text-white">{r.label}</span>
                        <span className="ml-auto font-micro text-[9px] text-neutral-500">
                          {r.kind} · CAP {r.capacity}
                        </span>
                      </div>
                    </Row>
                  ))}
              </Panel>
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}
