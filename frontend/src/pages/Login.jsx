import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { Badge, Btn } from "../components/kit";

const DEMO = [
  ["cmd.rahman@cros.gov", "INCIDENT COMMANDER"],
  ["resp.khan@cros.gov", "FIELD RESPONDER"],
  ["citizen.hasan@example.com", "CITIZEN"],
  ["med.okafor@cros.gov", "MEDICAL COORDINATOR"],
  ["shelter.nadeem@cros.gov", "SHELTER COORDINATOR"],
  ["comms.silva@cros.gov", "COMMS OPERATOR"],
  ["gateway.tanaka@cros.gov", "GATEWAY OPERATOR"],
  ["analyst.novak@cros.gov", "ANALYST / PLANNER"],
  ["officer.mbeki@cros.gov", "GOVERNMENT OFFICER"],
];

const HOME = {
  citizen: "/citizen",
  field_responder: "/responder",
  medical_coordinator: "/medical",
  shelter_coordinator: "/shelter",
  communications_operator: "/comms",
  field_gateway_operator: "/gateway",
  analyst_planner: "/analysis",
  system_administrator: "/admin",
};

export default function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("cmd.rahman@cros.gov");
  const [password, setPassword] = useState("CrosDemo!2026");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const r = await login(email.trim(), password);
    setBusy(false);
    if (!r.ok) return setError(r.error);
    nav(HOME[r.user.role] || "/command");
  };

  return (
    <div className="min-h-screen bg-void grain relative flex">
      <div className="hidden lg:flex flex-col justify-between w-[52%] border-r border-line p-14">
        <div>
          <div className="font-macro text-6xl xl:text-7xl text-white leading-none">
            CRISIS
            <br />
            RESPONSE
            <br />
            <span className="text-signal-cyan">OS</span>
          </div>
          <p className="font-micro text-[11px] text-neutral-500 mt-6 max-w-md leading-relaxed">
            OFFLINE-FIRST DISASTER COORDINATION · FLOOD RESPONSE MODULE · EVENT-SOURCED
            COMMAND, MESH / SATELLITE BACKHAUL, HUMAN-APPROVED AI RECOMMENDATIONS
          </p>
        </div>
        <div className="space-y-2">
          <div className="font-micro text-[9px] text-neutral-600">SUBSYSTEM STATUS</div>
          <div className="flex flex-wrap gap-2">
            <Badge value="CONNECTED" label="EVENT_BUS" />
            <Badge value="SYNCED" label="PROJECTIONS" />
            <Badge value="SIMULATED" label="SATELLITE_SIMULATED" />
            <Badge value="SIMULATED" label="RADIO_SIMULATED" />
            <Badge value="SIMULATED" label="SMS_SIMULATED" />
            <Badge value="DEGRADED" label="HITL_APPROVAL_REQUIRED" />
          </div>
        </div>
      </div>

      <div className="flex-1 flex items-center justify-center p-6">
        <div className="w-full max-w-sm">
          <h1 className="font-macro text-3xl text-white mb-1">OPERATOR SIGN-IN</h1>
          <p className="font-micro text-[10px] text-neutral-500 mb-8">
            AUTHORIZATION IS ENFORCED SERVER-SIDE
          </p>
          <form onSubmit={submit} className="space-y-3">
            <label className="block">
              <span className="font-micro text-[9px] text-neutral-500">EMAIL</span>
              <input
                data-testid="input-email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full mt-1 bg-surface border border-line px-3 py-2 font-micro text-[11px] text-white outline-none focus:border-signal-cyan"
              />
            </label>
            <label className="block">
              <span className="font-micro text-[9px] text-neutral-500">PASSWORD</span>
              <input
                data-testid="input-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full mt-1 bg-surface border border-line px-3 py-2 font-micro text-[11px] text-white outline-none focus:border-signal-cyan"
              />
            </label>
            {error && (
              <div
                data-testid="login-error"
                className="font-micro text-[10px] text-signal-red border border-signal-red px-2 py-1.5"
              >
                {error}
              </div>
            )}
            <Btn variant="cyan" testid="btn-login" type="submit" disabled={busy} className="w-full py-2.5">
              {busy ? "[///...] AUTHENTICATING" : "AUTHENTICATE"}
            </Btn>
          </form>

          <div className="mt-8 border-t border-line pt-4">
            <div className="font-micro text-[9px] text-neutral-600 mb-2">
              DEMO ROLES · PASSWORD CrosDemo!2026
            </div>
            <div className="space-y-1">
              {DEMO.map(([em, role]) => (
                <button
                  key={em}
                  data-testid={`demo-${role.split(" ")[0].toLowerCase()}`}
                  onClick={() => {
                    setEmail(em);
                    setPassword("CrosDemo!2026");
                  }}
                  className="w-full text-left font-micro text-[9px] text-neutral-500 hover:text-signal-cyan"
                >
                  {role} · {em}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
