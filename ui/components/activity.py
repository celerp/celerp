# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared activity feed helpers.

Single source of truth for event type labels, time formatting, entity URL
resolution, and detail extraction from ledger entry data dicts.

All activity/history sections should use ``activity_table()`` for DRY rendering.
"""

from __future__ import annotations

from fasthtml.common import *
from ui.i18n import t, get_lang

EVENT_TYPE_LABELS: dict[str, str] = {
    "item.created": "Item added",
    "item.updated": "Item updated",
    "item.deleted": "Item deleted",
    "item.quantity.adjusted": "Quantity adjusted",
    "item.quantity_adjusted": "Quantity adjusted",
    "item.transferred": "Item transferred",
    "item.expired": "Item expired",
    "item.reserved": "Item reserved",
    "item.unreserved": "Item unreserved",
    "item.pricing.set": "Price updated",
    "item.status.set": "Status changed",
    "item.split": "Item split",
    "item.merged": "Items merged",
    "item.transform": "Item transformed",
    "item.source_deactivated": "Merged into another item",
    "item.consumed": "Consumed in production",
    "item.produced": "Produced",
    "doc.created": "Document created",
    "doc.updated": "Document updated",
    "doc.finalized": "Document finalized",
    "doc.paid": "Payment recorded",
    "doc.voided": "Document voided",
    "doc.sent": "Document sent",
    "doc.marked_sent": "Marked as sent",
    "doc.converted": "Document converted",
    "doc.converted_to_bill": "Converted to bill",
    "doc.payment.received": "Payment received",
    "doc.payment.refunded": "Payment refunded",
    "doc.received": "Goods received",
    "doc.line_received": "Line item received",
    "doc.line_returned": "Line item returned",
    "doc.items_returned": "Items returned",
    "doc.shared": "Share link created",
    "doc.reverted_to_draft": "Reverted to draft",
    "contact.created": "Contact added",
    "contact.updated": "Contact updated",
    "deal.created": "Deal created",
    "deal.updated": "Deal updated",
    "deal.won": "Deal won",
    "deal.lost": "Deal lost",
    "memo.created": "Memo created",
    "memo.returned": "Memo returned",
    "scan.checked_in": "Scanned in",
    "scan.checked_out": "Scanned out",
}


def event_label(event_type: str) -> str:
    """Human label for a ledger event_type string."""
    return EVENT_TYPE_LABELS.get(
        event_type,
        event_type.replace(".", " ").replace("_", " ").title(),
    )


def relative_time(ts: str) -> str:
    """Format an ISO timestamp as a relative string (e.g. '3h ago')."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        s = int((now - dt).total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ts[:10] if ts else ""


def entity_url(entity_id: str) -> str:
    """Return the UI URL for an entity_id, or '' if unknown."""
    if not entity_id:
        return ""
    if entity_id.startswith("item:"):
        return f"/inventory/{entity_id}"
    if entity_id.startswith("doc:"):
        return f"/docs/{entity_id}"
    if entity_id.startswith("list:"):
        return f"/lists/{entity_id}"
    if entity_id.startswith("contact:"):
        return f"/contacts/{entity_id}"
    if entity_id.startswith("deal:"):
        return f"/crm/deals/{entity_id}"
    if entity_id.startswith("je:auto:"):
        # je:auto:{doc_id}:{op} — link back to the source document
        rest = entity_id[len("je:auto:"):]  # e.g. "doc:INV-2026-0001:fin"
        parts = rest.rsplit(":", 1)
        if len(parts) == 2:
            return f"/docs/{parts[0]}"
    return ""


# Event types that are system-internal and should not appear in user-facing history.
_SYSTEM_EVENT_TYPES = frozenset({
    "acc.journal_entry.created",
    "acc.journal_entry.posted",
    "acc.journal_entry.voided",
})

# metadata_.reason values that mark an event as a mechanical side-effect of a
# split/transform/merge operation. These rows are suppressed from the activity feed
# because the parent operation event (item.split / item.transform / item.merged) already
# provides a clean summary.
_OPERATION_NOISE_REASONS = frozenset({
    "consumed_by_split",
    "consumed_by_transform",
    "from_split",
    "from_transform",
    "from_merge",
})

# Event types that are self-describing via their label; detail column intentionally blank.
_SELF_DESCRIBING_EVENT_TYPES = frozenset({
    "doc.finalized",
    "doc.voided",
    "doc.reverted_to_draft",
    "doc.shared",
    "doc.converted",
    "doc.converted_to_bill",
    "item.created",
    "item.deleted",
    "contact.created",
    "deal.created",
    "deal.won",
    "deal.lost",
    "memo.created",
    "memo.returned",
})

_SYSTEM_FIELDS = frozenset({"updated_at", "created_at"})

# Human-readable labels for field keys shown in activity change summaries.
_FIELD_LABELS: dict[str, str] = {
    "contact_name": "Contact",
    "contact_company_name": "Contact company",
    "contact_id": "Contact",
    "commission_contact_id": "Commission contact",
    "contact_billing_address": "Billing address",
    "contact_shipping_address": "Shipping address",
    "contact_phone": "Phone",
    "contact_email": "Email",
    "contact_tax_id": "Tax ID",
    "payment_terms": "Payment terms",
    "ref_id": "Reference",
    "due_date": "Due date",
    "issue_date": "Issue date",
    "amount_outstanding": "Amount outstanding",
    "total": "Total",
    "status": "Status",
    "description": "Description",
    "customer_note": "Customer note",
    "internal_note": "Internal note",
    "currency": "Currency",
    "price_list": "Price list",
    "terms_text": "Terms",
    "shipping_attn": "Ship to",
    "doc_number": "Doc number",
    "name": "Name",
    "sku": "SKU",
    "quantity": "Quantity",
    "price": "Price",
    "category": "Category",
    "barcode": "Barcode",
    "location": "Location",
}

# ID fields that carry a raw entity-ID value; suppressed when a companion
# human-readable field is present in the same changeset.
_ID_FIELD_COMPANIONS: dict[str, str] = {
    "contact_id": "contact_name",
    "commission_contact_id": "commission_contact_name",
}


def detail_from_entry(data: dict, event_type: str) -> str:
    """Extract a short human-readable detail string from a ledger entry's data dict."""
    if not data or not isinstance(data, dict):
        return ""
    fields_changed = data.get("fields_changed", {})
    if fields_changed and isinstance(fields_changed, dict):
        summary = _fields_changed_summary(fields_changed)
        if summary:
            return summary
        # fields_changed was present but all entries were noise (empty→empty etc.)
        # Fall through to event-type-specific detail - no generic "Updated" fallback.
    if event_type in ("item.quantity.adjusted", "item.quantity_adjusted"):
        new_qty = data.get("new_qty") or data.get("quantity")
        if new_qty is not None:
            return f"Qty → {new_qty}"
    if event_type == "item.transferred":
        loc = data.get("location_name") or data.get("location_id", "")
        return f"→ {loc}" if loc else ""
    if event_type == "item.expired":
        reason = data.get("reason", "")
        return str(reason)[:60] if reason else ""
    if event_type == "item.pricing.set":
        price_type = data.get("price_type", "")
        new_price = data.get("new_price")
        label = price_type.replace("_", " ").title() if price_type else "Price"
        return f"{label} → {new_price}" if new_price is not None else label
    if event_type == "item.status.set":
        new_status = data.get("new_status", "")
        return f"→ {new_status}" if new_status else ""
    if event_type == "item.split":
        child_skus = data.get("child_skus", [])
        if child_skus:
            return f"→ {', '.join(str(s) for s in child_skus)}"
        child_ids = data.get("child_ids", [])
        return f"{len(child_ids)} children" if child_ids else ""
    if event_type == "item.transform":
        child_sku = data.get("child_sku", "")
        child_category = data.get("child_category", "")
        parts = []
        if child_sku:
            parts.append(f"→ {child_sku}")
        if child_category:
            parts.append(f"({child_category})")
        return " ".join(parts) if parts else ""
    if event_type == "item.merged":
        sources = data.get("source_entity_ids", [])
        source_skus = data.get("source_skus", {})
        qty = data.get("resulting_qty")
        if source_skus:
            sku_list = ", ".join(str(v) for v in source_skus.values())
            parts = [f"Merged from: {sku_list}"]
        elif sources:
            parts = [f"From {len(sources)} source items"]
        else:
            parts = []
        if qty is not None:
            parts.append(f"qty={qty}")
        return " - ".join(parts) if parts else ""
    if event_type == "item.source_deactivated":
        merged_into_sku = data.get("merged_into_sku", "")
        merged_into = data.get("merged_into", "")
        original_qty = data.get("original_qty")
        label = merged_into_sku or merged_into
        parts = [f"→ {label}"] if label else []
        if original_qty is not None:
            parts.append(f"qty was {original_qty}")
        return " - ".join(parts) if parts else ""
    if event_type == "item.consumed":
        qty = data.get("quantity_consumed")
        return f"Qty consumed: {qty}" if qty is not None else ""
    if event_type == "item.produced":
        qty = data.get("quantity_produced")
        return f"Qty produced: {qty}" if qty is not None else ""
    # --- Document-specific events ---
    doc_ref = data.get("doc_number") or data.get("ref_id") or data.get("ref") or ""
    if event_type == "doc.created":
        doc_type = data.get("doc_type", "")
        label = doc_type.replace("_", " ").title() if doc_type else "Document"
        return f"{label} {doc_ref}" if doc_ref else label
    if event_type == "doc.finalized":
        return doc_ref or ""
    if event_type == "doc.sent":
        recipient = data.get("sent_to") or data.get("recipient") or ""
        parts = []
        if doc_ref:
            parts.append(doc_ref)
        if recipient:
            parts.append(f"to {recipient}")
        return " ".join(parts) if parts else ""
    if event_type == "doc.marked_sent":
        return doc_ref or ""
    if event_type == "doc.paid":
        amount = data.get("amount")
        parts = [doc_ref] if doc_ref else []
        if amount is not None:
            parts.append(f"amount: {amount}")
        return " - ".join(parts) if parts else ""
    if event_type == "doc.payment.received":
        amount = data.get("amount")
        parts = [doc_ref] if doc_ref else []
        if amount is not None:
            parts.append(f"amount: {amount}")
        return " - ".join(parts) if parts else ""
    if event_type == "doc.payment.refunded":
        amount = data.get("amount")
        parts = [doc_ref] if doc_ref else []
        if amount is not None:
            parts.append(f"refunded: {amount}")
        return " - ".join(parts) if parts else ""
    if event_type == "doc.voided":
        reason = data.get("reason", "")
        parts = [doc_ref] if doc_ref else []
        if reason:
            parts.append(str(reason)[:80])
        return " - ".join(parts) if parts else ""
    if event_type == "doc.reverted_to_draft":
        reason = data.get("reason", "")
        parts = [doc_ref] if doc_ref else []
        if reason:
            parts.append(str(reason)[:80])
        return " - ".join(parts) if parts else ""
    if event_type == "doc.line_received":
        desc = data.get("description") or data.get("sku") or ""
        qty = data.get("quantity")
        loc = data.get("location_name") or data.get("location_id") or ""
        parts = []
        if desc:
            parts.append(desc)
        if qty is not None:
            parts.append(f"qty: {qty}")
        if loc:
            parts.append(f"at {loc}")
        return " - ".join(parts) if parts else ""
    if event_type == "doc.line_returned":
        desc = data.get("description") or data.get("sku") or ""
        qty = data.get("quantity")
        parts = []
        if desc:
            parts.append(desc)
        if qty is not None:
            parts.append(f"qty: {qty}")
        return " - ".join(parts) if parts else ""
    if event_type == "doc.converted_to_bill":
        bill_ref = data.get("bill_number") or data.get("bill_ref") or ""
        return f"Bill #{bill_ref}" if bill_ref else ""
    if event_type == "doc.converted":
        target_ref = data.get("target_ref") or data.get("target_doc_number") or ""
        return f"→ {target_ref}" if target_ref else (doc_ref or "")
    if event_type == "doc.updated":
        return _fields_changed_summary(fields_changed)
    if event_type == "doc.shared":
        return doc_ref or ""
    return ""


def _fields_changed_summary(fields_changed: dict) -> str:
    """Compact summary of field changes from a ledger data dict.

    Scalar changes: "field: old → new" (values capped at 40 chars).
    Complex changes (list/dict): "Lines edited", "Contact updated", etc.
    Unknown complex: "field updated".
    """
    if not fields_changed or not isinstance(fields_changed, dict):
        return ""
    user_fields = {k: v for k, v in fields_changed.items()
                   if k not in _SYSTEM_FIELDS and k not in {"attachments", "preview_image_id"}}
    if not user_fields:
        return ""

    # Suppress raw-ID fields when the companion human-readable field is also present.
    for id_field, companion in _ID_FIELD_COMPANIONS.items():
        if id_field in user_fields and companion in user_fields:
            del user_fields[id_field]

    _COMPLEX_LABELS: dict[str, str] = {
        "line_items": "Lines edited",
        "received_items": "Received items updated",
        "fulfilled_items": "Fulfilled items updated",
        "taxes": "Taxes updated",
        "addresses": "Address updated",
        "payments": "Payments updated",
        "tc_items": "T&C items updated",
        "attributes": "Attributes updated",
    }

    scalar_parts: list[str] = []
    complex_labels: list[str] = []

    for k, change in user_fields.items():
        if isinstance(change, dict):
            old = change.get("old")
            new = change.get("new")
        else:
            old, new = None, change

        # If either value is a list or dict, treat as complex
        if isinstance(old, (list, dict)) or isinstance(new, (list, dict)):
            if k == "line_items" and isinstance(new, list):
                if isinstance(old, list):
                    # True diff: old and new both present
                    def _li_key(li):
                        return li.get("entity_id") or li.get("item_id") or li.get("sku") or li.get("name") or ""
                    def _li_label(li):
                        name = li.get("name") or li.get("sku") or ""
                        qty = li.get("quantity")
                        return f"{name} ×{qty}" if qty is not None else name

                    old_by_key = {_li_key(li): li for li in old if isinstance(li, dict)}
                    new_by_key = {_li_key(li): li for li in new if isinstance(li, dict)}
                    added = [_li_label(li) for k2, li in new_by_key.items() if k2 not in old_by_key]
                    removed = [_li_label(li) for k2, li in old_by_key.items() if k2 not in new_by_key]
                    changed = []
                    for k2, new_li in new_by_key.items():
                        if k2 in old_by_key:
                            old_li = old_by_key[k2]
                            old_qty = old_li.get("quantity")
                            new_qty = new_li.get("quantity")
                            old_price = old_li.get("unit_price") or old_li.get("price")
                            new_price = new_li.get("unit_price") or new_li.get("price")
                            name = new_li.get("name") or new_li.get("sku") or ""
                            if old_qty != new_qty:
                                changed.append(f"{name} ×{old_qty}→{new_qty}")
                            elif old_price != new_price:
                                changed.append(f"{name} {old_price}→{new_price}")
                    parts = (
                        [f"+{x}" for x in added[:2]]
                        + [f"~{x}" for x in changed[:2]]
                        + [f"-{x}" for x in removed[:2]]
                    )
                    overflow = (len(added) + len(changed) + len(removed)) - len(parts)
                else:
                    # No old state - show final line state with qty/price
                    parts = []
                    for li in new:
                        if not isinstance(li, dict):
                            continue
                        name = li.get("name") or li.get("sku") or ""
                        if not name:
                            continue
                        qty = li.get("quantity")
                        price = li.get("unit_price") or li.get("price")
                        detail = name
                        if qty is not None:
                            detail += f" ×{qty}"
                        if price is not None:
                            detail += f" @ {price}"
                        parts.append(detail)
                    overflow = max(0, len(parts) - 3)
                if parts:
                    summary = "; ".join(parts[:3])
                    if overflow > 0:
                        summary += f" +{overflow} more"
                    complex_labels.append(summary)
                    continue
            label = _COMPLEX_LABELS.get(k) or f"{k.replace('_', ' ').title()} updated"
            if label not in complex_labels:
                complex_labels.append(label)
            continue

        # Treat None / "" / whitespace-only as semantically empty; skip no-op changes.
        def _empty(v) -> bool:
            return v is None or (isinstance(v, str) and not v.strip())

        if _empty(old) and _empty(new):
            continue
        if old == new:
            continue

        old_str = (str(old)[:40] + "…" if old is not None and len(str(old)) > 40 else str(old)) if not _empty(old) else "none"
        new_str = (str(new)[:40] + "…" if new is not None and len(str(new)) > 40 else str(new)) if not _empty(new) else "none"
        label = _FIELD_LABELS.get(k) or k.replace("_", " ").title()
        if not _empty(new):
            scalar_parts.append(f"{label}: {old_str} → {new_str}")
        else:
            scalar_parts.append(label)

    all_parts = scalar_parts + complex_labels
    if not all_parts:
        return ""
    suffix = "…" if len(all_parts) > 4 else ""
    return ", ".join(all_parts[:4]) + suffix


def format_timestamp(ts: str) -> str:
    """Format an ISO timestamp as 'YYYY-MM-DD HH:MM' for activity tables."""
    if not ts:
        return ""
    # Handle various ISO formats: '2026-03-25T07:30:01+00:00', '2026-03-25 07:30:01'
    clean = ts.replace("T", " ").replace("Z", "")
    # Strip timezone offset if present (e.g. '+00:00')
    if "+" in clean and clean.index("+") > 10:
        clean = clean[:clean.index("+")]
    elif clean.count("-") >= 3:
        # Handle negative UTC offset
        parts = clean.rsplit("-", 1)
        if len(parts) == 2 and ":" in parts[1] and len(parts[1]) <= 6:
            clean = parts[0]
    # Return date + time (minute precision)
    return clean[:16].strip()


def _event_display(entry: dict) -> tuple[str, str]:
    """Return (display_text, url) for the Event column.

    Produces linked entity references like 'Contact "Noah Severs" updated'.
    Works with both ledger entries (entity_name) and dashboard activities (name).
    """
    event_type = str(entry.get("event_type") or "")
    label = event_label(event_type)
    entity_id = str(entry.get("entity_id") or "")
    entity_name = str(entry.get("entity_name") or entry.get("name") or "")
    url = entity_url(entity_id)

    if entity_name:
        return f'{label}: "{entity_name}"', url
    return label, url


def _is_uuid(s: str) -> bool:
    """Return True if string looks like a raw UUID (should not be shown to users)."""
    import re
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s, re.I))


def activity_table(ledger: list[dict], *, title: str = "Recent Activity",
                   section_cls: str = "section", icon: str = "",
                   empty_msg: str = "No activity yet.",
                   max_display: int | None = None,
                   history_url: str | None = None) -> FT:
    """Unified DRY activity table used by all detail pages and dashboard.

    Columns: Event (linked to entity) | When (timestamp) | Details | User
    """
    EMPTY = "--"

    if not ledger:
        header_parts: list = []
        if icon:
            header_parts.append(Span(icon, cls="section-icon"))
        header_parts.append(H3(title, cls="section-title"))
        return Div(
            Div(*header_parts, cls="section-header") if icon else H3(title, cls="section-title"),
            P(empty_msg, cls="empty-state-msg"),
            cls=section_cls,
        )

    def _row(e: dict) -> FT | None:
        raw_type = str(e.get("event_type") or "")

        # Suppress system-internal events entirely
        if raw_type in _SYSTEM_EVENT_TYPES:
            return None

        # Suppress mechanical side-effect events from split/transform/merge operations.
        # The summary event (item.split / item.transform / item.merged) provides the
        # user-facing record; individual item.created / item.pricing.set / item.status.set
        # rows would only add noise.
        metadata = e.get("metadata_") or e.get("metadata") or {}
        if isinstance(metadata, dict):
            reason = metadata.get("reason", "")
            if reason in _OPERATION_NOISE_REASONS:
                return None
            # item.created with parent_id = child created by split or transform;
            # shown via parent's item.split / item.transform event instead.
            if raw_type == "item.created" and metadata.get("parent_id"):
                return None

        display_text, url = _event_display(e)
        event_cell = Td(A(display_text, href=url, cls="table-link") if url else display_text)

        ts_raw = str(e.get("ts") or "")
        ts_display = format_timestamp(ts_raw) or EMPTY
        when_cell = Td(ts_display)

        data = e.get("data") or {}
        detail = detail_from_entry(data, raw_type) if isinstance(data, dict) else ""

        # Drop rows that carried only noise (empty→empty field changes with no other detail)
        if not detail and isinstance(data, dict) and data.get("fields_changed"):
            return None

        # Self-describing events: label carries all info, detail column blank
        if not detail and raw_type in _SELF_DESCRIBING_EVENT_TYPES:
            detail_cell = Td("")
        else:
            detail_cell = Td(detail or EMPTY, cls="activity-detail-cell")

        actor = str(e.get("actor_name") or e.get("actor") or e.get("actor_id") or "")
        user_cell = Td(actor if (actor and not _is_uuid(actor)) else EMPTY)

        return Tr(event_cell, when_cell, user_cell, detail_cell)

    # Filter first, then slice — so max_display counts meaningful rows only
    all_rows = [r for e in ledger if (r := _row(e)) is not None]
    display_rows = all_rows[:max_display] if max_display else all_rows
    threshold = max_display or len(all_rows)

    header_parts = []
    if icon:
        header_parts.append(Span(icon, cls="section-icon"))
    header_parts.append(H3(title, cls="section-title"))

    footer_text = f"Showing last {len(display_rows)} events"
    if len(all_rows) >= threshold:
        footer = P(
            A(footer_text, href=history_url, cls="table-link") if history_url else footer_text,
            cls="table-footer-note",
        )
    else:
        footer = ""

    return Div(
        Div(*header_parts, cls="section-header") if icon else H3(title, cls="section-title"),
        Table(
            Thead(Tr(Th(t("th.event")), Th(t("th.when")), Th(t("th.user")), Th(t("th.details")))),
            Tbody(*display_rows),
            cls="data-table",
        ),
        footer,
        cls=section_cls,
    )
