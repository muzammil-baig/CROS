import React from "react";

const SIGNAL = {
  CONNECTED: "#00FF66",
  SYNCED: "#00FF66",
  VERIFIED: "#00FF66",
  pass: "#00FF66",
  available: "#00FF66",
  online: "#00FF66",
  completed: "#00FF66",
  approved: "#00FF66",
  CORROBORATED: "#00E5FF",
  SYNCING: "#00E5FF",
  SATELLITE_BACKHAUL: "#00E5FF",
  MESH_ONLY: "#00E5FF",
  SIMULATED: "#00E5FF",
  PREDICTED: "#00E5FF",
  DEGRADED: "#FFB020",
  LOCAL_ONLY: "#FFB020",
  QUEUED: "#FFB020",
  STALE: "#FFB020",
  UNVERIFIED: "#FFB020",
  caution: "#FFB020",
  pending_approval: "#FFB020",
  dispatched: "#FFB020",
  ISOLATED: "#FF4500",
  CONFLICT: "#FF4500",
  CONTRADICTORY: "#FF3B30",
  block: "#FF3B30",
  blocked: "#FF3B30",
  critical: "#FF3B30",
  failed: "#FF3B30",
  rejected: "#FF3B30",
  offline: "#FF3B30",
  high: "#FF4500",
  normal: "#8A8A8A",
  low: "#8A8A8A",
};

export function color(value) {
  return SIGNAL[value] || "#8A8A8A";
}

export function Badge({ value, label, testid, title }) {
  const c = color(value);
  return (
    <span
      title={title}
      data-testid={testid}
      className="font-micro text-[10px] px-1.5 py-0.5 border inline-block whitespace-nowrap"
      style={{ color: c, borderColor: c, background: "transparent" }}
    >
      [{String(label ?? value ?? "—").replace(/_/g, "_")}]
    </span>
  );
}

export function Panel({ title, right, children, className = "", testid }) {
  return (
    <section data-testid={testid} className={`panel ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between px-3 py-2 border-b border-line bg-elevated">
          <h3 className="font-micro text-[11px] text-neutral-300">{title}</h3>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div>{children}</div>
    </section>
  );
}

export function Btn({ children, onClick, variant = "ghost", disabled, testid, className = "", type = "button" }) {
  const styles = {
    ghost: "text-neutral-200 border-line hover:bg-neutral-200 hover:text-black",
    cyan: "text-signal-cyan border-signal-cyan hover:bg-signal-cyan hover:text-black",
    green: "text-signal-green border-signal-green hover:bg-signal-green hover:text-black",
    red: "text-signal-red border-signal-red hover:bg-signal-red hover:text-black",
    amber: "text-signal-amber border-signal-amber hover:bg-signal-amber hover:text-black",
  }[variant];
  return (
    <button
      type={type}
      data-testid={testid}
      disabled={disabled}
      onClick={onClick}
      className={`cros-btn px-3 py-1.5 text-[11px] ${styles} disabled:opacity-35 disabled:cursor-not-allowed ${className}`}
    >
      {children}
    </button>
  );
}

export function Stat({ label, value, tone = "#EDEDED", testid }) {
  return (
    <div className="panel px-3 py-2" data-testid={testid}>
      <div className="font-micro text-[9px] text-neutral-500">{label}</div>
      <div className="font-macro text-2xl" style={{ color: tone }}>
        {value}
      </div>
    </div>
  );
}

export function Row({ children, onClick, active, testid }) {
  return (
    <div
      data-testid={testid}
      onClick={onClick}
      className={`px-3 py-2 border-b border-line/70 cursor-pointer transition-colors duration-75 ${
        active ? "bg-elevated" : "hover:bg-elevated"
      }`}
    >
      {children}
    </div>
  );
}

export function KV({ k, v, tone }) {
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="font-micro text-[10px] text-neutral-500">{k}</span>
      <span className="font-micro text-[10px] text-right" style={{ color: tone || "#DEDEDE" }}>
        {v ?? "—"}
      </span>
    </div>
  );
}

export function Loader({ label = "LOADING" }) {
  return (
    <div className="font-micro text-[11px] text-signal-cyan p-4 pulse-signal">[///...] {label}</div>
  );
}

export function Empty({ label }) {
  return <div className="font-micro text-[10px] text-neutral-600 p-4">— {label} —</div>;
}

export function ago(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
