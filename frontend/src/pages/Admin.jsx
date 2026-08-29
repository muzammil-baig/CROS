import React, { useCallback, useEffect, useState } from "react";
import { api, apiError } from "../lib/api";
import { Badge, Btn, Empty, KV, Loader, Panel, Row, Stat, ago } from "../components/kit";
import { Shell } from "../components/Shell";

export default function Admin() {
  const [users, setUsers] = useState(null);
  const [devices, setDevices] = useState([]);
  const [orgs, setOrgs] = useState([]);
  const [health, setHealth] = useState(null);
  const [audit, setAudit] = useState([]);
  const [note, setNote] = useState(null);

  const load = useCallback(async () => {
    const results = await Promise.allSettled([
      api.get("/users"),
      api.get("/devices"),
      api.get("/organizations"),
      api.get("/system/health"),
      api.get("/audit?limit=80"),
    ]);
    const [u, d, o, h, a] = results;
    setUsers(u.status === "fulfilled" ? u.value.data.items : []);
    setDevices(d.status === "fulfilled" ? d.value.data.items : []);
    setOrgs(o.status === "fulfilled" ? o.value.data.items : []);
    setHealth(h.status === "fulfilled" ? h.value.data : null);
    setAudit(a.status === "fulfilled" ? a.value.data.items : []);
    const failed = results.filter((r) => r.status === "rejected");
    if (failed.length) {
      setNote({ tone: "#FFB020", text: `PARTIAL LOAD · ${failed.length} SUBSYSTEM(S) UNAVAILABLE: ${failed.map((f) => apiError(f.reason)).join(" | ")}` });
    }
  }, []);

  useEffect(() => {
    load();
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

  if (!users) return <Shell><Loader label="LOADING ADMIN CONSOLE" /></Shell>;

  return (
    <Shell>
      <div className="h-full overflow-y-auto p-4 space-y-3">
        <h1 className="font-macro text-3xl text-white">SYSTEM ADMINISTRATION</h1>
        <div className="panel px-3 py-2 font-micro text-[10px] text-signal-amber border-signal-amber">
          PLATFORM ADMINISTRATION DOES NOT GRANT OPERATIONAL AUTHORITY · MISSION APPROVAL REMAINS
          WITH THE INCIDENT COMMANDER
        </div>
        {note && (
          <div className="panel px-3 py-2 font-micro text-[10px]" data-testid="admin-note" style={{ color: note.tone, borderColor: note.tone }}>
            {note.text}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <Stat label="USERS" value={users.length} testid="stat-users" />
          <Stat label="DEVICES" value={devices.length} testid="stat-devices" />
          <Stat label="REVOKED" value={devices.filter((d) => d.revoked).length} tone="#FF3B30" testid="stat-revoked" />
          <Stat label="ORGANIZATIONS" value={orgs.length} testid="stat-orgs" />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <Panel title="USERS &amp; ROLES" testid="panel-users">
            {users.map((u) => (
              <Row key={u.user_id} testid={`user-${u.user_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-micro text-[10px] text-white">{u.email}</span>
                  <Badge value={u.disabled ? "block" : "pass"} label={u.role} />
                  <span className="ml-auto flex gap-1">
                    <Btn
                      testid={`btn-toggle-user-${u.user_id}`}
                      variant={u.disabled ? "green" : "amber"}
                      onClick={() => run(() => api.post(`/users/${u.user_id}/disable`, { disabled: !u.disabled }), `${u.email} ${u.disabled ? "ENABLED" : "DISABLED"}`)}
                    >
                      {u.disabled ? "ENABLE" : "DISABLE"}
                    </Btn>
                  </span>
                </div>
                <div className="font-micro text-[9px] text-neutral-600">
                  {u.org_id || "no org"} · {u.permissions?.length} PERMISSIONS
                </div>
              </Row>
            ))}
          </Panel>

          <Panel title="DEVICE IDENTITY &amp; TRUST" testid="panel-devices">
            {devices.map((d) => (
              <Row key={d.device_id} testid={`device-${d.device_id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge value={d.revoked ? "block" : "pass"} label={d.revoked ? "REVOKED" : d.trust_level} />
                  {d.simulated_signer && <Badge value="SIMULATED" label="SIMULATED_SIGNER" />}
                  <span className="font-micro text-[9px] text-neutral-300 break-all">{d.device_id}</span>
                  {!d.revoked && (
                    <Btn
                      className="ml-auto"
                      variant="red"
                      testid={`btn-revoke-${d.device_id}`}
                      onClick={() => run(() => api.post(`/devices/${d.device_id}/revoke`, { reason: "Administrative revocation from admin console" }), `DEVICE ${d.device_id} REVOKED · FUTURE EVENTS WILL BE REJECTED`)}
                    >
                      REVOKE
                    </Btn>
                  )}
                </div>
                <div className="font-micro text-[9px] text-neutral-600">
                  {d.device_type} · SIGNING {d.signing_mode} · PRIVATE KEY IN DB:{" "}
                  {String(d.private_key_stored_in_database)}
                </div>
              </Row>
            ))}
          </Panel>

          <Panel title="PLATFORM HEALTH" testid="panel-admin-health">
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

          <Panel title="SECURITY AUDIT" testid="panel-admin-audit">
            {audit
              .filter((a) => ["UNAUTHORIZED_ACTION_ATTEMPT", "SIGNATURE_VERIFICATION_FAILED", "DEVICE_CREDENTIAL_REVOKED", "DEVICE_REGISTERED", "LOGIN", "LOGIN_FAILED", "ROLE_CHANGED"].includes(a.action))
              .slice(0, 30)
              .map((a) => (
                <Row key={a.entry_id} testid={`sec-audit-${a.entry_id}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge value={a.outcome === "denied" ? "block" : "pass"} label={a.outcome} />
                    <span className="font-micro text-[10px] text-signal-cyan">{a.action}</span>
                    <span className="font-micro text-[9px] text-neutral-500 break-all">{a.entity_id}</span>
                    <span className="ml-auto font-micro text-[9px] text-neutral-600">{ago(a.created_at)}</span>
                  </div>
                </Row>
              ))}
            {!audit.length && <Empty label="NO AUDIT ENTRIES" />}
          </Panel>
        </div>
      </div>
    </Shell>
  );
}
