"""Deterministic information verification layer.

Computes corroboration / contradiction / freshness and a confidence score from
persisted provenance records. Never deletes contributing reports.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STALE_AFTER_SECONDS = 1800
DEDUPE_RADIUS_M = 120
DEDUPE_WINDOW_SECONDS = 1800

SOURCE_TRUST = {
    "field_responder": 0.95,
    "incident_commander": 0.98,
    "government_officer": 0.9,
    "gateway_operator": 0.85,
    "citizen": 0.6,
    "sensor": 0.9,
    "external_feed": 0.5,
    "simulator": 0.7,
}


def haversine_m(a, b) -> float:
    lon1, lat1 = a[0], a[1]
    lon2, lat2 = b[0], b[1]
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _age(iso: str) -> float:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return 0.0


def evaluate(primary_report: dict, related_reports: list[dict]) -> dict:
    """primary_report: the originating report. related_reports: nearby-in-space-and-time reports."""
    coords = (primary_report.get("location") or {}).get("coordinates", [0, 0])
    corroborating, contradicting = [], []

    for r in related_reports:
        if r.get("report_id") == primary_report.get("report_id"):
            continue
        rc = (r.get("location") or {}).get("coordinates")
        if not rc:
            continue
        dist = haversine_m(coords, rc)
        if dist > DEDUPE_RADIUS_M * 4:
            continue
        if _age(r.get("created_at", "")) > DEDUPE_WINDOW_SECONDS * 2:
            continue
        if r.get("contradicts") or r.get("assertion") == "no_emergency_present":
            contradicting.append(r["report_id"])
        else:
            corroborating.append(r["report_id"])

    base = SOURCE_TRUST.get(primary_report.get("source_type", "citizen"), 0.5)
    confidence = base
    confidence += min(0.3, 0.12 * len(corroborating))
    confidence -= min(0.5, 0.2 * len(contradicting))

    freshness = _age(primary_report.get("created_at", ""))
    stale = freshness > STALE_AFTER_SECONDS
    if stale:
        confidence -= 0.15
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    if contradicting and len(contradicting) >= len(corroborating):
        status = "CONTRADICTORY"
    elif stale:
        status = "STALE"
    elif primary_report.get("source_type") in ("field_responder", "incident_commander",
                                               "government_officer", "sensor"):
        status = "VERIFIED"
    elif corroborating:
        status = "CORROBORATED"
    else:
        status = "UNVERIFIED"

    return {
        "status": status,
        "confidence": confidence,
        "source_type": primary_report.get("source_type"),
        "origin_device_id": primary_report.get("device_id"),
        "corroborating_report_ids": corroborating,
        "contradicting_report_ids": contradicting,
        "freshness_seconds": int(freshness),
        "stale": stale,
        "algorithm": "deterministic_corroboration_v1",
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def find_duplicate(candidate: dict, existing: list[dict]) -> dict | None:
    """Spatio-temporal dedupe. Returns the existing request considered the same event."""
    coords = (candidate.get("location") or {}).get("coordinates")
    if not coords:
        return None
    best, best_dist = None, DEDUPE_RADIUS_M
    for e in existing:
        ec = (e.get("location") or {}).get("coordinates")
        if not ec or e.get("category") != candidate.get("category"):
            continue
        if _age(e.get("created_at", "")) > DEDUPE_WINDOW_SECONDS:
            continue
        d = haversine_m(coords, ec)
        if d <= best_dist:
            best, best_dist = e, d
    return best
