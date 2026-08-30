/**
 * Real client-held device identity: Ed25519 keypair generated in the browser via
 * WebCrypto. The private key never leaves the device and is never sent to the
 * server; offline events are signed locally so they can be verified end-to-end.
 *
 * Falls back to server-delegated signing (SIMULATED_SIGNER) only when the
 * runtime has no Ed25519 support.
 */

const KEY_STORE = "cros.device.keypair";

function b64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  bytes.forEach((b) => {
    s += String.fromCharCode(b);
  });
  return btoa(s);
}

function fromB64(str) {
  const bin = atob(str);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Canonical JSON identical to Python json.dumps(sort_keys=True, separators=(',',':')). */
export function canonicalJson(value) {
  if (value === null || value === undefined) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

const SIGNED_FIELDS = [
  "event_id",
  "event_type",
  "schema_version",
  "origin_device_id",
  "origin_actor_id",
  "logical_timestamp",
  "wall_clock_timestamp",
  "causal_parent_ids",
  "priority",
  "ttl_seconds",
  "correlation_id",
  "payload",
];

export function signingPayload(envelope) {
  const subset = {};
  SIGNED_FIELDS.forEach((f) => {
    subset[f] = envelope[f] === undefined ? null : envelope[f];
  });
  return canonicalJson(subset);
}

export async function ed25519Supported() {
  try {
    const kp = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    return !!kp.privateKey;
  } catch {
    return false;
  }
}

/** Generate + persist a device keypair. Returns the SPKI public key (base64). */
export async function ensureKeypair() {
  const cached = localStorage.getItem(KEY_STORE);
  if (cached) return JSON.parse(cached);
  try {
    const kp = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    const priv = await crypto.subtle.exportKey("pkcs8", kp.privateKey);
    const pub = await crypto.subtle.exportKey("spki", kp.publicKey);
    const material = { private_pkcs8: b64(priv), public_spki: b64(pub), algorithm: "Ed25519" };
    localStorage.setItem(KEY_STORE, JSON.stringify(material));
    return material;
  } catch {
    return null;
  }
}

export function hasClientKey() {
  return !!localStorage.getItem(KEY_STORE);
}

export function clearKeypair() {
  localStorage.removeItem(KEY_STORE);
}

/** Sign an envelope in place with the client-held private key. */
export async function signEnvelope(envelope) {
  const material = JSON.parse(localStorage.getItem(KEY_STORE) || "null");
  if (!material) return null;
  try {
    const key = await crypto.subtle.importKey(
      "pkcs8",
      fromB64(material.private_pkcs8),
      { name: "Ed25519" },
      false,
      ["sign"]
    );
    const bytes = new TextEncoder().encode(signingPayload(envelope));
    const sig = await crypto.subtle.sign({ name: "Ed25519" }, key, bytes);
    return b64(sig);
  } catch {
    return null;
  }
}
