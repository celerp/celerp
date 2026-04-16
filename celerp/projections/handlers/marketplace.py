# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def apply_marketplace_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "mp.listing.created":
        current.update({"entity_type": "mp_listing", **data})
        current.setdefault("status", "draft")
        current.setdefault("is_on_marketplace", False)
    elif event_type == "mp.listing.updated":
        for field, change in data["fields_changed"].items():
            current[field] = change.get("new")
    elif event_type == "mp.listing.published":
        current["status"] = "published"
        current["is_on_marketplace"] = True
    elif event_type == "mp.listing.unpublished":
        current["status"] = "unpublished"
        current["is_on_marketplace"] = False
        if data.get("reason"):
            current["unpublish_reason"] = data["reason"]

    elif event_type == "mp.order.received":
        current.update({"entity_type": "mp_order", **data})
        current.setdefault("status", "received")
    elif event_type == "mp.order.fulfilled":
        current["status"] = "fulfilled"
        if data.get("fulfillment_ref"):
            current["fulfillment_ref"] = data["fulfillment_ref"]
    elif event_type == "mp.order.cancelled":
        current["status"] = "cancelled"
        if data.get("reason"):
            current["cancel_reason"] = data["reason"]
    else:
        raise ValueError(f"Unsupported marketplace event: {event_type}")

    return current
