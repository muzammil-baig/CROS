"""Communication transport abstraction + deterministic transport selection policy.

Every transport implements the same contract. Physical/provider-dependent
transports (satellite, radio, SMS) are simulator-backed and report
`simulated=True`; the rest of the system only talks to the abstraction.
"""
import asyncio
import random
from abc import ABC, abstractmethod

from ..constants import EventType, Priority
from ..models import utcnow_iso

PRIORITY_RANK = {"critical": 3, "high": 2, "normal": 1, "low": 0}


class Transport(ABC):
    name: str = "transport"
    simulated: bool = False

    def __init__(self):
        self.available = True
        self.degradation = 0.0  # 0..1 injected degradation

    @property
    @abstractmethod
    def capabilities(self) -> dict:
        """latency_ms, bandwidth_kbps, reliability, cost_per_kb, energy_cost,
        supports_ack, supports_priority, max_payload_kb"""

    @abstractmethod
    async def _transmit(self, message: dict) -> dict:
        ...

    def state(self) -> dict:
        c = dict(self.capabilities)
        c["reliability"] = round(c["reliability"] * (1 - self.degradation), 3)
        c["latency_ms"] = round(c["latency_ms"] * (1 + 3 * self.degradation))
        c["bandwidth_kbps"] = round(c["bandwidth_kbps"] * max(0.05, 1 - self.degradation), 2)
        return {
            "name": self.name,
            "available": self.available and self.degradation < 1.0,
            "simulated": self.simulated,
            "degradation": round(self.degradation, 3),
            **c,
            "updated_at": utcnow_iso(),
        }

    async def send(self, message: dict) -> dict:
        st = self.state()
        if not st["available"]:
            return {"delivered": False, "transport": self.name, "error": "TRANSPORT_UNAVAILABLE",
                    "simulated": self.simulated}
        size_kb = max(0.1, message.get("size_kb", 1.0))
        if size_kb > st["max_payload_kb"]:
            return {"delivered": False, "transport": self.name, "error": "PAYLOAD_TOO_LARGE",
                    "simulated": self.simulated}
        result = await self._transmit(message)
        result.setdefault("transport", self.name)
        result.setdefault("simulated", self.simulated)
        result["latency_ms"] = result.get("latency_ms", st["latency_ms"])
        result["cost"] = round(st["cost_per_kb"] * size_kb, 6)
        result["supports_ack"] = st["supports_ack"]
        return result


class _SimLink(Transport):
    """Shared simulated-link behaviour: latency wait, reliability draw."""
    _caps: dict = {}

    @property
    def capabilities(self) -> dict:
        return self._caps

    async def _transmit(self, message: dict) -> dict:
        st = self.state()
        size_kb = max(0.1, message.get("size_kb", 1.0))
        tx_ms = st["latency_ms"] + (size_kb * 8 / max(0.1, st["bandwidth_kbps"])) * 1000
        await asyncio.sleep(min(0.35, tx_ms / 8000.0))
        ok = random.random() < st["reliability"]
        return {"delivered": ok, "latency_ms": round(tx_ms),
                "error": None if ok else "LINK_LOSS",
                "acknowledged": ok and st["supports_ack"]}


class InternetTransport(_SimLink):
    name = "internet"
    _caps = {"latency_ms": 45, "bandwidth_kbps": 20000, "reliability": 0.995,
             "cost_per_kb": 0.0, "energy_cost": 0.1, "supports_ack": True,
             "supports_priority": True, "max_payload_kb": 10240}


class CellularTransport(_SimLink):
    name = "cellular"
    _caps = {"latency_ms": 120, "bandwidth_kbps": 4000, "reliability": 0.97,
             "cost_per_kb": 0.00002, "energy_cost": 0.3, "supports_ack": True,
             "supports_priority": True, "max_payload_kb": 4096}


class SMSTransport(_SimLink):
    name = "sms"
    simulated = True
    _caps = {"latency_ms": 5000, "bandwidth_kbps": 0.14, "reliability": 0.9,
             "cost_per_kb": 0.02, "energy_cost": 0.2, "supports_ack": False,
             "supports_priority": False, "max_payload_kb": 0.14}


class LocalWiFiTransport(_SimLink):
    name = "local_wifi"
    _caps = {"latency_ms": 12, "bandwidth_kbps": 50000, "reliability": 0.99,
             "cost_per_kb": 0.0, "energy_cost": 0.2, "supports_ack": True,
             "supports_priority": True, "max_payload_kb": 10240}


class BluetoothTransport(_SimLink):
    name = "bluetooth"
    _caps = {"latency_ms": 80, "bandwidth_kbps": 700, "reliability": 0.93,
             "cost_per_kb": 0.0, "energy_cost": 0.05, "supports_ack": True,
             "supports_priority": False, "max_payload_kb": 512}


class MeshTransport(_SimLink):
    name = "mesh"
    _caps = {"latency_ms": 450, "bandwidth_kbps": 250, "reliability": 0.9,
             "cost_per_kb": 0.0, "energy_cost": 0.25, "supports_ack": True,
             "supports_priority": True, "max_payload_kb": 256}


class RadioTransport(_SimLink):
    name = "radio"
    simulated = True
    _caps = {"latency_ms": 900, "bandwidth_kbps": 9.6, "reliability": 0.85,
             "cost_per_kb": 0.0, "energy_cost": 0.6, "supports_ack": False,
             "supports_priority": True, "max_payload_kb": 4}


class SatelliteTransport(Transport):
    """Talks to a pluggable provider adapter. MVP adapter is a simulator."""
    name = "satellite"

    def __init__(self, adapter=None):
        super().__init__()
        self.adapter = adapter or SatelliteSimulatorAdapter()
        self.simulated = self.adapter.simulated

    @property
    def capabilities(self) -> dict:
        return self.adapter.capabilities

    async def _transmit(self, message: dict) -> dict:
        return await self.adapter.transmit(message, self.state())


class SatelliteProviderAdapter(ABC):
    simulated = False
    provider = "abstract"

    @property
    @abstractmethod
    def capabilities(self) -> dict:
        ...

    @abstractmethod
    async def transmit(self, message: dict, link_state: dict) -> dict:
        ...


class SatelliteSimulatorAdapter(SatelliteProviderAdapter):
    """Provider-neutral satellite simulator: window visibility, latency, cost, ack."""
    simulated = True
    provider = "simulator"

    def __init__(self):
        self.window_open = True
        self.pass_seconds_remaining = 240

    @property
    def capabilities(self) -> dict:
        return {"latency_ms": 1400, "bandwidth_kbps": 64, "reliability": 0.9,
                "cost_per_kb": 0.005, "energy_cost": 0.8, "supports_ack": True,
                "supports_priority": True, "max_payload_kb": 128}

    async def transmit(self, message: dict, link_state: dict) -> dict:
        if not self.window_open:
            return {"delivered": False, "error": "NO_SATELLITE_WINDOW",
                    "provider": self.provider}
        size_kb = max(0.1, message.get("size_kb", 1.0))
        tx_ms = link_state["latency_ms"] + (size_kb * 8 / link_state["bandwidth_kbps"]) * 1000
        await asyncio.sleep(min(0.4, tx_ms / 6000.0))
        ok = random.random() < link_state["reliability"]
        return {"delivered": ok, "latency_ms": round(tx_ms), "provider": self.provider,
                "acknowledged": ok, "error": None if ok else "SATELLITE_LINK_LOSS",
                "billed_cost": round(size_kb * self.capabilities["cost_per_kb"], 6)}


class TransportRegistry:
    def __init__(self):
        self.transports: dict[str, Transport] = {}
        for t in (InternetTransport(), CellularTransport(), LocalWiFiTransport(),
                  MeshTransport(), BluetoothTransport(), SMSTransport(),
                  RadioTransport(), SatelliteTransport()):
            self.transports[t.name] = t

    def get(self, name: str) -> Transport | None:
        return self.transports.get(name)

    def states(self) -> list[dict]:
        return [t.state() for t in self.transports.values()]

    def set_degradation(self, name: str, value: float):
        t = self.transports.get(name)
        if t:
            t.degradation = max(0.0, min(1.0, value))
            t.available = value < 1.0

    def connectivity_state(self) -> str:
        s = {t.name: t.state() for t in self.transports.values()}

        def up(n):
            return s[n]["available"] and s[n]["reliability"] > 0.4

        if up("internet"):
            return "CONNECTED" if s["internet"]["reliability"] > 0.9 else "DEGRADED"
        if up("cellular"):
            return "DEGRADED"
        if up("satellite"):
            return "SATELLITE_BACKHAUL"
        if up("mesh") or up("local_wifi") or up("bluetooth"):
            return "MESH_ONLY"
        if up("radio") or up("sms"):
            return "LOCAL_ONLY"
        return "ISOLATED"


def score_transport(state: dict, priority: str, size_kb: float,
                    require_ack: bool = False) -> float:
    """Deterministic transport score. Higher is better. -inf means ineligible."""
    if not state["available"]:
        return float("-inf")
    if size_kb > state["max_payload_kb"]:
        return float("-inf")
    if require_ack and not state["supports_ack"]:
        return float("-inf")
    rank = PRIORITY_RANK.get(priority, 1)
    latency_term = 1000.0 / (state["latency_ms"] + 50.0)
    bandwidth_term = min(10.0, state["bandwidth_kbps"] / 100.0)
    reliability_term = state["reliability"] * 12.0
    cost_penalty = state["cost_per_kb"] * size_kb * (40.0 if rank < 3 else 4.0)
    energy_penalty = state["energy_cost"] * (2.0 if rank < 2 else 0.5)
    priority_bonus = 2.0 * rank if state["supports_priority"] else 0.0
    ack_bonus = 1.5 if state["supports_ack"] else 0.0
    urgency_latency_weight = 3.0 if rank >= 2 else 1.0
    return round(reliability_term + urgency_latency_weight * latency_term + bandwidth_term
                 + priority_bonus + ack_bonus - cost_penalty - energy_penalty, 4)


def select_transport(states: list[dict], priority: str, size_kb: float,
                     require_ack: bool = False) -> tuple[str | None, list[dict]]:
    scored = []
    for st in states:
        scored.append({"transport": st["name"], "score": score_transport(
            st, priority, size_kb, require_ack), "available": st["available"],
            "simulated": st["simulated"]})
    eligible = [s for s in scored if s["score"] != float("-inf")]
    eligible.sort(key=lambda s: -s["score"])
    scored.sort(key=lambda s: (s["score"] == float("-inf"), -s["score"]))
    for s in scored:
        if s["score"] == float("-inf"):
            s["score"] = None
    return (eligible[0]["transport"] if eligible else None), scored


registry = TransportRegistry()
