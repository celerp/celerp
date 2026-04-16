# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy


def apply_manufacturing_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "mfg.order.created":
        current.update({"entity_type": "mfg_order", **data})
        current.setdefault("status", "created")
        current.setdefault("steps_completed", [])
        current.setdefault("is_in_production", False)
        current.setdefault("actual_outputs", [])
    elif event_type == "mfg.order.started":
        current["status"] = "started"
        current["is_in_production"] = True
    elif event_type == "mfg.step.completed":
        current.setdefault("steps_completed", [])
        if data["step_id"] not in current["steps_completed"]:
            current["steps_completed"].append(data["step_id"])
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
    elif event_type == "bom.created":
        current.update({"entity_type": "bom", **data})
        current.setdefault("components", [])
    elif event_type == "bom.updated":
        current.update(data)
    elif event_type == "bom.deleted":
        current["deleted"] = True
    else:
        raise ValueError(f"Unsupported mfg event: {event_type}")

    return current
