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
# Measurements that restate the carrying amount (calc.engine Revalue and Impair).
REMEASUREMENTS = ("revaluation", "impairment", "impairment_reversal")


def _dec(value) -> Decimal:
    return Decimal(str(value or 0))


def money(value, currency: str) -> str:
    """An amount as stored and shown: rounded once to ``currency``'s precision."""
    from celerp.services.money import round_money

    return str(round_money(_dec(value), currency))


def said(value, currency: str) -> str:
    """An amount in a message, as the pages show it: symbol, thousands grouped, currency precision."""
    from celerp.output.doc_print import fmt_money

    return fmt_money(money(value, currency), currency)


def live(movements: list[dict]) -> list[dict]:
    """The movements that still count: undone ones and their reversals cancel out."""
    reversed_ids = {m.get("reverses") for m in movements if m.get("reverses")}
    return [m for m in movements if m["id"] not in reversed_ids and not m.get("reverses")]


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
    if data.get("surplus_account"):
        state["surplus_account"] = data["surplus_account"]
    return state


def apply_fixed_asset_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state or {})
    if event_type in ("fa.class.created", "fa.movement.recorded", "fa.maint.recorded", "fa.op.recorded", "fa.review.recorded",
                      "fa.run.prepared", "fa.run.posted", "fa.recon.recorded", "fa.taxbook.recorded",
                      "fa.taxyear.recorded", "fa.yearend.recorded", "fa.import.recorded", "fa.import.row",
                      "fa.reminder.created"):
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
    if event_type == "fa.asset.file_attached":
        current.setdefault("files", []).append({
            "id": data["file_id"], "filename": data["filename"], "mime": data.get("mime"), "size": data.get("size"),
            "url": data.get("url", ""), "description": data.get("description") or "",
            "document_tag": data.get("document_tag") or "", "uploaded_at": data.get("uploaded_at") or ""})
        return current
    if event_type in ("fa.asset.file_tagged", "fa.asset.file_described"):
        field = "document_tag" if event_type == "fa.asset.file_tagged" else "description"
        for f in current.get("files") or []:
            if f.get("id") == data["file_id"]:
                f[field] = data.get(field) or ""
        return current
    if event_type == "fa.asset.file_deleted":
        current["files"] = [f for f in current.get("files") or [] if f.get("id") != data["file_id"]]
        return current
    if event_type == "fa.asset.confirmed":
        current["confirmed"] = deepcopy(data["confirmed"])
        return current
    if event_type == "fa.asset.problem":
        current.setdefault("problems", []).append(deepcopy(data["problem"]))
        return current
    if event_type == "fa.check.recorded":
        return deepcopy(data)
    if event_type == "fa.check.removed":
        return {**current, "removed": True, "removed_by": data["user_id"]}
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
    if event_type == "fa.reminder.changed":
        current.update(deepcopy(data["changes"]))
        for row in current.get("history") or []:
            if row.get("id") == data.get("undid"):
                row["undone"] = True
        current["history"] = [deepcopy(data["history"]), *(current.get("history") or [])]
        return current
    if event_type == "fa.reminder.told":
        current.setdefault("told", []).append(data["stage"])
        return current
    if event_type in ("fa.class.updated", "fa.asset.updated", "fa.maint.updated"):
        current.update(deepcopy(data["changes"]))
        current["version"] = int(current.get("version") or 0) + 1
        return current
    if event_type in ("fa.asset.status", "fa.maint.status", "fa.review.status"):
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
