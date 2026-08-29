import React from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { Badge, Btn, ago } from "./kit";
import { useAuth } from "../context/AuthContext";
import { useLive } from "../context/LiveContext";
import { outboundQueue } from "../lib/localstore";

const NAV_BY_ROLE = {
  incident_commander: [["/command", "COMMAND"], ["/analysis", "ANALYSIS"]],
  government_officer: [["/command", "COMMAND"], ["/analysis", "ANALYSIS"]],
  medical_coordinator: [["/medical", "MEDICAL"], ["/command", "COMMAND"]],
  shelter_coordinator: [["/shelter", "SHELTER"], ["/command", "COMMAND"]],
  communications_operator: [["/comms", "COMMS"], ["/command", "COMMAND"]],
  field_gateway_operator: [["/gateway", "GATEWAY"]],
  analyst_planner: [["/analysis", "ANALYSIS"], ["/command", "COMMAND"]],
  system_administrator: [["/admin", "ADMIN"]],
  field_responder: [["/responder", "FIELD"]],
  citizen: [["/citizen", "EMERGENCY"]],
};

export function TopBar() {
  const { user, logout } = useAuth();
  const { connectivity, wsState, syncNow, localSync, snapshot } = useLive();
  const nav = useNavigate();
  const pending = outboundQueue().length;
  const links = NAV_BY_ROLE[user?.role] || [];

  return (
    <header className="min-h-12 shrink-0 bg-elevated border-b border-line flex flex-wrap items-center px-3 gap-x-3 gap-y-1 py-1.5">
      <div className="font-macro text-lg text-white">CROS</div>
      <span className="font-micro text-[9px] text-neutral-500 hidden xl:inline">
        CRISIS RESPONSE OS
      </span>

      <nav className="flex items-center gap-1">
        {links.map(([to, label]) => (
          <NavLink
            key={to}
            to={to}
            data-testid={`nav-${label.toLowerCase()}`}
            className={({ isActive }) =>
              `font-micro text-[10px] px-2 py-1 border ${
                isActive
                  ? "text-black bg-signal-cyan border-signal-cyan"
                  : "text-neutral-400 border-transparent hover:border-line"
              }`
            }
          >
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
        <Badge value={connectivity} testid="badge-connectivity" title="Live connectivity state" />
        <span className="hidden sm:inline">
          <Badge
            value={wsState === "CONNECTED" ? "SYNCED" : "DEGRADED"}
            label={`RT_${wsState}`}
            testid="badge-realtime"
          />
        </span>
        <Badge
          value={pending ? "QUEUED" : localSync.sync_state}
          label={pending ? `QUEUED_${pending}` : localSync.sync_state}
          testid="badge-sync"
          title={`Last local sync: ${ago(localSync.last_sync)}`}
        />
        {snapshot?.transports?.some((t) => t.simulated) && (
          <span className="hidden md:inline">
            <Badge value="SIMULATED" label="SAT_SIMULATED" testid="badge-simulated" />
          </span>
        )}
        <Btn variant="cyan" testid="btn-sync-now" onClick={syncNow}>
          SYNC
        </Btn>
        <span className="font-micro text-[10px] text-neutral-400 hidden xl:inline" data-testid="current-user">
          {user?.name} · {user?.role}
        </span>
        <Btn
          testid="btn-logout"
          onClick={async () => {
            await logout();
            nav("/login");
          }}
        >
          EXIT
        </Btn>
      </div>
    </header>
  );
}

export function Shell({ children }) {
  return (
    <div className="h-screen flex flex-col bg-void overflow-x-hidden">
      <TopBar />
      <main className="flex-1 min-h-0 overflow-hidden">{children}</main>
    </div>
  );
}
