# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from .expansion import merge_inputs

# The run's own accounting facts. Only movement events write them, so a created event (from an
# import or any other caller) can never carry them in. Older runs replay with the defaults: a run
# with movement but no recorded value is marked untracked until the upgrade settles it.
WIP_FACTS = ("wip_issued", "wip_transferred", "wip_wasted", "wip_account_code", "wip_untracked",
             "wip_unresolved", "receipts", "closing")
# What completion changes on a run, kept with the completion so reopening restores it exactly.
_CLOSED = ("status", "is_in_production", "actual_outputs", "waste", "labor_hours", "wip_transferred", "wip_wasted")


def _money(value) -> str:
    return str(Decimal(str(value or 0)))


def _add(current: dict, key: str, value) -> None:
    current[key] = _money(Decimal(current.get(key) or "0") + Decimal(str(value or 0)))


def _issued_values(current: dict, data: dict) -> None:
    """Each component's value as ``data["components"]`` records it, for the components it names."""
    values = {c.get("item_id"): c.get("value") for c in data.get("components") or []}
    for inp in current.get("inputs", []):
        if inp.get("item_id") in values:
            inp["issued_value"] = _money(values[inp["item_id"]])


def _positive(line: dict) -> bool:
    return float(line.get("quantity") or 0) > 0


def apply_manufacturing_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "mfg.order.created":
        current.update({"entity_type": "mfg_order", **{k: v for k, v in data.items() if k not in WIP_FACTS}})
        # Canonical statuses: planned -> in_progress -> on_hold -> completed / cancelled.
        # `data` may carry an explicit status (e.g. a one-tap build that completes immediately);
        # otherwise a new run starts Planned.
        current.setdefault("status", "planned")
        current.setdefault("is_in_production", False)
        current.setdefault("actual_outputs", [])
        # Execution progress: how much of each input has been issued, and how much output received.
        current.setdefault("received_qty", 0.0)
        # The lots this run has produced, in receipt order - the run's own record of its output,
        # used to re-cost them at completion without scanning every item.
        current.setdefault("received_lots", [])
        # One line per component: a component listed on two lines needs both amounts. A line at
        # zero or below (only an older release stored one) is kept as written, after them, and
        # holds the run back from going ahead (movements.run_shape_problem). What was issued,
        # and the value it took, are written only by movements.
        lines = [{k: v for k, v in i.items() if k not in ("issued_qty", "issued_value")}
                 for i in current.get("inputs", [])]
        valid = [i for i in lines if _positive(i)]
        current["inputs"] = [{**i, "issued_qty": 0.0}
                             for i in [*merge_inputs(valid), *(i for i in lines if not _positive(i))]]
    elif event_type == "mfg.order.started":
        current["status"] = "in_progress"
        current["is_in_production"] = True
    elif event_type == "mfg.order.on_hold":
        current["status"] = "on_hold"
        current["is_in_production"] = False
        if data.get("reason"):
            current["hold_reason"] = data["reason"]
    elif event_type == "mfg.order.resumed":
        current["status"] = "in_progress"
        current["is_in_production"] = True
        current.pop("hold_reason", None)
    elif event_type == "mfg.order.issued":
        # Issuing components auto-advances a planned/on-hold run to In Progress.
        if current.get("status") in (None, "planned", "on_hold"):
            current["status"] = "in_progress"
            current["is_in_production"] = True
        current.pop("hold_reason", None)
        # The same item may appear more than once in an event; every occurrence counts.
        issued: dict = {}
        values: dict = {}
        for i in data.get("items", []):
            issued[i.get("item_id")] = issued.get(i.get("item_id"), 0.0) + float(i.get("quantity") or 0)
            if "value" in i:
                values[i.get("item_id")] = Decimal(str(values.get(i.get("item_id")) or 0)) + Decimal(str(i["value"]))
        for inp in current.get("inputs", []):
            item_id = inp.get("item_id")
            if item_id in issued:
                # A component issued with its value keeps that value for a return; one issued
                # without it (an older release) keeps none, and cannot be returned.
                if item_id in values and (inp.get("issued_value") is not None or not inp.get("issued_qty")):
                    _add(inp, "issued_value", values[item_id])
                else:
                    inp.pop("issued_value", None)
                inp["issued_qty"] = float(inp.get("issued_qty") or 0) + issued.pop(item_id)
        if "value" in data:
            _add(current, "wip_issued", data["value"])
            if data.get("wip_account_code"):
                current["wip_account_code"] = data["wip_account_code"]
        elif data.get("items"):
            current["wip_untracked"] = True
    elif event_type == "mfg.order.returned":
        returned = {i.get("item_id"): i for i in data.get("items", [])}
        for inp in current.get("inputs", []):
            line = returned.pop(inp.get("item_id"), None)
            if line is not None:
                inp["issued_qty"] = max(0.0, round(float(inp.get("issued_qty") or 0) - float(line["quantity"]), 9))
                _add(inp, "issued_value", -Decimal(str(line["value"])))
        _add(current, "wip_issued", -Decimal(str(data["value"])))
    elif event_type == "mfg.order.received":
        current["received_qty"] = float(current.get("received_qty") or 0) + float(data.get("quantity") or 0)
        lot_id = data.get("lot_item_id")
        if lot_id:
            lots = list(current.get("received_lots") or [])
            if lot_id not in lots:
                lots.append(lot_id)
            current["received_lots"] = lots
        if "value" in data:
            _add(current, "wip_transferred", data["value"])
            current["receipts"] = [*(current.get("receipts") or []), {
                "lot_item_id": lot_id, "quantity": float(data.get("quantity") or 0), "value": _money(data["value"])}]
        else:
            current["wip_untracked"] = True
    elif event_type == "mfg.order.scheduled":
        # Phase-A scheduling: apply only the keys provided (a blank value clears the field).
        for key in ("due_date", "planned_start", "priority"):
            if key in data:
                current[key] = data[key] or None
    elif event_type == "mfg.order.receipt_undone":
        lot_id = data.get("lot_item_id")
        current["received_qty"] = max(0.0, round(float(current.get("received_qty") or 0) - float(data["quantity"]), 9))
        current["received_lots"] = [lot for lot in current.get("received_lots") or [] if lot != lot_id]
        current["receipts"] = [r for r in current.get("receipts") or [] if r.get("lot_item_id") != lot_id]
        _add(current, "wip_transferred", -Decimal(str(data["value"])))
    elif event_type == "mfg.order.reopened":
        before = (current.pop("closing", None) or {}).get("before") or {}
        for key in _CLOSED:
            if key in before:
                current[key] = before[key]
            else:
                current.pop(key, None)
    elif event_type == "mfg.order.completed":
        if data.get("closing") is not None:
            current["closing"] = {**data["closing"], "before": {k: current[k] for k in _CLOSED if k in current}}
        current["status"] = "completed"
        current["is_in_production"] = False
        if data.get("actual_outputs") is not None:
            current["actual_outputs"] = data["actual_outputs"]
        if data.get("waste") is not None:
            current["waste"] = data["waste"]
        if data.get("labor_hours") is not None:
            current["labor_hours"] = data["labor_hours"]
        # Completion clears the run: everything issued is now finished goods or waste.
        if "transferred" in data:
            current["wip_transferred"] = _money(data["transferred"])
            current["wip_wasted"] = _money(data.get("wasted"))
    elif event_type == "mfg.order.wip_opened":
        # An older run's value, reconstructed from its own history when it was settled: in all,
        # and for each component, so a return gives back exactly what each took.
        _issued_values(current, data)
        current["wip_issued"] = _money(data.get("issued"))
        current["wip_transferred"] = _money(data.get("transferred"))
        current["receipts"] = list(data.get("receipts") or [])
        if data.get("wip_account_code"):
            current["wip_account_code"] = data["wip_account_code"]
        current.pop("wip_untracked", None)
    elif event_type == "mfg.order.wip_reconciled":
        # The run was issued what the user stated, and output received before it took the
        # share the reconciliation recorded for each lot.
        _issued_values(current, data)
        current["wip_issued"] = _money(data.get("issued"))
        current["wip_transferred"] = _money(data.get("transferred"))
        current.pop("wip_wasted", None)
        current["receipts"] = list(data.get("receipts") or [])
        if data.get("wip_account_code"):
            current["wip_account_code"] = data["wip_account_code"]
        current.pop("wip_untracked", None)
        current.pop("wip_unresolved", None)
    elif event_type == "mfg.operation.recorded":
        current.update({"entity_type": "mfg_operation", **data})
    elif event_type == "mfg.order.wip_unresolved":
        current["wip_unresolved"] = data.get("reason") or "unresolved"
    elif event_type == "mfg.order.cancelled":
        current["status"] = "cancelled"
        current["is_in_production"] = False
        if data.get("reason"):
            current["cancel_reason"] = data["reason"]
    else:
        raise ValueError(f"Unsupported mfg event: {event_type}")

    return current
