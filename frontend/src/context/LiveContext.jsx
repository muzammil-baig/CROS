import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, wsUrl, getToken } from "../lib/api";
import {
  knownEventIds,
  ingestRemoteEvents,
  markDelivered,
  markFailed,
  outboundQueue,
  localLog,
  setSyncMeta,
  syncMeta,
  deviceId,
} from "../lib/localstore";
import { useAuth } from "./AuthContext";

const LiveContext = createContext(null);
export const useLive = () => useContext(LiveContext);

export function LiveProvider({ children }) {
  const { user, permissions } = useAuth();
  const [snapshot, setSnapshot] = useState(null);  const [wsState, setWsState] = useState("DISCONNECTED");
  const [lastEvent, setLastEvent] = useState(null);
  const [browserOnline, setBrowserOnline] = useState(navigator.onLine);
  const [localSync, setLocalSync] = useState(syncMeta());
  const seqRef = useRef(null);
  const wsRef = useRef(null);

  const refresh = useCallback(async () => {
    if (!permissions.includes("incident:read")) return null;
    try {
      const { data } = await api.get("/command-center");
      setSnapshot(data);
      return data;
    } catch {
      return null;
    }
  }, [permissions]);

  useEffect(() => {
    if (!user) return;
    refresh();
    const t = setInterval(refresh, 20000);
    return () => clearInterval(t);
  }, [user, refresh]);

  // realtime with reconnect + missed-update replay
  useEffect(() => {
    if (!user || !getToken()) return;
    let closed = false;
    let retry;
    const connect = () => {
      setWsState("CONNECTING");
      const ws = new WebSocket(wsUrl(seqRef.current));
      wsRef.current = ws;
      ws.onopen = () => {
        setWsState("CONNECTED");
        ws.send(JSON.stringify({ subscribe: ["*"] }));
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.seq != null) seqRef.current = msg.seq;
        if (msg.topic === "hello" || msg.topic === "subscribed" || msg.topic === "pong") return;
        setLastEvent(msg);
        refresh();
      };
      ws.onclose = () => {
        setWsState("DISCONNECTED");
        if (!closed) retry = setTimeout(connect, 3000);
      };
      ws.onerror = () => setWsState("DEGRADED");
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      wsRef.current?.close();
    };
  }, [user, refresh]);

  useEffect(() => {
    const on = () => setBrowserOnline(true);
    const off = () => setBrowserOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  /** Push locally queued events upstream and pull anything we are missing. */
  const syncNow = useCallback(async () => {
    const queued = outboundQueue();
    setSyncMeta({ sync_state: "SYNCING", pending: queued.length });
    setLocalSync(syncMeta());
    const log = localLog();
    const envelopes = queued
      .map((m) => log.find((e) => e.event_id === m.event_id))
      .filter(Boolean);
    try {
      const { data } = await api.post("/sync/device", {
        device_id: deviceId(),
        known_event_ids: knownEventIds(),
        events: envelopes,
        max_events: 50,
        priority_floor: "low",
      });
      const ok = (data.ingested || [])
        .filter((i) => i.status === "applied" || i.status === "duplicate")
        .map((i) => i.event_id);
      const bad = (data.ingested || [])
        .filter((i) => i.status === "rejected")
        .map((i) => i.event_id);
      markDelivered(ok);
      if (bad.length) markFailed(bad, "rejected_by_server");
      ingestRemoteEvents(data.events || []);
      setSyncMeta({
        sync_state: data.more_available ? "SYNCING" : "SYNCED",
        last_sync: new Date().toISOString(),
        pending: outboundQueue().length,
      });
      setLocalSync(syncMeta());
      await refresh();
      return data;
    } catch (e) {
      setSyncMeta({ sync_state: "QUEUED", pending: outboundQueue().length });
      setLocalSync(syncMeta());
      return null;
    }
  }, [refresh]);

  useEffect(() => {
    if (!user) return;
    const t = setInterval(() => {
      if (browserOnline && outboundQueue().length) syncNow();
    }, 12000);
    return () => clearInterval(t);
  }, [user, browserOnline, syncNow]);

  const connectivity = !browserOnline
    ? "LOCAL_ONLY"
    : wsState === "CONNECTED"
    ? snapshot?.connectivity_state || "CONNECTED"
    : wsState === "CONNECTING"
    ? "DEGRADED"
    : "DEGRADED";

  return (
    <LiveContext.Provider
      value={{
        snapshot,
        refresh,
        wsState,
        lastEvent,
        connectivity,
        browserOnline,
        syncNow,
        localSync,
        setLocalSync,
      }}
    >
      {children}
    </LiveContext.Provider>
  );
}
