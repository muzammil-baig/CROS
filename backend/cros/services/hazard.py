"""Pluggable hazard modules. The core domain stays hazard-agnostic."""
import math
from abc import ABC, abstractmethod

from ..models import utcnow_iso


class HazardModule(ABC):
    hazard_type: str = "generic"

    @abstractmethod
    def severity(self, hazard: dict) -> float:
        ...

    @abstractmethod
    def predict(self, hazard: dict, horizon_minutes: int) -> dict:
        ...

    @abstractmethod
    def blocks_road(self, hazard: dict) -> bool:
        ...

    @abstractmethod
    def vehicle_profiles(self) -> list[str]:
        ...


class FloodModule(HazardModule):
    """Numerical flood model: depth from water level vs terrain, rainfall accumulation."""
    hazard_type = "flood"
    IMPASSABLE_DEPTH_M = 0.8
    BOAT_MIN_DEPTH_M = 0.4

    def severity(self, hazard: dict) -> float:
        depth = float(hazard.get("water_level_m") or 0.0)
        rate = float(hazard.get("rise_rate_m_per_hour") or 0.0)
        return round(min(1.0, 0.35 * depth + 0.4 * min(1.0, rate / 0.5) + 0.25 *
                         (1.0 if depth >= self.IMPASSABLE_DEPTH_M else 0.0)), 3)

    def predict(self, hazard: dict, horizon_minutes: int = 60) -> dict:
        depth = float(hazard.get("water_level_m") or 0.0)
        rate = float(hazard.get("rise_rate_m_per_hour") or 0.0)
        rainfall = float(hazard.get("rainfall_mm_per_hour") or 0.0)
        # rainfall contributes via runoff coefficient over the catchment
        effective_rate = rate + (rainfall / 1000.0) * 6.0
        hours = horizon_minutes / 60.0
        projected = round(max(0.0, depth + effective_rate * hours), 3)
        growth = 1.0 + min(1.5, effective_rate * hours * 1.2)
        return {
            "hazard_id": hazard.get("hazard_id"),
            "horizon_minutes": horizon_minutes,
            "current_water_level_m": depth,
            "projected_water_level_m": projected,
            "effective_rise_rate_m_per_hour": round(effective_rate, 4),
            "projected_severity": round(min(1.0, 0.35 * projected + 0.4 *
                                           min(1.0, effective_rate / 0.5)), 3),
            "projected_area_growth_factor": round(growth, 3),
            "projected_geometry": _scale_polygon(hazard.get("geometry"), math.sqrt(growth)),
            "impassable_for_road_vehicles": projected >= self.IMPASSABLE_DEPTH_M,
            "navigable_by_boat": projected >= self.BOAT_MIN_DEPTH_M,
            "verification_status": "PREDICTED",
            "model": "flood_kinematic_v1",
            "computed_at": utcnow_iso(),
        }

    def blocks_road(self, hazard: dict) -> bool:
        return float(hazard.get("water_level_m") or 0.0) >= self.IMPASSABLE_DEPTH_M

    def vehicle_profiles(self) -> list[str]:
        return ["boat", "helicopter", "road"]


def _scale_polygon(geometry: dict | None, factor: float):
    if not geometry or geometry.get("type") != "Polygon":
        return geometry
    rings = geometry["coordinates"]
    out = []
    for ring in rings:
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        out.append([[round(cx + (p[0] - cx) * factor, 6),
                     round(cy + (p[1] - cy) * factor, 6)] for p in ring])
    return {"type": "Polygon", "coordinates": out}


_REGISTRY: dict[str, HazardModule] = {}


def register(module: HazardModule):
    _REGISTRY[module.hazard_type] = module


def get_module(hazard_type: str) -> HazardModule:
    return _REGISTRY.get(hazard_type, _REGISTRY["flood"])


def registered_types() -> list[str]:
    return sorted(_REGISTRY.keys())


register(FloodModule())
