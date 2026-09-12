"""Edge / field gateway runtime (Raspberry-Pi-class reference platform).

Real local persistence with SQLite: local append-only event log, local
projections, outbound/inbound queues and sync metadata. The gateway operates
with no cloud connectivity and relays queued events when a transport window
appears.

NOTE: SpatiaLite extension binaries are unavailable in this container, so
geometry is stored as GeoJSON text and spatial predicates are evaluated with
Shapely (same semantics, no spatial index). Recorded as a known limitation.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..config import ROOT_DIR
from ..models import utcnow_iso

EDGE_DIR = ROOT_DIR / ".edge_nodes"
EDGE_DIR.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  logical_timestamp TEXT NOT NULL,
  wall_clock_timestamp TEXT NOT NULL,
  priority TEXT NOT NULL,
  origin_device_id TEXT,
  envelope TEXT NOT NULL,
  applied INTEGER DEFAULT 0,
  synced INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_local_events_hlc ON local_events(logical_timestamp);
CREATE TABLE IF NOT EXISTS outbound_queue (
  message_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  priority TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  queued_at TEXT NOT NULL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS inbound_queue (
  event_id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL,
  applied INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projections (
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  doc TEXT NOT NULL,
  hlc TEXT,
  PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS sync_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS device_registry_cache (
  device_id TEXT PRIMARY KEY,
  public_key TEXT,
  revoked INTEGER DEFAULT 0,
  cached_at TEXT
);
CREATE TABLE IF NOT EXISTS geometries (
  entity_id TEXT PRIMARY KEY,
  entity_type TEXT,
  geojson TEXT
);
"""


class EdgeNode:
    """A local edge/gateway runtime backed by its own SQLite file."""

    def __init__(self, gateway_id: str):
        self.gateway_id = gateway_id
        self.path = EDGE_DIR / f"{gateway_id}.sqlite"
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------ local event log
    def append_event(self, envelope: dict) -> dict:
        cur = self.conn.execute("SELECT event_id FROM local_events WHERE event_id=?",
                                (envelope["event_id"],))
        if cur.fetchone():
            return {"status": "duplicate", "event_id": envelope["event_id"]}
        self.conn.execute(
            "INSERT INTO local_events (event_id,event_type,logical_timestamp,"
            "wall_clock_timestamp,priority,origin_device_id,envelope,applied,synced) "
            "VALUES (?,?,?,?,?,?,?,1,0)",
            (envelope["event_id"], envelope["event_type"], envelope["logical_timestamp"],
             envelope["wall_clock_timestamp"], str(envelope.get("priority", "normal")),
             envelope.get("origin_device_id"), json.dumps(envelope, default=str)))
        self.conn.execute(
            "INSERT OR IGNORE INTO outbound_queue (message_id,event_id,priority,state,"
            "queued_at) VALUES (?,?,?,'QUEUED',?)",
            (f"M-{envelope['event_id']}", envelope["event_id"],
             str(envelope.get("priority", "normal")), utcnow_iso()))
        self.apply_local_projection(envelope)
        self.conn.commit()
        return {"status": "applied", "event_id": envelope["event_id"]}

    def apply_local_projection(self, envelope: dict):
        p = envelope.get("payload") or {}
        for key, etype in (("request_id", "emergency_request"), ("mission_id", "mission"),
                           ("hazard_id", "hazard"), ("incident_id", "incident")):
            if p.get(key):
                eid = p[key]
                row = self.conn.execute(
                    "SELECT doc FROM projections WHERE entity_type=? AND entity_id=?",
                    (etype, eid)).fetchone()
                doc = json.loads(row["doc"]) if row else {}
                doc.update({k: v for k, v in p.items() if k != "recommendation"})
                doc["last_event_id"] = envelope["event_id"]
                doc["hlc"] = envelope["logical_timestamp"]
                self.conn.execute(
                    "INSERT INTO projections (entity_type,entity_id,doc,hlc) VALUES (?,?,?,?) "
                    "ON CONFLICT(entity_type,entity_id) DO UPDATE SET doc=excluded.doc,"
                    "hlc=excluded.hlc",
                    (etype, eid, json.dumps(doc, default=str), envelope["logical_timestamp"]))
                if p.get("geometry"):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO geometries (entity_id,entity_type,geojson) "
                        "VALUES (?,?,?)", (eid, etype, json.dumps(p["geometry"])))
                break

    # ----------------------------------------------------------- queues
    def outbound(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT o.*, e.envelope FROM outbound_queue o JOIN local_events e "
            "ON e.event_id=o.event_id WHERE o.state IN ('QUEUED','FAILED') "
            "ORDER BY CASE o.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                            "WHEN 'normal' THEN 2 ELSE 3 END, o.queued_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "envelope": json.loads(r["envelope"])} for r in rows]

    def mark_synced(self, event_ids: list[str], state: str = "DELIVERED",
                    error: str | None = None):
        for eid in event_ids:
            self.conn.execute("UPDATE outbound_queue SET state=?, attempts=attempts+1,"
                              "last_error=? WHERE event_id=?", (state, error, eid))
            if state == "DELIVERED":
                self.conn.execute("UPDATE local_events SET synced=1 WHERE event_id=?", (eid,))
        self.conn.commit()

    def ingest_inbound(self, envelopes: list[dict]) -> dict:
        applied, dupes = [], []
        for env in envelopes:
            row = self.conn.execute("SELECT event_id FROM inbound_queue WHERE event_id=?",
                                    (env["event_id"],)).fetchone()
            if row:
                dupes.append(env["event_id"])
                continue
            self.conn.execute("INSERT INTO inbound_queue (event_id,received_at,applied) "
                              "VALUES (?,?,1)", (env["event_id"], utcnow_iso()))
            self.append_event(env)
            applied.append(env["event_id"])
        self.conn.commit()
        return {"applied": applied, "duplicates": dupes}

    def known_event_ids(self, limit: int = 2000) -> list[str]:
        rows = self.conn.execute("SELECT event_id FROM local_events ORDER BY "
                                 "logical_timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [r["event_id"] for r in rows]

    def cache_devices(self, devices: list[dict]):
        for d in devices:
            self.conn.execute(
                "INSERT OR REPLACE INTO device_registry_cache (device_id,public_key,revoked,"
                "cached_at) VALUES (?,?,?,?)",
                (d["device_id"], d.get("public_key"), 1 if d.get("revoked") else 0,
                 utcnow_iso()))
        self.conn.commit()

    def set_meta(self, key: str, value):
        self.conn.execute("INSERT OR REPLACE INTO sync_meta (key,value) VALUES (?,?)",
                          (key, json.dumps(value, default=str)))
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def health(self) -> dict:
        def one(sql, *a):
            return self.conn.execute(sql, a).fetchone()[0]
        return {
            "gateway_id": self.gateway_id,
            "runtime": "sqlite_edge_node",
            "db_path": str(self.path),
            "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "local_events": one("SELECT COUNT(*) FROM local_events"),
            "unsynced_events": one("SELECT COUNT(*) FROM local_events WHERE synced=0"),
            "outbound_queued": one("SELECT COUNT(*) FROM outbound_queue WHERE state IN "
                                   "('QUEUED','FAILED')"),
            "inbound_processed": one("SELECT COUNT(*) FROM inbound_queue"),
            "local_projections": one("SELECT COUNT(*) FROM projections"),
            "cached_devices": one("SELECT COUNT(*) FROM device_registry_cache"),
            "cached_geometries": one("SELECT COUNT(*) FROM geometries"),
            "spatialite": False,
            "spatial_engine": "shapely_geojson",
            "last_sync": self.get_meta("last_sync"),
            "checked_at": utcnow_iso(),
        }


_nodes: dict[str, EdgeNode] = {}


def node(gateway_id: str) -> EdgeNode:
    if gateway_id not in _nodes:
        _nodes[gateway_id] = EdgeNode(gateway_id)
    return _nodes[gateway_id]


def all_nodes() -> list[str]:
    return sorted(p.stem for p in EDGE_DIR.glob("*.sqlite"))
