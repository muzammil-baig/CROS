/**
 * Real local-first persistence for the field/citizen apps.
 *
 * Maintains, in browser storage that survives reload and disconnection:
 *   - a local append-only event log
 *   - local projections derived from that log
 *   - an outbound transmission queue
 *   - a local Hybrid Logical Clock
 *   - sync metadata (last sync, pending count, dedupe set)
 *
 * Offline actions create real signed-envelope-shaped events with a stable
 * event_id and HLC; the same identity is later transmitted to the cloud, so
 * duplicate delivery is a safe no-op.
 */

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
import { signEnvelope } from "./devicecrypto";
const K = {
  log: "cros.local.events",
  queue: "cros.local.queue",
  proj: "cros.local.projections",
  hlc: "cros.local.hlc",
  meta: "cros.local.meta",
  device: "cros.local.device",
};

function enc(value, len) {
  let v = BigInt(value);
  let out = "";
  for (let i = 0; i < len; i++) {
    out = ALPHABET[Number(v & 31n)] + out;
    v >>= 5n;
  }
  return out;
}

export function ulid() {
  const ms = BigInt(Date.now());
  let rand = 0n;
  const bytes = new Uint8Array(10);
  crypto.getRandomValues(bytes);
  bytes.forEach((b) => {
    rand = (rand << 8n) | BigInt(b);
  });
  return enc(ms, 10) + enc(rand, 16);
}

function read(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(v) : fallback;
  } catch {
    return fallback;
  }
}
function write(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

export function deviceId() {
  let d = localStorage.getItem(K.device);
  if (!d) {
    d = `DEV-LOCAL-${ulid()}`;
    localStorage.setItem(K.device, d);
  }
  return d;
}
export function setDeviceId(id) {
  localStorage.setItem(K.device, id);
}

/** Local Hybrid Logical Clock: physical.counter.node */
export function hlcNow(nodeId = deviceId()) {
  const state = read(K.hlc, { physical: 0, counter: 0 });
  const wall = Date.now();
  if (wall > state.physical) {
    state.physical = wall;
    state.counter = 0;
  } else {
    state.counter += 1;
  }
  write(K.hlc, state);
  return `${state.physical}.${state.counter}.${nodeId}`;
}

export function hlcMerge(remote) {
  const [rp, rc] = String(remote).split(".");
  const state = read(K.hlc, { physical: 0, counter: 0 });
  const newest = Math.max(Date.now(), state.physical, Number(rp) || 0);
  if (newest === state.physical && newest === Number(rp)) {
    state.counter = Math.max(state.counter, Number(rc) || 0) + 1;
  } else if (newest === state.physical) state.counter += 1;
  else if (newest === Number(rp)) state.counter = (Number(rc) || 0) + 1;
  else state.counter = 0;
  state.physical = newest;
  write(K.hlc, state);
}

export function buildEnvelope({ eventType, payload, actorId, priority = "normal", ttl = 172800 }) {  return {
    event_id: ulid(),
    event_type: eventType,
    schema_version: 1,
    origin_device_id: deviceId(),
    origin_actor_id: actorId || null,
    logical_timestamp: hlcNow(),
    wall_clock_timestamp: new Date().toISOString(),
    causal_parent_ids: [],
    priority,
    ttl_seconds: ttl,
    correlation_id: crypto.randomUUID(),
    signature: null,
    payload,
  };
}

export async function buildSignedEnvelope(args) {
  const env = buildEnvelope(args);
  const signature = await signEnvelope(env);
  if (signature) env.signature = signature;
  return env;
}

export function appendLocalEvent(envelope) {
  const log = read(K.log, []);
  if (log.some((e) => e.event_id === envelope.event_id)) {
    return { status: "duplicate", envelope };
  }
  log.push(envelope);
  write(K.log, log.slice(-500));
  const queue = read(K.queue, []);
  queue.push({
    message_id: `M-${envelope.event_id}`,
    event_id: envelope.event_id,
    priority: envelope.priority,
    state: "QUEUED",
    attempts: 0,
    queued_at: new Date().toISOString(),
  });
  write(K.queue, queue);
  applyLocalProjection(envelope);
  return { status: "applied", envelope };
}

export function applyLocalProjection(envelope, remote = false) {
  const proj = read(K.proj, {});
  const p = envelope.payload || {};
  const id = p.request_id || p.mission_id || p.hazard_id;
  if (!id) return;
  const prev = proj[id] || {};
  proj[id] = {
    ...prev,
    ...p,
    entity_type: p.request_id ? "emergency_request" : p.mission_id ? "mission" : "hazard",
    event_type: envelope.event_type,
    sync_state: remote ? "SYNCED" : prev.sync_state === "SYNCED" ? "SYNCED" : "QUEUED",
    local_only: remote ? false : prev.local_only !== false,
    hlc: envelope.logical_timestamp,
    created_at: envelope.wall_clock_timestamp,
    event_id: envelope.event_id,
  };
  write(K.proj, proj);
}

export function localProjections() {
  return Object.values(read(K.proj, {}));
}
export function localLog() {
  return read(K.log, []);
}
export function outboundQueue() {
  return read(K.queue, []).filter((m) => m.state === "QUEUED" || m.state === "FAILED");
}
export function knownEventIds() {
  return read(K.log, []).map((e) => e.event_id);
}

export function markDelivered(eventIds) {
  const queue = read(K.queue, []);
  const set = new Set(eventIds);
  queue.forEach((m) => {
    if (set.has(m.event_id)) m.state = "DELIVERED";
  });
  write(K.queue, queue);
  const proj = read(K.proj, {});
  Object.values(proj).forEach((d) => {
    if (set.has(d.event_id)) {
      d.sync_state = "SYNCED";
      d.local_only = false;
    }
  });
  write(K.proj, proj);
}

export function markFailed(eventIds, error) {
  const queue = read(K.queue, []);
  const set = new Set(eventIds);
  queue.forEach((m) => {
    if (set.has(m.event_id)) {
      m.state = "FAILED";
      m.attempts += 1;
      m.last_error = error;
    }
  });
  write(K.queue, queue);
}

export function syncMeta() {
  return read(K.meta, { last_sync: null, sync_state: "SYNCED", pending: 0 });
}
export function setSyncMeta(patch) {
  write(K.meta, { ...syncMeta(), ...patch });
}

export function ingestRemoteEvents(events) {
  let applied = 0;
  events.forEach((e) => {
    hlcMerge(e.logical_timestamp);
    const log = read(K.log, []);
    if (log.some((x) => x.event_id === e.event_id)) return;
    log.push(e);
    write(K.log, log.slice(-500));
    applyLocalProjection(e, true);
    applied += 1;
  });
  return applied;
}
