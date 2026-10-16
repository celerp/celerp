# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fa. projection handler. An asset's financial figures change only by its movements:
each movement is recorded on its own entity and mirrored onto the asset, so the asset's
figures always equal the fold of its movements (fold_movements)."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

# The money buckets a movement carries, as signed amounts.
BUCKETS = ("cost", "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus")


def _dec(value) -> Decimal:
    return Decimal(str(value or 0))


def fold_movements(movements: list[dict]) -> dict:
    """The financial figures the movements add up to."""
    out = {k: Decimal(0) for k in BUCKETS}
    quantity = Decimal(0)
    for mv in movements:
        for k in BUCKETS:
            out[k] += _dec((mv.get("amounts") or {}).get(k))
        quantity += _dec(mv.get("quantity"))
    return {**{k: str(v) for k, v in out.items()}, "quantity": _whole(quantity)}


def _whole(value: Decimal):
    return int(value) if value == value.to_integral_value() else str(value)


def _moved(state: dict, data: dict) -> dict:
    for k in BUCKETS:
        state[k] = str(_dec(state.get(k)) + _dec((data.get("amounts") or {}).get(k)))
    state["quantity"] = _whole(_dec(state.get("quantity")) + _dec(data.get("quantity")))
    state["movement_count"] = int(state.get("movement_count") or 0) + 1
    return state


def apply_fixed_asset_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state or {})
    if event_type in ("fa.class.created", "fa.movement.recorded", "fa.maint.recorded", "fa.op.recorded",
                      "fa.run.prepared", "fa.run.posted"):
        return deepcopy(data)
    if event_type == "fa.correction.requested":
        return {**deepcopy(data), "state": "requested"}
    if event_type == "fa.correction.applied":
        return {**current, **deepcopy(data), "state": "applied"}
    if event_type in ("fa.correction.withdrawn", "fa.correction.undone"):
        return {**current, "state": event_type.rsplit(".", 1)[1], "closed_by_op": data.get("op_key")}
    if event_type == "fa.run.reversed":
        return {**current, "status": "reversed", "reversed_by_op": data["op_key"]}
    if event_type == "fa.reading.recorded":
        current.setdefault("readings", {})[data["period"]] = data["units"]
        return current
    if event_type == "fa.skip.recorded":
        current.setdefault("skips", {})[data["month"]] = data["reason"]
        return current
    if event_type == "fa.skip.withdrawn":
        current.setdefault("skips", {}).pop(data["month"], None)
        return current
    if event_type == "fa.asset.exception":
        current["exception"] = deepcopy(data["exception"])
        return current
    if event_type == "fa.asset.created":
        new = deepcopy(data)
        new.setdefault("quantity", 0)
        if new.get("status") != "physical":
            new.update({k: new.get(k) or "0" for k in BUCKETS})
        new.update(movement_count=0, version=1)
        return new
    if event_type in ("fa.class.updated", "fa.asset.updated"):
        current.update(deepcopy(data["changes"]))
        current["version"] = int(current.get("version") or 0) + 1
        return current
    if event_type in ("fa.asset.status", "fa.maint.status"):
        current["status"] = data["status"]
        # A disposal records itself and clears what held the asset; undoing it clears the record.
        # A sale records where it stands with its invoice.
        current.update({k: deepcopy(data[k]) for k in ("disposal", "exception", "sale") if k in data})
        return current
    if event_type == "fa.asset.financials":
        current.update(deepcopy(data))
        current.update({k: current.get(k) or "0" for k in BUCKETS})
        current["version"] = int(current.get("version") or 0) + 1
        return current
    if event_type == "fa.asset.moved":
        return _moved(current, data)
    if event_type == "fa.op.undone":
        current["undone_by"] = data["undo_key"]
        return current
    if event_type == "fa.src.claimed":
        return {**deepcopy(data), "status": "claimed", "generation": int(current.get("generation") or 0)}
    if event_type == "fa.src.released":
        return {**current, "status": "released", "released_by_op": data["op_key"],
                "generation": int(current.get("generation") or 0) + 1}
    return current
