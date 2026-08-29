"""Operation-based CRDT counters for hospital / shelter capacity.

Capacity is never assigned last-write-wins. Every change is an idempotent
increment/decrement operation identified by operation_id; the current value is
the fold of all operations, so concurrent edge updates converge.
"""
from ..models import utcnow_iso

COUNTER_KINDS = {
    "hospital": ["occupied_beds", "occupied_icu_beds", "incoming_patients"],
    "shelter": ["occupancy", "reserved"],
}


async def apply_operation(db, *, facility_id: str, counter_kind: str, operation: str,
                          amount: int, operation_id: str, actor_id: str | None,
                          origin_device_id: str | None = None) -> dict:
    """Idempotent by operation_id. Returns the recomputed facility counters."""
    existing = await db.capacity_ops.find_one({"operation_id": operation_id}, {"_id": 0})
    if existing is None:
        await db.capacity_ops.insert_one({
            "operation_id": operation_id,
            "facility_id": facility_id,
            "counter_kind": counter_kind,
            "operation": operation,
            "amount": int(amount),
            "actor_id": actor_id,
            "origin_device_id": origin_device_id,
            "created_at": utcnow_iso(),
        })
        duplicate = False
    else:
        duplicate = True
    counters = await recompute(db, facility_id)
    return {"duplicate": duplicate, "counters": counters}


async def recompute(db, facility_id: str) -> dict:
    ops = await db.capacity_ops.find({"facility_id": facility_id}, {"_id": 0}).to_list(20000)
    inc: dict[str, int] = {}
    dec: dict[str, int] = {}
    for op in ops:
        target = inc if op["operation"] == "increment" else dec
        target[op["counter_kind"]] = target.get(op["counter_kind"], 0) + int(op["amount"])
    kinds = set(inc) | set(dec)
    counters = {k: {"increments": inc.get(k, 0), "decrements": dec.get(k, 0),
                    "value": inc.get(k, 0) - dec.get(k, 0)} for k in kinds}
    await db.facilities.update_one(
        {"facility_id": facility_id},
        {"$set": {"counters": counters, "counters_updated_at": utcnow_iso(),
                  "op_count": len(ops)}})
    return counters


def availability(facility: dict) -> dict:
    counters = facility.get("counters") or {}
    ftype = facility.get("facility_type")
    if ftype == "hospital":
        beds = int(facility.get("capacity_total") or 0)
        occ = counters.get("occupied_beds", {}).get("value", 0)
        icu_total = int(facility.get("icu_capacity_total") or 0)
        icu_occ = counters.get("occupied_icu_beds", {}).get("value", 0)
        return {"beds_total": beds, "beds_occupied": occ, "beds_available": max(0, beds - occ),
                "icu_total": icu_total, "icu_occupied": icu_occ,
                "icu_available": max(0, icu_total - icu_occ),
                "incoming": counters.get("incoming_patients", {}).get("value", 0)}
    total = int(facility.get("capacity_total") or 0)
    occ = counters.get("occupancy", {}).get("value", 0)
    res = counters.get("reserved", {}).get("value", 0)
    return {"capacity_total": total, "occupancy": occ, "reserved": res,
            "available": max(0, total - occ - res),
            "utilization_pct": round(100 * occ / total, 1) if total else 0.0}
