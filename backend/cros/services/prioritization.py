"""Deterministic prioritization (explicit scoring, no LLM)."""
from datetime import datetime, timezone

CATEGORY_WEIGHT = {
    "person_trapped": 40,
    "medical": 36,
    "rescue": 30,
    "evacuation": 20,
    "hazard": 12,
    "welfare_check": 8,
    "safe_report": 0,
}

VULNERABILITY_WEIGHT = {
    "infant": 10, "child": 8, "elderly": 8, "disabled": 8,
    "pregnant": 9, "injured": 10, "unconscious": 14, "none": 0,
}

VERIFICATION_MULTIPLIER = {
    "VERIFIED": 1.0, "CORROBORATED": 0.95, "UNVERIFIED": 0.8,
    "PREDICTED": 0.7, "STALE": 0.6, "CONTRADICTORY": 0.45,
}

TIERS = [(75, "critical"), (55, "high"), (30, "normal"), (0, "low")]


def _age_seconds(iso: str) -> float:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _bounded_number(value, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def tier_for(score: float) -> str:
    for threshold, tier in TIERS:
        if score >= threshold:
            return tier
    return "low"


def compute_priority(request: dict, hazard_severity: float = 0.0) -> dict:
    """Pure function: returns score, tier and the explicit factor breakdown."""
    factors = {}
    factors["category"] = float(CATEGORY_WEIGHT.get(request.get("category"), 10))

    try:
        people = max(1, min(10000, int(request.get("people_count") or 1)))
    except (TypeError, ValueError, OverflowError):
        people = 1
    factors["people_count"] = float(min(15, 3 * people))

    vulns = request.get("vulnerabilities") or []
    factors["vulnerability"] = float(min(20, sum(VULNERABILITY_WEIGHT.get(v, 0) for v in vulns)))

    age_min = _age_seconds(request.get("created_at", "")) / 60.0
    factors["time_waiting"] = float(min(15, age_min / 4.0))

    proximity = _bounded_number(request.get("hazard_proximity", hazard_severity))
    factors["hazard_proximity"] = round(proximity * 15, 2)
    factors["hazard_exposure"] = float(min(15, _bounded_number(hazard_severity) * 15))

    if request.get("water_rising"):
        factors["water_rising"] = 8.0

    raw = sum(factors.values())
    verification = (request.get("verification") or {}).get("status", "UNVERIFIED")
    multiplier = VERIFICATION_MULTIPLIER.get(verification, 0.8)
    score = round(min(100.0, raw * multiplier), 2)

    return {
        "score": score,
        "tier": tier_for(score),
        "factors": {k: round(v, 2) for k, v in factors.items()},
        "verification_multiplier": multiplier,
        "algorithm": "deterministic_weighted_v2",
        "scoring_version": "priority-v2",
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
