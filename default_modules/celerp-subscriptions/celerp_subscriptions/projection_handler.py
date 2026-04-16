# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def apply_subscription_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "sub.created":
        current.update({"entity_type": "subscription", **data})
        current.setdefault("status", "active")
    elif event_type == "sub.updated":
        for field, change in data.get("fields_changed", {}).items():
            current[field] = change.get("new")
    elif event_type == "sub.paused":
        current["status"] = "paused"
    elif event_type == "sub.resumed":
        current["status"] = "active"
        if data.get("next_run"):
            current["next_run"] = data["next_run"]
    elif event_type == "sub.generated":
        current["last_run"] = data.get("generated_at")
        current["last_generated_doc_id"] = data.get("doc_id")
        if data.get("next_run"):
            current["next_run"] = data["next_run"]
    elif event_type == "sub.expired":
        current["status"] = "expired"
    else:
        raise ValueError(f"Unsupported subscription event: {event_type}")

    return current
