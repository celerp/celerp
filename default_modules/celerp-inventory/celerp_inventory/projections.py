# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from copy import deepcopy

# Maps old weight_unit abbreviations to new unit names
_WEIGHT_UNIT_MAP: dict[str, str] = {
    "ct": "carat",
    "g": "gram",
    "kg": "kg",
    "oz": "oz",
    "lb": "lb",
}


def _sync_expiry_from_attributes(state: dict) -> dict:
    """Promote attributes.expiry_date → expires_at so the projection column stays current.

    Also promotes attributes.warranty_exp for electronics/automotive categories.
    Only sets expires_at when the attribute is a non-empty string; never clears it.
    """
    attrs = state.get("attributes") or {}
    expiry_val = attrs.get("expiry_date") or attrs.get("warranty_exp")
    if expiry_val and isinstance(expiry_val, str) and expiry_val.strip():
        state["expires_at"] = expiry_val.strip()
    return state


def _migrate_sell_by(state: dict) -> dict:
    """Migrate old sell_by="weight" format to unit name.

    Old format: sell_by="weight", weight_unit="ct", weight=<float>
    New format: sell_by="carat", quantity=<float>

    For items with both a piece count (quantity>1) and a weight,
    the piece count moves to attributes["pieces"] and quantity becomes the weight.
    """
    if state.get("sell_by") != "weight":
        return state
    weight_unit = state.get("weight_unit") or "ct"
    unit_name = _WEIGHT_UNIT_MAP.get(weight_unit, weight_unit)
    state["sell_by"] = unit_name
    weight = state.get("weight")
    if weight is not None:
        qty = float(state.get("quantity") or 0)
        if qty > 1:
            attrs = dict(state.get("attributes") or {})
            attrs["pieces"] = qty
            state["attributes"] = attrs
        state["quantity"] = float(weight)
    state.pop("weight", None)
    state.pop("weight_unit", None)
    return state


def apply_item_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)
    if event_type in {"item.created", "item.snapshot"}:
        current.update(data)
        current.setdefault("is_available", True)
        current.setdefault("status", "available")
        current = _migrate_sell_by(current)
        current = _sync_expiry_from_attributes(current)
    elif event_type == "item.updated":
        for field, change in data["fields_changed"].items():
            current[field] = change.get("new")
        current = _sync_expiry_from_attributes(current)
    elif event_type == "item.pricing.set":
        current[data["price_type"]] = data["new_price"]
    elif event_type == "item.status.set":
        current["status"] = data["new_status"]
    elif event_type == "item.transferred":
        current["location_id"] = data["to_location_id"]
    elif event_type == "item.quantity.adjusted":
        current["quantity"] = data["new_qty"]
    elif event_type in {"item.expired", "item.disposed"}:
        current["is_available"] = False
        current["is_expired"] = event_type == "item.expired"
    elif event_type == "item.split":
        # Parent stays available with reduced qty (qty reduction via item.quantity.adjusted)
        current["children"] = data.get("child_ids", [])
        current["child_skus"] = data.get("child_skus", [])
    elif event_type == "item.merged":
        # No-op: marker event only. Real state is set by item.created on the new item.
        pass
    elif event_type == "item.source_deactivated":
        # Emitted on source items when absorbed by a merge.
        current["is_available"] = False
        current["quantity"] = 0
        current["status"] = "merged"
        current["merged_into"] = data.get("merged_into")
    elif event_type == "item.consumed":
        current["quantity"] = max(0.0, float(current.get("quantity", 0)) - float(data["quantity_consumed"]))
    elif event_type == "item.produced":
        current["quantity"] = float(current.get("quantity", 0)) + float(data["quantity_produced"])
    elif event_type == "item.reserved":
        current["reserved_quantity"] = float(current.get("reserved_quantity", 0)) + float(data["quantity"])
    elif event_type == "item.unreserved":
        current["reserved_quantity"] = max(0.0, float(current.get("reserved_quantity", 0)) - float(data["quantity"]))
    elif event_type == "item.fulfilled":
        current["quantity"] = 0
        current["is_available"] = False
        current["status"] = "fulfilled"
        current.setdefault("fulfilled_for_docs", [])
        current["fulfilled_for_docs"].append(data["source_doc_id"])
    elif event_type == "item.fulfillment_reversed":
        current["quantity"] = float(data["quantity_restored"])
        current["is_available"] = True
        current["status"] = "available"
        doc_id = data.get("source_doc_id")
        fulfilled_docs = current.get("fulfilled_for_docs", [])
        if doc_id and doc_id in fulfilled_docs:
            fulfilled_docs.remove(doc_id)
            current["fulfilled_for_docs"] = fulfilled_docs
    elif event_type == "item.patched":
        # CSV upsert: merge data fields into existing state, then re-run migrations
        current.update(data)
        current = _migrate_sell_by(current)
        current = _sync_expiry_from_attributes(current)
    else:
        raise ValueError(f"Unsupported item event: {event_type}")
    return current
