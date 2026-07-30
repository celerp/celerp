# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

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

# Statuses where an item is available for operations (split, transform, etc.)
# Allowlist by design: unknown/new statuses are unavailable until explicitly added here.
# is_item_available() is the single source of truth — derive at read time, never store.
_ACTIVE_STATUSES: frozenset[str] = frozenset({"available", "active"})

# ── Core vs attribute partition (single source of truth for the WRITE side) ──────────────────────
# Keys that live at the TOP LEVEL of item state: identity, quantities, cost bases, lifecycle markers,
# relationships, files, and system fields. EVERYTHING ELSE is a category attribute and belongs under
# state["attributes"] — including `pieces`. Per-price-list fields (`*_price` / `*_price_total`) also
# stay top-level and are matched by suffix in `_is_core_key`.
#
# Reads flatten both locations, so top-level vs nested is invisible to normal consumers; only raw-state
# readers (e.g. merge conflict resolution) care. Normalizing on write keeps storage canonical so those
# readers stay correct. If a NEW top-level state key is ever added to the projection, add it here too —
# otherwise it would be misclassified as an attribute and relocated.
_CORE_ITEM_KEYS: frozenset[str] = frozenset({
    # the attributes container itself is top-level (holds all category attributes); a field edit may
    # replace it wholesale via fields_changed["attributes"], so it must NOT be treated as an attribute
    "attributes",
    # identity / system
    "id", "entity_id", "company_id", "sku", "name", "barcode", "category", "status",
    "created_at", "updated_at", "location_id", "location_name", "idempotency_key",
    # quantities / measures  (NOTE: `pieces` is intentionally NOT core — it lives under attributes)
    "quantity", "sell_by", "unit", "weight", "weight_unit", "gross_weight", "gross_weight_unit",
    "reserved_quantity", "quantity_fulfilled",
    # cost bases (per-list *_price fields are matched by suffix, not listed)
    "cost_total", "cost_price", "cost_base", "cost_landed", "landed_contributions",
    # reorder / planning
    "reorder_point", "reorder_qty",
    # flags / classification
    "allow_splitting", "inventory_type", "pick_method", "consignment_flag", "item_type",
    "is_expired", "expires_at", "landed_cost_kind", "recoverable",
    # purchase side
    "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor",
    # free-text core fields
    "short_description", "description", "notes", "hs_code", "batch_no",
    # relationships / lifecycle markers
    "parent_id", "parent_sku", "children", "child_skus", "merged_into", "split_from",
    "transformed_from", "transformed_into", "fulfilled_for_docs",
    "status_doc_id", "status_doc_number",
    # files / media
    "files", "attachments", "preview_image_id",
    # other structured internals
    "recipe", "workflow", "tax_codes",
})


def _is_core_key(key: str) -> bool:
    """True if `key` stays at the top level of item state (core field or a per-list price field)."""
    return key in _CORE_ITEM_KEYS or key.endswith("_price") or key.endswith("_price_total")


def _normalize_attributes(current: dict) -> None:
    """Relocate every non-core top-level field into state["attributes"] (in place).

    Category attributes (grade, type, color, …) and `pieces` belong under `attributes`; only core
    identity/quantity/cost/lifecycle/price fields stay top-level. Some producers put attributes
    top-level (a field edit, `POST /items` extra fields), which this heals so storage is canonical.
    A top-level value takes precedence over an existing nested one — it is the freshly written value.
    """
    movable = [k for k in current if k != "attributes" and not _is_core_key(k)]
    if not movable:
        return
    attrs = dict(current.get("attributes") or {})
    for k in movable:
        val = current.pop(k)
        if val is None:
            attrs.pop(k, None)
        else:
            attrs[k] = val
    current["attributes"] = attrs


def is_item_available(state: dict) -> bool:
    """Derive availability from status. Single authoritative check — no stored flag."""
    return str(state.get("status") or "").lower() in _ACTIVE_STATUSES

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


def _recompute_cost(current: dict) -> None:
    """Derive the effective cost_total = cost_base + landed cost.

    cost_base is the goods' purchase/manual cost (what the user edits). Landed cost is stored as
    per-unit contributions keyed by "<source_bill_id>::<kind>"; the total landed = Σ unit × quantity,
    so landed scales as quantity is received. cost_total stays authoritative for valuation/COGS.

    Idempotent. Bootstraps cost_base from a legacy cost_total when the split is absent, so existing
    items (cost_total only, no landed) are unaffected: cost_total == cost_base.
    """
    contribs = current.get("landed_contributions") or {}
    if current.get("cost_base") is None:
        if current.get("cost_total") is not None:
            current["cost_base"] = float(current["cost_total"])
        elif not contribs:
            return  # item has no cost set at all
        else:
            current["cost_base"] = 0.0
    base = float(current.get("cost_base") or 0)
    qty = float(current.get("quantity") or 0)
    landed_unit = sum(float(v or 0) for v in contribs.values())
    current["cost_landed"] = round(landed_unit * qty, 2)
    current["cost_total"] = round(base + current["cost_landed"], 2)
    current.pop("cost_price", None)  # always derived from cost_total at read time (flatten_item)


def _stamp_status_doc(current: dict, data: dict) -> None:
    """Keep the status <-> document pairing in lockstep with a status change.

    A doc-driven status change (fulfil, memo->invoice conversion, consignment
    receive) sends source_doc_id + doc_number and stamps the pairing; any status
    change without a source doc clears it, so the pairing can never outlive the
    status that earned it."""
    if data.get("source_doc_id"):
        current["status_doc_id"] = data["source_doc_id"]
        current["status_doc_number"] = data.get("doc_number") or ""
    else:
        current.pop("status_doc_id", None)
        current.pop("status_doc_number", None)


def apply_item_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)
    if event_type in {"item.created", "item.snapshot"}:
        current.update(data)
        # Category attributes (incl. `pieces`) are canonical under attributes["<key>"]. Some producers
        # put them TOP-LEVEL in the create payload — POST /items via extra="allow", CIF imports — so
        # relocate every non-core top-level field into `attributes`. This makes storage uniform across
        # producers and lets a snapshot self-heal any item stored top-level historically.
        _normalize_attributes(current)
        current.setdefault("status", "available")
        current.setdefault("inventory_type", "stocked")
        # Default purchase unit = sell unit, conversion = 1 (most items bought in same unit as sold)
        if not current.get("purchase_unit") and current.get("sell_by"):
            current.setdefault("purchase_unit", current["sell_by"])
        current.setdefault("purchase_conversion_factor", 1)
        current = _migrate_sell_by(current)
        current = _sync_expiry_from_attributes(current)
        _recompute_cost(current)
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
            elif field in ("cost_price", "cost_total"):
                # A manual cost edit sets the BASE cost; landed cost is then re-added on top by
                # _recompute_cost. cost_price is a unit value (× qty -> base); cost_total is the base
                # directly. Clearing either removes the base.
                new_val = change.get("new")
                if new_val in (None, ""):
                    current.pop("cost_base", None)
                    current.pop("cost_price", None)
                    current.pop("cost_total", None)
                    current.pop("cost_landed", None)
                elif field == "cost_price":
                    qty = float(current.get("quantity") or 0)
                    current["cost_base"] = round(float(new_val) * qty, 2)
                    current.pop("cost_price", None)
                else:  # cost_total
                    current["cost_base"] = round(float(new_val), 2)
            elif _is_core_key(field):
                # Core / price field — stays TOP-LEVEL. Clearing (None/"") unsets it — remove the key
                # rather than storing an empty string or null, so it reads as truly absent (issue #202).
                new_val = change.get("new")
                if new_val in (None, ""):
                    current.pop(field, None)
                else:
                    current[field] = new_val
                if field == "status":
                    # A manual status edit has no source document behind it
                    _stamp_status_doc(current, {})
            else:
                # Category attribute — canonical under attributes["<field>"], never top-level (like
                # `pieces`). Clearing removes it from BOTH locations so nothing lingers if the value was
                # historically stored top-level.
                new_val = change.get("new")
                attrs = dict(current.get("attributes") or {})
                if new_val in (None, ""):
                    attrs.pop(field, None)
                else:
                    attrs[field] = new_val
                current["attributes"] = attrs
                current.pop(field, None)
        current = _sync_expiry_from_attributes(current)
        _recompute_cost(current)
    elif event_type == "item.pricing.set":
        pt = data["price_type"]
        price = data["new_price"]
        if pt == "cost_total":
            # cost_total pricing sets the base; landed is re-added by _recompute_cost.
            current["cost_base"] = price
            current.pop("cost_price", None)
            _recompute_cost(current)
        elif pt == "cost_price":
            current["cost_price"] = price   # legacy path - do NOT pop cost_total here
        else:
            current[pt] = price
    elif event_type == "item.status.set":
        new_status = data["new_status"]
        current["status"] = new_status
        _stamp_status_doc(current, data)
    elif event_type == "item.transferred":
        current["location_id"] = data["to_location_id"]
        if "updated_at" in data:
            current["updated_at"] = data["updated_at"]
    elif event_type == "item.quantity.adjusted":
        current["quantity"] = data["new_qty"]
        # Returning consigned goods to their supplier adjusts the quantity and settles the
        # borrowed/owned question in the same breath: the emitter sends consignment_flag
        # (None once nothing is left on hand, "in" while a partial balance remains). Only
        # honour the key when present, so ordinary stock adjustments never touch the flag.
        if "consignment_flag" in data:
            current["consignment_flag"] = data["consignment_flag"]
        # A return sends the goods cost that left with the units. Only honoured when
        # present, so a plain stock correction still leaves the lot's cost alone.
        if "cost_base" in data and data["cost_base"] is not None:
            current["cost_base"] = float(data["cost_base"])
        _recompute_cost(current)  # landed cost is per-unit, so it scales with quantity
    elif event_type == "item.landed_cost.applied":
        # Absolute per-unit landed contribution for one (source bill, kind); overwrite-safe so
        # re-running allocation with changed freight self-corrects. amount=0 clears the contribution.
        contribs = dict(current.get("landed_contributions") or {})
        key = f"{data['source_bill_id']}::{data['kind']}"
        amount = float(data.get("unit_amount") or 0)
        if amount:
            contribs[key] = amount
        else:
            contribs.pop(key, None)
        current["landed_contributions"] = contribs
        _recompute_cost(current)
    elif event_type in {"item.expired", "item.disposed"}:  # item.disposed is legacy; maps to archived
        current["is_expired"] = event_type == "item.expired"
        current["status"] = "expired" if event_type == "item.expired" else "archived"
        _stamp_status_doc(current, {})
    elif event_type == "item.split":
        # Parent stays available with reduced qty (qty reduction via item.quantity.adjusted)
        current["children"] = data.get("child_ids", [])
        current["child_skus"] = data.get("child_skus", [])
    elif event_type == "item.split_from":
        # Origin marker on the child: state comes from item.created. History-only.
        current["split_from"] = data.get("parent_id")
    elif event_type == "item.transform":
        current["transformed_into"] = data.get("child_id")
    elif event_type == "item.transformed_from":
        # Origin marker on the child: state comes from item.created. History-only.
        current["transformed_from"] = data.get("parent_id")
    elif event_type == "item.merged":
        # No-op: marker event only. Real state is set by item.created on the new item.
        pass
    elif event_type == "item.source_deactivated":
        # Emitted on source items when absorbed by a merge.
        current["quantity"] = float(data.get("original_qty") or current.get("quantity") or 0)
        current["status"] = "merged"
        _stamp_status_doc(current, {})
        current["merged_into"] = data.get("merged_into")
    elif event_type == "item.consumed":
        current["quantity"] = max(0.0, float(current.get("quantity", 0)) - float(data["quantity_consumed"]))
    elif event_type == "item.produced":
        current["quantity"] = float(current.get("quantity", 0)) + float(data["quantity_produced"])
    elif event_type == "item.recipe.set":
        # Dumb full-replace: inventory stores the recipe verbatim; all interpretation
        # (validation, cost roll-up) lives in celerp-manufacturing.
        current["recipe"] = data["recipe"]
    elif event_type == "item.workflow.set":
        # Dumb full-replace: inventory stores the production workflow verbatim;
        # interpretation (worksheet, future scheduling) lives downstream. Mirrors
        # item.recipe.set (module boundary: data here, meaning elsewhere).
        current["workflow"] = data["workflow"]
    elif event_type == "item.reserved":
        current["reserved_quantity"] = float(current.get("reserved_quantity", 0)) + float(data["quantity"])
    elif event_type == "item.unreserved":
        current["reserved_quantity"] = max(0.0, float(current.get("reserved_quantity", 0)) - float(data["quantity"]))
    elif event_type == "item.fulfilled":
        current["quantity"] = float(data.get("quantity_fulfilled", 0))
        current["quantity_fulfilled"] = float(data.get("quantity_fulfilled", 0))
        current["status"] = "memo_out" if data.get("doc_type") == "memo" else "sold"
        _stamp_status_doc(current, data)
        current.setdefault("fulfilled_for_docs", [])
        current["fulfilled_for_docs"].append(data["source_doc_id"])
    elif event_type == "item.fulfillment_reversed":
        current["quantity"] = float(data["quantity_restored"])
        current["status"] = "available"
        _stamp_status_doc(current, {})
        doc_id = data.get("source_doc_id")
        fulfilled_docs = current.get("fulfilled_for_docs", [])
        if doc_id and doc_id in fulfilled_docs:
            fulfilled_docs.remove(doc_id)
            current["fulfilled_for_docs"] = fulfilled_docs
    elif event_type == "item.patched":
        # CSV upsert: merge data fields into existing state, then re-run migrations
        current.update(data)
        if "status" in data and not data.get("status_doc_id"):
            # An upsert that changes status without a source doc drops the stale pairing
            _stamp_status_doc(current, {})
        _normalize_attributes(current)  # keep attributes canonical if the upsert carried them top-level
        current = _migrate_sell_by(current)
        current = _sync_expiry_from_attributes(current)
        _recompute_cost(current)
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
        # F9 (idempotency): keep file_id unique in the projection. Re-applying the same
        # item.file.attached - a replayed event, or two call sites each emitting it (the
        # F1 class) - must not create a duplicate list entry. Update in place if the
        # file_id is already present; otherwise append.
        _existing = next((f for f in current["files"] if f.get("id") == entry["id"]), None)
        if _existing is not None:
            _existing.update(entry)
        else:
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
