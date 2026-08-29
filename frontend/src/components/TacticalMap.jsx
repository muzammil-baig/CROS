import React, { useMemo, useState } from "react";
import { color } from "./kit";

const AREA = { minLon: 90.355, maxLon: 90.465, minLat: 23.715, maxLat: 23.825 };
const W = 1000;
const H = 1000;

function project([lon, lat]) {
  const x = ((lon - AREA.minLon) / (AREA.maxLon - AREA.minLon)) * W;
  const y = H - ((lat - AREA.minLat) / (AREA.maxLat - AREA.minLat)) * H;
  return [x, y];
}

const KIND_GLYPH = {
  boat: "◆",
  ambulance: "▲",
  helicopter: "✚",
  team: "■",
  truck: "▮",
};

export default function TacticalMap({
  hazards = [],
  requests = [],
  resources = [],
  facilities = [],
  gateways = [],
  routes = [],
  roadGraph = null,
  onSelect,
  selectedId,
  showLayers = {},
}) {
  const [hover, setHover] = useState(null);
  const layers = {
    hazards: true,
    requests: true,
    resources: true,
    facilities: true,
    gateways: true,
    routes: true,
    roads: true,
    ...showLayers,
  };

  const grid = useMemo(() => {
    const lines = [];
    for (let i = 0; i <= 10; i++) {
      lines.push({ x1: (i * W) / 10, y1: 0, x2: (i * W) / 10, y2: H });
      lines.push({ x1: 0, y1: (i * H) / 10, x2: W, y2: (i * H) / 10 });
    }
    return lines;
  }, []);

  return (
    <div className="relative w-full h-full bg-void grain overflow-hidden" data-testid="tactical-map">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-full" preserveAspectRatio="xMidYMid slice">
        {grid.map((l, i) => (
          <line key={i} {...l} stroke="#1b1b1b" strokeWidth="1" />
        ))}

        {layers.roads &&
          roadGraph?.edges?.map((e) => {
            const ov = roadGraph.overlay?.[e.edge_id];
            const [a, b] = e.geometry.coordinates.map(project);
            const stroke = ov ? (ov.blocked ? "#FF3B30" : "#FFB020") : "#2f2f2f";
            return (
              <line
                key={e.edge_id}
                x1={a[0]}
                y1={a[1]}
                x2={b[0]}
                y2={b[1]}
                stroke={stroke}
                strokeWidth={e.road_class === "arterial" ? 2.5 : e.road_class === "bridge" ? 3 : 1.2}
                strokeDasharray={ov?.blocked ? "6 4" : undefined}
              />
            );
          })}

        {layers.hazards &&
          hazards.map((h) => {
            if (h.geometry?.type !== "Polygon") return null;
            const pts = h.geometry.coordinates[0].map(project).map((p) => p.join(",")).join(" ");
            const predicted = (h.verification?.status || "") === "PREDICTED";
            const c = h.hazard_type === "flood" ? "#00E5FF" : "#FF4500";
            return (
              <polygon
                key={h.hazard_id}
                points={pts}
                fill={c}
                fillOpacity={0.1 + 0.22 * (h.severity || 0.4)}
                stroke={c}
                strokeWidth="1.5"
                strokeDasharray={predicted ? "8 5" : undefined}
                onMouseEnter={() =>
                  setHover({
                    title: `${h.hazard_type.toUpperCase()} · sev ${h.severity}`,
                    lines: [
                      h.description,
                      `WATER ${h.water_level_m ?? "—"} m`,
                      `${h.verification?.status || "UNVERIFIED"}`,
                    ],
                  })
                }
                onMouseLeave={() => setHover(null)}
                onClick={() => onSelect?.({ type: "hazard", id: h.hazard_id, doc: h })}
                style={{ cursor: "pointer" }}
              />
            );
          })}

        {layers.routes &&
          routes.map((r, i) =>
            r?.geometry?.coordinates ? (
              <polyline
                key={i}
                points={r.geometry.coordinates.map(project).map((p) => p.join(",")).join(" ")}
                fill="none"
                stroke={r.status === "DEGRADED_ROUTE" ? "#FFB020" : "#00E5FF"}
                strokeWidth="3"
                strokeDasharray="10 6"
              />
            ) : null
          )}

        {layers.facilities &&
          facilities.map((f) => {
            const [x, y] = project(f.location.coordinates);
            const isHospital = f.facility_type === "hospital";
            return (
              <g
                key={f.facility_id}
                onClick={() => onSelect?.({ type: "facility", id: f.facility_id, doc: f })}
                onMouseEnter={() =>
                  setHover({
                    title: f.name,
                    lines: [
                      isHospital
                        ? `BEDS ${f.availability?.beds_available}/${f.availability?.beds_total}`
                        : `SPACE ${f.availability?.available}/${f.availability?.capacity_total}`,
                      (f.specialties || []).join(", "),
                    ],
                  })
                }
                onMouseLeave={() => setHover(null)}
                style={{ cursor: "pointer" }}
              >
                <rect
                  x={x - 7}
                  y={y - 7}
                  width="14"
                  height="14"
                  fill="#0A0A0A"
                  stroke={isHospital ? "#FF3B30" : "#00FF66"}
                  strokeWidth="2"
                />
                <text
                  x={x}
                  y={y + 4}
                  fontSize="11"
                  textAnchor="middle"
                  fill={isHospital ? "#FF3B30" : "#00FF66"}
                  fontFamily="JetBrains Mono"
                >
                  {isHospital ? "H" : "S"}
                </text>
              </g>
            );
          })}

        {layers.gateways &&
          gateways.map((g) => {
            const [x, y] = project(g.location.coordinates);
            const c = color(g.status);
            return (
              <g
                key={g.gateway_id}
                onClick={() => onSelect?.({ type: "gateway", id: g.gateway_id, doc: g })}
                onMouseEnter={() =>
                  setHover({ title: g.name, lines: [g.gateway_id, g.connectivity_state] })
                }
                onMouseLeave={() => setHover(null)}
                style={{ cursor: "pointer" }}
              >
                <circle cx={x} cy={y} r="12" fill="none" stroke={c} strokeWidth="1" opacity="0.5" />
                <path d={`M${x - 6},${y + 5} L${x},${y - 6} L${x + 6},${y + 5} Z`} fill={c} />
              </g>
            );
          })}

        {layers.resources &&
          resources.map((r) => {
            if (!r.location?.coordinates) return null;
            const [x, y] = project(r.location.coordinates);
            const c = r.status === "available" ? "#00FF66" : r.status === "assigned" ? "#FFB020" : "#8A8A8A";
            return (
              <g
                key={r.resource_id}
                onClick={() => onSelect?.({ type: "resource", id: r.resource_id, doc: r })}
                onMouseEnter={() =>
                  setHover({ title: `${r.label} · ${r.kind}`, lines: [r.status, `CAP ${r.capacity}`] })
                }
                onMouseLeave={() => setHover(null)}
                style={{ cursor: "pointer" }}
              >
                <text x={x} y={y} fontSize="16" textAnchor="middle" fill={c}>
                  {KIND_GLYPH[r.kind] || "●"}
                </text>
              </g>
            );
          })}

        {layers.requests &&
          requests.map((q) => {
            if (!q.location?.coordinates) return null;
            const [x, y] = project(q.location.coordinates);
            const tier = q.priority?.tier || "normal";
            const c = color(tier === "critical" ? "critical" : tier);
            const sel = selectedId === q.request_id;
            return (
              <g
                key={q.request_id}
                onClick={() => onSelect?.({ type: "request", id: q.request_id, doc: q })}
                onMouseEnter={() =>
                  setHover({
                    title: `${q.category} · ${tier}`,
                    lines: [
                      q.description?.slice(0, 70),
                      `${q.verification?.status} ${Math.round((q.verification?.confidence || 0) * 100)}%`,
                      `SCORE ${q.priority?.score ?? "—"}`,
                    ],
                  })
                }
                onMouseLeave={() => setHover(null)}
                style={{ cursor: "pointer" }}
              >
                {tier === "critical" && (
                  <circle cx={x} cy={y} r="14" fill="none" stroke={c} strokeWidth="1" className="pulse-signal" />
                )}
                <circle cx={x} cy={y} r={sel ? 8 : 5} fill={c} stroke="#0A0A0A" strokeWidth="1.5" />
                {sel && <circle cx={x} cy={y} r="16" fill="none" stroke="#00E5FF" strokeWidth="2" />}
              </g>
            );
          })}
      </svg>

      {hover && (
        <div className="absolute top-3 left-3 panel-elevated px-3 py-2 max-w-xs pointer-events-none">
          <div className="font-micro text-[10px] text-signal-cyan">{hover.title}</div>
          {hover.lines.filter(Boolean).map((l, i) => (
            <div key={i} className="font-micro text-[9px] text-neutral-400">
              {l}
            </div>
          ))}
        </div>
      )}

      <div className="absolute bottom-3 left-3 panel-elevated px-3 py-2">
        <div className="font-micro text-[9px] text-neutral-500 mb-1">LEGEND</div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
          {[
            ["● CRITICAL REQ", "#FF3B30"],
            ["● HIGH REQ", "#FF4500"],
            ["◆ BOAT / ▲ AMB", "#00FF66"],
            ["H HOSPITAL", "#FF3B30"],
            ["S SHELTER", "#00FF66"],
            ["▲ GATEWAY", "#00E5FF"],
            ["▬ BLOCKED ROAD", "#FF3B30"],
            ["▭ FLOOD ZONE", "#00E5FF"],
          ].map(([l, c]) => (
            <span key={l} className="font-micro text-[9px]" style={{ color: c }}>
              {l}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

export { project, AREA };
