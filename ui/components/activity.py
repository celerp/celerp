# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
    "item.disposed": "Item disposed",
    "item.reserved": "Item reserved",
    "item.unreserved": "Item unreserved",
    "item.pricing.set": "Price updated",
    "item.status.set": "Status changed",
    "item.split": "Item split",
    "item.merged": "Items merged",
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
    return ""


def detail_from_entry(data: dict, event_type: str) -> str:
    """Extract a short human-readable detail string from a ledger entry's data dict."""
    if not data or not isinstance(data, dict):
        return ""
    fields_changed = data.get("fields_changed", {})
    if fields_changed and isinstance(fields_changed, dict):
        keys = [k for k in fields_changed if k not in {"attachments", "preview_image_id"}]
        if keys:
            preview = ", ".join(keys[:4])
            return ("Changed: " + preview + ("…" if len(keys) > 4 else ""))
    if event_type in ("item.quantity.adjusted", "item.quantity_adjusted"):
        new_qty = data.get("new_qty") or data.get("quantity")
        if new_qty is not None:
            return f"Qty → {new_qty}"
    if event_type == "item.transferred":
        loc = data.get("location_name") or data.get("location_id", "")
        return f"→ {loc}" if loc else ""
    if event_type in ("item.expired", "item.disposed"):
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
    """Compact summary of field changes from a ledger data dict."""
    if not fields_changed or not isinstance(fields_changed, dict):
        return ""
    keys = [k for k in fields_changed if k not in {"attachments", "preview_image_id"}]
    if keys:
        preview = ", ".join(keys[:4])
        return "Changed: " + preview + ("..." if len(keys) > 4 else "")
    return ""


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
                   max_display: int | None = None) -> FT:
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

    def _row(e: dict) -> FT:
        display_text, url = _event_display(e)
        event_cell = Td(A(display_text, href=url, cls="table-link") if url else display_text)

        ts_raw = str(e.get("ts") or "")
        ts_display = format_timestamp(ts_raw) or EMPTY
        when_cell = Td(ts_display)

        data = e.get("data") or {}
        raw_type = str(e.get("event_type") or "")
        detail = detail_from_entry(data, raw_type) if isinstance(data, dict) else ""
        detail_cell = Td(detail or EMPTY)

        actor = str(e.get("actor_name") or e.get("actor") or e.get("actor_id") or "")
        user_cell = Td(actor if (actor and not _is_uuid(actor)) else EMPTY)

        return Tr(event_cell, when_cell, detail_cell, user_cell)

    display = ledger[:max_display] if max_display else ledger
    threshold = max_display or len(ledger)

    header_parts = []
    if icon:
        header_parts.append(Span(icon, cls="section-icon"))
    header_parts.append(H3(title, cls="section-title"))

    footer = P(f"Showing last {len(display)} events", cls="table-footer-note") if len(ledger) >= threshold else ""

    return Div(
        Div(*header_parts, cls="section-header") if icon else H3(title, cls="section-title"),
        Table(
            Thead(Tr(Th(t("th.event")), Th(t("th.when")), Th(t("th.details")), Th(t("th.user")))),
            Tbody(*[_row(e) for e in display]),
            cls="data-table",
        ),
        footer,
        cls=section_cls,
    )
