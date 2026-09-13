import axios from "axios";

const BASE = process.env.REACT_APP_BACKEND_URL ||
  (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1"
    ? "http://127.0.0.1:8011"
    : window.location.origin);
export const API = `${BASE.replace(/\/$/, "")}/api/v1`;

export const api = axios.create({ baseURL: API, withCredentials: true, timeout: 60000 });

let accessToken = localStorage.getItem("cros.token") || null;

export function setToken(t) {
  accessToken = t;
  if (t) localStorage.setItem("cros.token", t);
  else localStorage.removeItem("cros.token");
}
export function getToken() {
  return accessToken;
}

api.interceptors.request.use((config) => {
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
  return config;
});

export function apiError(e) {
  const d = e?.response?.data;
  if (d?.error?.message) return `${d.error.code}: ${d.error.message}`;
  if (typeof d?.detail === "string") return d.detail;
  if (Array.isArray(d?.detail)) return d.detail.map((x) => x.msg || JSON.stringify(x)).join(" ");
  if (e?.response?.status === 404) {
    return "LOGIN SERVICE UNAVAILABLE (404). Use the demo credentials shown above, then retry when the backend is online.";
  }
  if (e?.response?.status === 401) return "EMAIL OR PASSWORD IS INCORRECT. Try the demo credentials shown above.";
  return e?.message || "Request failed";
}

export function wsUrl(sinceSeq) {
  const base = BASE.replace(/^http/, "ws");
  const q = new URLSearchParams({ token: accessToken || "" });
  if (sinceSeq != null) q.set("since_seq", String(sinceSeq));
  return `${base}/api/v1/realtime?${q.toString()}`;
}
