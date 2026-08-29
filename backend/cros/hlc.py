"""Hybrid Logical Clock. Format: <physical_ms>.<counter>.<node_id>"""
import threading
import time


class HybridLogicalClock:
    def __init__(self, node_id: str):
        self.node_id = node_id
        self._lock = threading.Lock()
        self._physical = 0
        self._counter = 0

    def now(self) -> str:
        with self._lock:
            wall = int(time.time() * 1000)
            if wall > self._physical:
                self._physical, self._counter = wall, 0
            else:
                self._counter += 1
            return f"{self._physical}.{self._counter}.{self.node_id}"

    def update(self, remote: str) -> str:
        """Merge a remote HLC, returning our new local HLC."""
        r_phys, r_cnt, _ = parse(remote)
        with self._lock:
            wall = int(time.time() * 1000)
            newest = max(wall, self._physical, r_phys)
            if newest == self._physical == r_phys:
                self._counter = max(self._counter, r_cnt) + 1
            elif newest == self._physical:
                self._counter += 1
            elif newest == r_phys:
                self._counter = r_cnt + 1
            else:
                self._counter = 0
            self._physical = newest
            return f"{self._physical}.{self._counter}.{self.node_id}"


def parse(value: str):
    parts = str(value).split(".")
    if len(parts) < 3:
        raise ValueError(f"invalid HLC: {value}")
    return int(parts[0]), int(parts[1]), ".".join(parts[2:])


def compare(a: str, b: str) -> int:
    ap, ac, an = parse(a)
    bp, bc, bn = parse(b)
    if (ap, ac, an) == (bp, bc, bn):
        return 0
    return -1 if (ap, ac, an) < (bp, bc, bn) else 1


def happened_before(a: str, b: str) -> bool:
    return compare(a, b) < 0
