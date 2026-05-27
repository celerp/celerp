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

_IMAGE_MIME_PREFIXES = ("image/",)

# Old attachment type → new document_tag mapping (for lazy migration)
_ATTACHMENT_TYPE_TO_TAG: dict[str, str] = {
    "image": "product_images",
    "video": "product_images",
    "certificate": "certificates",
    "view_360": "view_360",
}


def _is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def _maybe_migrate_attachments(current: dict) -> None:
    """Lazily migrate item.state["attachments"] (old format) to item.state["files"].

    Runs only when "attachments" is non-empty and "files" is absent/empty.
    Idempotent: safe to call multiple times.
    """
    old = current.get("attachments") or []
    if not old or current.get("files"):
        return
    existing_preview = current.get("preview_image_id")
    files: list[dict] = []
    first_image_done = False
    for att in old:
        att_type = att.get("type", "image")
        tag = _ATTACHMENT_TYPE_TO_TAG.get(att_type, "product_images")
        att_id = att.get("id", "")
        is_hero = False
        if tag == "product_images" and _is_image_mime(att.get("mime", "")):
            if existing_preview:
                is_hero = att_id == existing_preview
            elif not first_image_done:
                is_hero = True
                first_image_done = True
        files.append({
            "id": att_id,
            "filename": att.get("filename", ""),
            "mime": att.get("mime", ""),
            "size": att.get("size", 0),
            "url": att.get("url", ""),
            "document_tag": tag,
            "description": att.get("label") or None,
            "uploaded_at": None,
            "is_hero": is_hero,
        })
    current["files"] = files
    current["attachments"] = []


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
        current.setdefault("inventory_type", "stocked")
        # Default purchase unit = sell unit, conversion = 1 (most items bought in same unit as sold)
        if not current.get("purchase_unit") and current.get("sell_by"):
            current.setdefault("purchase_unit", current["sell_by"])
        current.setdefault("purchase_conversion_factor", 1)
        current = _migrate_sell_by(current)
        current = _sync_expiry_from_attributes(current)
    elif event_type == "item.updated":
        for field, change in data["fields_changed"].items():
            if field == "pieces":
                # pieces always lives in attributes["pieces"] — never at top-level
                attrs = dict(current.get("attributes") or {})
                new_val = change.get("new")
                if new_val is None:
                    attrs.pop("pieces", None)
                else:
                    attrs["pieces"] = new_val
                current["attributes"] = attrs
                current.pop("pieces", None)
            else:
                current[field] = change.get("new")
        current = _sync_expiry_from_attributes(current)
    elif event_type == "item.pricing.set":
        pt = data["price_type"]
        price = data["new_price"]
        if pt == "cost_total":
            current["cost_total"] = price
            current.pop("cost_price", None)  # cost_price is now derived
        elif pt == "cost_price":
            current["cost_price"] = price   # legacy path - do NOT pop cost_total here
        else:
            current[pt] = price
    elif event_type == "item.status.set":
        new_status = data["new_status"]
        current["status"] = new_status
        # Inactive statuses must also flip is_available so availability guards work correctly.
        _INACTIVE_STATUSES = frozenset({"archived", "merged", "expired", "sold"})
        if new_status in _INACTIVE_STATUSES:
            current["is_available"] = False
        else:
            current["is_available"] = True
    elif event_type == "item.transferred":
        current["location_id"] = data["to_location_id"]
        if "updated_at" in data:
            current["updated_at"] = data["updated_at"]
    elif event_type == "item.quantity.adjusted":
        current["quantity"] = data["new_qty"]
    elif event_type in {"item.expired", "item.disposed"}:  # item.disposed is legacy; maps to archived
        current["is_available"] = False
        current["is_expired"] = event_type == "item.expired"
        current["status"] = "expired" if event_type == "item.expired" else "archived"
    elif event_type == "item.split":
        # Parent stays available with reduced qty (qty reduction via item.quantity.adjusted)
        current["children"] = data.get("child_ids", [])
        current["child_skus"] = data.get("child_skus", [])
    elif event_type == "item.transform":
        current["transformed_into"] = data.get("child_id")
    elif event_type == "item.merged":
        # No-op: marker event only. Real state is set by item.created on the new item.
        pass
    elif event_type == "item.source_deactivated":
        # Emitted on source items when absorbed by a merge.
        current["is_available"] = False
        current["quantity"] = float(data.get("original_qty") or current.get("quantity") or 0)
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
        current["quantity"] = float(data.get("quantity_fulfilled", 0))
        current["quantity_fulfilled"] = float(data.get("quantity_fulfilled", 0))
        current["is_available"] = False
        current["status"] = "memo_out" if data.get("doc_type") == "memo" else "sold"
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
    elif event_type == "item.file.attached":
        _maybe_migrate_attachments(current)
        current.setdefault("files", [])
        entry: dict = {
            "id": data["file_id"],
            "filename": data["filename"],
            "mime": data["mime"],
            "size": data["size"],
            "url": data.get("url", ""),
            "document_tag": data.get("document_tag"),
            "description": data.get("description"),
            "uploaded_at": data.get("uploaded_at"),
            "is_hero": bool(data.get("is_hero", False)),
        }
        if entry["is_hero"]:
            for f in current["files"]:
                f["is_hero"] = False
            current["preview_image_id"] = entry["id"]
        current["files"].append(entry)
    elif event_type == "item.file.tagged":
        for f in current.get("files", []):
            if f.get("id") == data["file_id"]:
                f["document_tag"] = data["document_tag"]
    elif event_type == "item.file.description_updated":
        for f in current.get("files", []):
            if f.get("id") == data["file_id"]:
                f["description"] = data["description"]
    elif event_type == "item.file.deleted":
        fid = data["file_id"]
        was_hero = any(f.get("is_hero") and f.get("id") == fid for f in current.get("files", []))
        current["files"] = [f for f in current.get("files", []) if f.get("id") != fid]
        if was_hero:
            imgs = [f for f in current["files"] if _is_image_mime(f.get("mime", ""))]
            if imgs:
                imgs[0]["is_hero"] = True
                current["preview_image_id"] = imgs[0]["id"]
            else:
                current["preview_image_id"] = None
    elif event_type == "item.file.hero_set":
        fid = data["file_id"]
        for f in current.get("files", []):
            f["is_hero"] = f.get("id") == fid
        hero = next((f for f in current.get("files", []) if f.get("is_hero")), None)
        current["preview_image_id"] = hero["id"] if hero else None
    else:
        raise ValueError(f"Unsupported item event: {event_type}")
    return current
