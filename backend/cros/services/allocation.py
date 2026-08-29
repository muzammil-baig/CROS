"""Resource allocation via optimal assignment (Hungarian / linear sum assignment).

NOTE ON ARCHITECTURE SUBSTITUTION: 02_SYSTEM_ARCHITECTURE specifies Google
OR-Tools. OR-Tools is not installable in this runtime, so the equivalent
deterministic combinatorial optimiser (scipy linear_sum_assignment, exact for
the assignment problem) is used behind the same service boundary, with a
deterministic greedy fallback if the solver fails. No LLM is involved.
"""
import numpy as np

from ..models import utcnow_iso
from .routing import compute_route, haversine_m

INFEASIBLE = 1e6

SUITABILITY = {
    "rescue": {"boat": 1.0, "team": 0.7, "helicopter": 0.9, "ambulance": 0.2, "truck": 0.3},
    "person_trapped": {"boat": 0.9, "team": 1.0, "helicopter": 0.8, "ambulance": 0.2, "truck": 0.4},
    "medical": {"ambulance": 1.0, "helicopter": 0.9, "boat": 0.5, "team": 0.4, "truck": 0.1},
    "evacuation": {"boat": 0.9, "truck": 1.0, "team": 0.6, "helicopter": 0.5, "ambulance": 0.3},
    "hazard": {"team": 1.0, "truck": 0.6, "boat": 0.5, "ambulance": 0.1, "helicopter": 0.3},
}

TIER_WEIGHT = {"critical": 4.0, "high": 3.0, "normal": 2.0, "low": 1.0}


def _suitability(category: str, kind: str) -> float:
    return SUITABILITY.get(category, {}).get(kind, 0.3)


def cost_for(request: dict, resource: dict, graph_doc: dict, hazards: list[dict]) -> dict:
    """Deterministic cost of assigning resource -> request, with feasibility reasons."""
    reasons = []
    if resource.get("status") != "available":
        reasons.append("resource_unavailable")
    suit = _suitability(request.get("category", "rescue"), resource.get("kind", "team"))
    if suit < 0.25:
        reasons.append("unsuitable_capability")
    cap = int(resource.get("capacity") or 1)
    need = int(request.get("people_count") or 1)
    if cap < need:
        reasons.append("insufficient_capacity")

    r_loc = (resource.get("location") or {}).get("coordinates")
    q_loc = (request.get("location") or {}).get("coordinates")
    eta = None
    route_status = "NOT_COMPUTED"
    if r_loc and q_loc:
        profile = "boat" if resource.get("kind") == "boat" else "road"
        route = compute_route(graph_doc, hazards, r_loc, q_loc, profile)
        route_status = route["status"]
        if route_status == "NO_ROUTE":
            reasons.append("route_infeasible")
        else:
            eta = route["eta_seconds"]
            if route_status == "DEGRADED_ROUTE":
                reasons.append("route_degraded")
    if eta is None:
        eta = (haversine_m(r_loc, q_loc) / 8.0) if (r_loc and q_loc) else 3600.0

    tier = (request.get("priority") or {}).get("tier", "normal")
    blocking = [r for r in reasons if r not in ("route_degraded",)]
    cost = (eta / 60.0) / (suit * TIER_WEIGHT.get(tier, 2.0))
    if "route_degraded" in reasons:
        cost *= 1.5
    if blocking:
        cost = INFEASIBLE
    return {
        "cost": round(cost, 4),
        "eta_seconds": round(eta, 1),
        "suitability": suit,
        "route_status": route_status,
        "feasible": not blocking,
        "reasons": reasons,
    }


def propose(requests: list[dict], resources: list[dict], graph_doc: dict,
            hazards: list[dict]) -> dict:
    """Exact optimal assignment; deterministic greedy fallback on solver failure."""
    if not requests or not resources:
        return {"assignments": [], "unassigned_request_ids": [r["request_id"] for r in requests],
                "solver": "none", "objective": 0.0, "computed_at": utcnow_iso()}

    matrix, meta = [], []
    for rq in requests:
        row, mrow = [], []
        for rs in resources:
            c = cost_for(rq, rs, graph_doc, hazards)
            row.append(c["cost"])
            mrow.append(c)
        matrix.append(row)
        meta.append(mrow)

    solver = "scipy_linear_sum_assignment_exact"
    pairs = []
    try:
        from scipy.optimize import linear_sum_assignment
        arr = np.array(matrix, dtype=float)
        ri, ci = linear_sum_assignment(arr)
        pairs = list(zip(ri.tolist(), ci.tolist()))
    except Exception:
        solver = "deterministic_greedy_fallback"
        used = set()
        order = sorted(range(len(requests)),
                       key=lambda i: -TIER_WEIGHT.get(
                           (requests[i].get("priority") or {}).get("tier", "normal"), 2.0))
        for i in order:
            best_j, best_c = None, INFEASIBLE
            for j in range(len(resources)):
                if j in used:
                    continue
                if matrix[i][j] < best_c:
                    best_j, best_c = j, matrix[i][j]
            if best_j is not None and best_c < INFEASIBLE:
                used.add(best_j)
                pairs.append((i, best_j))

    assignments, objective = [], 0.0
    assigned_requests = set()
    for i, j in pairs:
        m = meta[i][j]
        if not m["feasible"]:
            continue
        objective += m["cost"]
        assigned_requests.add(requests[i]["request_id"])
        assignments.append({
            "request_id": requests[i]["request_id"],
            "resource_id": resources[j]["resource_id"],
            "responder_user_id": resources[j].get("responder_user_id"),
            "resource_label": resources[j].get("label"),
            "resource_kind": resources[j].get("kind"),
            "cost": m["cost"],
            "eta_seconds": m["eta_seconds"],
            "suitability": m["suitability"],
            "route_status": m["route_status"],
            "notes": m["reasons"],
        })
    unassigned = [r["request_id"] for r in requests if r["request_id"] not in assigned_requests]
    return {
        "assignments": assignments,
        "unassigned_request_ids": unassigned,
        "solver": solver,
        "objective": round(objective, 4),
        "constraints": ["availability", "capacity", "suitability", "travel_time",
                        "route_feasibility", "mission_priority"],
        "computed_at": utcnow_iso(),
    }
