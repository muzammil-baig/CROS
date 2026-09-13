"""Deterministic information verification layer.

Computes corroboration / contradiction / freshness and a confidence score from
persisted provenance records. Never deletes contributing reports.
"""
from __future__ import annotations

import math
import re
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

    uncertainty = []
    if not corroborating:
        uncertainty.append("No independent corroborating report in the spatial-temporal window")
    if not contradicting and status not in ("VERIFIED", "CORROBORATED"):
        uncertainty.append("Source confidence or freshness is insufficient for verification")
    return {
        "status": status,
        "confidence": confidence,
        "uncertainty": uncertainty,
        "evidence": {
            "primary_report_id": primary_report.get("report_id"),
            "source_trust": base,
            "spatial_radius_m": DEDUPE_RADIUS_M * 4,
            "temporal_window_seconds": DEDUPE_WINDOW_SECONDS * 2,
        },
        "source_type": primary_report.get("source_type"),
        "origin_device_id": primary_report.get("device_id"),
        "corroborating_report_ids": corroborating,
        "contradicting_report_ids": contradicting,
        "freshness_seconds": int(freshness),
        "stale": stale,
        "algorithm": "deterministic_corroboration_v1",
        "schema_version": "verification-v1",
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _tokens(value: object) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) > 2}


def semantic_fingerprint(request: dict) -> tuple:
    """Stable business identity for semantic dedupe, excluding event/request IDs."""
    location = request.get("location") or {}
    coords = location.get("coordinates") or []
    rounded_coords = tuple(round(float(value), 5) for value in coords[:2])
    return (
        request.get("reporter_user_id") or request.get("origin_actor_id"),
        request.get("origin_device_id") or request.get("device_id"),
        request.get("category"),
        " ".join(sorted(_tokens(request.get("description")))),
        int(request.get("people_count") or 0),
        bool(request.get("water_rising")),
        rounded_coords,
    )


def _same_event(candidate: dict, existing: dict) -> bool:
    """Match only the same source/device and materially identical emergency claim."""
    candidate_source = candidate.get("reporter_user_id") or candidate.get("origin_actor_id")
    existing_source = existing.get("reporter_user_id") or existing.get("origin_actor_id")
    candidate_device = candidate.get("origin_device_id") or candidate.get("device_id")
    existing_device = existing.get("origin_device_id") or existing.get("device_id")
    if not candidate_source or candidate_source != existing_source:
        return False
    if candidate_device and existing_device and candidate_device != existing_device:
        return False
    return semantic_fingerprint(candidate) == semantic_fingerprint(existing)


def classify_duplicate(candidate: dict, existing: list[dict]) -> dict:
    """Return an explainable duplicate decision without mutating domain state."""
    candidate_fp = semantic_fingerprint(candidate)
    for item in existing:
        if _age(item.get("created_at", "")) > DEDUPE_WINDOW_SECONDS:
            continue
        if _same_event(candidate, item):
            return {"classification": "DUPLICATE", "reason": "same_source_device_and_semantic_fingerprint",
                    "fingerprint": candidate_fp, "matched_request_id": item.get("request_id"),
                    "matched_event_id": item.get("last_event_id"), "window_seconds": DEDUPE_WINDOW_SECONDS}
    return {"classification": "NOT_DUPLICATE", "reason": "no_same_source_device_semantic_match",
            "fingerprint": candidate_fp, "matched_request_id": None, "matched_event_id": None,
            "window_seconds": DEDUPE_WINDOW_SECONDS}


def find_duplicate(candidate: dict, existing: list[dict]) -> dict | None:
    """Spatio-temporal dedupe with exact source/device semantic identity."""
    coords = (candidate.get("location") or {}).get("coordinates")
    if not coords:
        return None
    best, best_dist = None, DEDUPE_RADIUS_M
    for e in existing:
        ec = (e.get("location") or {}).get("coordinates")
        if not ec or e.get("category") != candidate.get("category") or not _same_event(candidate, e):
            continue
        if _age(e.get("created_at", "")) > DEDUPE_WINDOW_SECONDS:
            continue
        d = haversine_m(coords, ec)
        if d <= best_dist:
            best, best_dist = e, d
    return best
