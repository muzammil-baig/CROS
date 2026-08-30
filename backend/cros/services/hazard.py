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


class CycloneModule(HazardModule):
    """Wind/surge model: severity from sustained wind, gusts and storm surge depth."""
    hazard_type = "cyclone"
    DAMAGING_WIND_KPH = 120.0
    IMPASSABLE_SURGE_M = 0.6

    def severity(self, hazard: dict) -> float:
        wind = float(hazard.get("wind_speed_kph") or 0.0)
        gust = float(hazard.get("gust_speed_kph") or wind * 1.3)
        surge = float(hazard.get("storm_surge_m") or 0.0)
        return round(min(1.0, 0.5 * min(1.0, wind / 200.0) + 0.2 * min(1.0, gust / 260.0)
                         + 0.3 * min(1.0, surge / 3.0)), 3)

    def predict(self, hazard: dict, horizon_minutes: int = 60) -> dict:
        wind = float(hazard.get("wind_speed_kph") or 0.0)
        trend = float(hazard.get("intensification_kph_per_hour") or 0.0)
        surge = float(hazard.get("storm_surge_m") or 0.0)
        surge_rate = float(hazard.get("surge_rate_m_per_hour") or 0.0)
        hours = horizon_minutes / 60.0
        proj_wind = max(0.0, wind + trend * hours)
        proj_surge = max(0.0, surge + surge_rate * hours)
        projected = {"wind_speed_kph": proj_wind, "storm_surge_m": proj_surge,
                     "gust_speed_kph": proj_wind * 1.3}
        drift = float(hazard.get("track_speed_kph") or 18.0) * hours
        return {
            "hazard_id": hazard.get("hazard_id"),
            "horizon_minutes": horizon_minutes,
            "current_wind_speed_kph": wind,
            "projected_wind_speed_kph": round(proj_wind, 1),
            "current_storm_surge_m": surge,
            "projected_storm_surge_m": round(proj_surge, 3),
            "projected_severity": self.severity(projected),
            "projected_track_drift_km": round(drift, 1),
            "projected_area_growth_factor": round(1.0 + min(1.2, drift / 40.0), 3),
            "projected_geometry": _scale_polygon(hazard.get("geometry"),
                                                 math.sqrt(1.0 + min(1.2, drift / 40.0))),
            "damaging_winds": proj_wind >= self.DAMAGING_WIND_KPH,
            "impassable_for_road_vehicles": proj_surge >= self.IMPASSABLE_SURGE_M
            or proj_wind >= self.DAMAGING_WIND_KPH,
            "navigable_by_boat": proj_wind < self.DAMAGING_WIND_KPH,
            "verification_status": "PREDICTED",
            "model": "cyclone_wind_surge_v1",
            "computed_at": utcnow_iso(),
        }

    def blocks_road(self, hazard: dict) -> bool:
        return (float(hazard.get("wind_speed_kph") or 0.0) >= self.DAMAGING_WIND_KPH
                or float(hazard.get("storm_surge_m") or 0.0) >= self.IMPASSABLE_SURGE_M
                or bool(hazard.get("debris")))

    def vehicle_profiles(self) -> list[str]:
        return ["road", "boat"]


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
register(CycloneModule())
