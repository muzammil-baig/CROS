"""Geospatial routing: persistent base road graph + dynamic hazard overlay.

The base road graph is stored once and is NEVER rewritten for temporary
hazards. Hazards are applied as an overlay (edge cost multipliers / blocks)
computed at query time.
"""
import math
from datetime import datetime, timezone

import networkx as nx
from shapely.geometry import LineString, Point, shape

from ..models import utcnow_iso

# Operating area: flood-prone river delta district (synthetic but consistent)
AREA = {"min_lon": 90.36, "max_lon": 90.46, "min_lat": 23.72, "max_lat": 23.82}
GRID = 11  # 11x11 intersections

BASE_SPEED_KPH = {"arterial": 45.0, "local": 28.0, "bridge": 35.0}


def _node_id(i: int, j: int) -> str:
    return f"N{i:02d}{j:02d}"


def node_coords(i: int, j: int):
    lon = AREA["min_lon"] + (AREA["max_lon"] - AREA["min_lon"]) * i / (GRID - 1)
    lat = AREA["min_lat"] + (AREA["max_lat"] - AREA["min_lat"]) * j / (GRID - 1)
    return [round(lon, 6), round(lat, 6)]


def haversine_m(a, b) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp = p2 - p1
    dl = math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def build_base_graph() -> dict:
    """Produce the base road graph document (nodes + edges)."""
    nodes, edges = [], []
    for i in range(GRID):
        for j in range(GRID):
            nodes.append({"node_id": _node_id(i, j), "coordinates": node_coords(i, j)})
    # The river runs along j == 5; only j==5 crossings at i in {2,5,8} are bridges.
    for i in range(GRID):
        for j in range(GRID):
            a = _node_id(i, j)
            if i + 1 < GRID:
                b = _node_id(i + 1, j)
                cls = "arterial" if j % 5 == 0 else "local"
                edges.append(_edge(a, b, node_coords(i, j), node_coords(i + 1, j), cls))
            if j + 1 < GRID:
                b = _node_id(i, j + 1)
                if j == 4:
                    if i not in (2, 5, 8):
                        continue
                    cls = "bridge"
                else:
                    cls = "arterial" if i % 5 == 0 else "local"
                edges.append(_edge(a, b, node_coords(i, j), node_coords(i, j + 1), cls))
    return {
        "graph_id": "base-road-graph-v1",
        "area": AREA,
        "nodes": nodes,
        "edges": edges,
        "updated_at": utcnow_iso(),
        "source": "synthetic_district_grid_v1",
    }


def _edge(a, b, ca, cb, cls):
    length = haversine_m(ca, cb)
    return {
        "edge_id": f"{a}-{b}",
        "from": a, "to": b, "road_class": cls,
        "length_m": round(length, 1),
        "base_travel_seconds": round(length / (BASE_SPEED_KPH[cls] * 1000 / 3600), 1),
        "geometry": {"type": "LineString", "coordinates": [ca, cb]},
    }


def hazard_overlay(graph_doc: dict, hazards: list[dict]) -> dict:
    """Compute per-edge cost multipliers/blocks from active hazards. Base graph untouched."""
    overlay = {}
    polys = []
    for h in hazards:
        geom = h.get("geometry")
        if not geom:
            continue
        try:
            polys.append((shape(geom), h))
        except Exception:
            continue
    for e in graph_doc["edges"]:
        line = LineString(e["geometry"]["coordinates"])
        worst = None
        for poly, h in polys:
            if poly.intersects(line):
                sev = float(h.get("severity", 0.5))
                if worst is None or sev > worst[0]:
                    worst = (sev, h)
        if worst:
            sev, h = worst
            depth = float(h.get("water_level_m") or sev * 2.0)
            blocked = depth >= 0.8 or bool(h.get("road_blocked")) or sev >= 0.9
            overlay[e["edge_id"]] = {
                "hazard_id": h.get("hazard_id"),
                "hazard_type": h.get("hazard_type"),
                "severity": sev,
                "water_level_m": depth,
                "blocked": blocked,
                "cost_multiplier": 1e9 if blocked else round(1 + 3 * sev, 2),
                "verification": (h.get("verification") or {}).get("status", "UNVERIFIED"),
            }
    return overlay


def _to_nx(graph_doc: dict, overlay: dict, allow_hazard: bool):
    g = nx.DiGraph()
    for n in graph_doc["nodes"]:
        g.add_node(n["node_id"], coordinates=n["coordinates"])
    for e in graph_doc["edges"]:
        ov = overlay.get(e["edge_id"])
        mult = 1.0
        if ov:
            if ov["blocked"] and not allow_hazard:
                continue
            mult = 3.0 if ov["blocked"] else ov["cost_multiplier"]
        cost = e["base_travel_seconds"] * mult
        for a, b in ((e["from"], e["to"]), (e["to"], e["from"])):
            g.add_edge(a, b, weight=cost, edge_id=e["edge_id"],
                       length_m=e["length_m"], hazard=ov)
    return g


def nearest_node(graph_doc: dict, coords) -> str:
    best, bd = None, float("inf")
    for n in graph_doc["nodes"]:
        d = haversine_m(coords, n["coordinates"])
        if d < bd:
            best, bd = n["node_id"], d
    return best


def compute_route(graph_doc: dict, hazards: list[dict], origin, destination,
                  vehicle_profile: str = "road") -> dict:
    """Returns a route with geometry, ETA, hazard exposure and graph freshness."""
    overlay = hazard_overlay(graph_doc, hazards)
    if vehicle_profile == "boat":
        # Boats traverse water-covered segments; only non-water hazards (debris,
        # structural failure, damaging wind) block them. Hazard-agnostic rule.
        overlay = {k: v for k, v in overlay.items()
                   if v.get("hazard_type") != "flood"}
    src = nearest_node(graph_doc, origin)
    dst = nearest_node(graph_doc, destination)

    degraded = False
    try:
        g = _to_nx(graph_doc, overlay, allow_hazard=False)
        path = nx.shortest_path(g, src, dst, weight="weight")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        g = _to_nx(graph_doc, overlay, allow_hazard=True)
        degraded = True
        try:
            path = nx.shortest_path(g, src, dst, weight="weight")
        except Exception:
            return {"status": "NO_ROUTE", "graph_id": graph_doc["graph_id"],
                    "graph_updated_at": graph_doc["updated_at"],
                    "computed_at": utcnow_iso()}

    coords, total_s, total_m, exposures = [], 0.0, 0.0, []
    node_map = {n["node_id"]: n["coordinates"] for n in graph_doc["nodes"]}
    coords.append(node_map[path[0]])
    for a, b in zip(path, path[1:]):
        d = g[a][b]
        total_s += d["weight"]
        total_m += d["length_m"]
        coords.append(node_map[b])
        if d.get("hazard"):
            exposures.append({"edge_id": d["edge_id"], **{
                k: d["hazard"][k] for k in ("hazard_id", "severity", "water_level_m", "blocked")}})
    return {
        "status": "DEGRADED_ROUTE" if degraded else "OK",
        "profile": vehicle_profile,
        "origin": {"type": "Point", "coordinates": list(origin)},
        "destination": {"type": "Point", "coordinates": list(destination)},
        "geometry": {"type": "LineString", "coordinates": coords},
        "node_path": path,
        "distance_m": round(total_m, 1),
        "eta_seconds": round(total_s, 1),
        "hazard_exposure": exposures,
        "hazard_overlay_edges": len(overlay),
        "avoided_all_blocks": not degraded,
        "graph_id": graph_doc["graph_id"],
        "graph_updated_at": graph_doc["updated_at"],
        "graph_age_seconds": _age(graph_doc["updated_at"]),
        "computed_at": utcnow_iso(),
        "algorithm": "dijkstra_networkx_hazard_overlay_v1",
        "routing_version": "route-v1-networkx-fallback",
    }


def _age(iso: str) -> int:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return 0


def point_in_hazards(coords, hazards: list[dict]) -> float:
    """Max severity of hazards containing the point (0.0 if none)."""
    p = Point(coords[0], coords[1])
    worst = 0.0
    for h in hazards:
        try:
            if shape(h["geometry"]).contains(p):
                worst = max(worst, float(h.get("severity", 0.5)))
        except Exception:
            continue
    return worst
