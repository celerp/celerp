# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy


def apply_manufacturing_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "mfg.order.created":
        current.update({"entity_type": "mfg_order", **data})
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
        current["inputs"] = [{**i, "issued_qty": float(i.get("issued_qty") or 0)} for i in current.get("inputs", [])]
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
        issued = {i.get("item_id"): float(i.get("quantity") or 0) for i in data.get("items", [])}
        for inp in current.get("inputs", []):
            if inp.get("item_id") in issued:
                inp["issued_qty"] = float(inp.get("issued_qty") or 0) + issued[inp["item_id"]]
    elif event_type == "mfg.order.received":
        current["received_qty"] = float(current.get("received_qty") or 0) + float(data.get("quantity") or 0)
        lot_id = data.get("lot_item_id")
        if lot_id:
            lots = list(current.get("received_lots") or [])
            if lot_id not in lots:
                lots.append(lot_id)
            current["received_lots"] = lots
    elif event_type == "mfg.order.scheduled":
        # Phase-A scheduling: apply only the keys provided (a blank value clears the field).
        for key in ("due_date", "planned_start", "priority"):
            if key in data:
                current[key] = data[key] or None
    elif event_type == "mfg.order.completed":
        current["status"] = "completed"
        current["is_in_production"] = False
        if data.get("actual_outputs") is not None:
            current["actual_outputs"] = data["actual_outputs"]
        if data.get("waste") is not None:
            current["waste"] = data["waste"]
        if data.get("labor_hours") is not None:
            current["labor_hours"] = data["labor_hours"]
    elif event_type == "mfg.order.cancelled":
        current["status"] = "cancelled"
        current["is_in_production"] = False
        if data.get("reason"):
            current["cancel_reason"] = data["reason"]
    else:
        raise ValueError(f"Unsupported mfg event: {event_type}")

    return current
