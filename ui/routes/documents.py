# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from urllib.parse import urlencode

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import search_bar, EMPTY, pagination, searchable_select, breadcrumbs, status_cards, empty_state_cta, fmt_money, format_value, currency_symbol, unwrap_address
from ui.components.activity import activity_table
from ui.components.notes import notes_tab as _shared_notes_tab, note_edit_form as _shared_note_edit_form
from ui.components.files import _files_section as _shared_doc_files_section


def _enrich_doc_files(doc: dict) -> list[dict]:
    """Tag each file in a doc with its linked_ref and linked_url."""
    doc_id = doc.get("entity_id") or doc.get("id") or ""
    ref = doc.get("ref_id") or doc.get("doc_number") or ""
    url = f"/docs/{doc_id}" if doc_id else ""
    return [{**f, "linked_ref": ref, "linked_url": url} for f in doc.get("files", [])]


def _doc_files_section(entity_type: str, entity_id: str, files: list[dict], **kwargs):
    return _shared_doc_files_section(entity_type, entity_id, files, **kwargs)
from ui.components.notes import _safe_id
from ui.config import get_token as _token, get_role as _get_role
from ui.i18n import t, get_lang
from ui.routes.reports import _date_filter_bar, _parse_dates, _resolve_preset

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lists constants
# ---------------------------------------------------------------------------
_LIST_TYPES = ["quotation", "transfer", "audit"]
_LIST_DATE_FIELDS = {"date", "link_expiry"}

_PER_PAGE = 50
_PER_PAGE_OPTIONS = [25, 50, 100, 250]
_DOC_TYPES = ["invoice", "purchase_order", "bill", "receipt", "credit_note", "memo", "consignment_in", "list"]
_DOC_TYPE_PAGE_LABELS: dict[str, str] = {
    "invoice": "Invoices",
    "purchase_order": "Draft Bills & POs",
    "bill": "Vendor Bills",
    "receipt": "Receipts",
    "credit_note": "Credit Notes",
    "memo": "Consignment Out",
    "consignment_in": "Consignment In",
    "list": "Lists",
    "subscription_invoice": "Subscription Templates",
    "subscription_po": "Subscription PO Templates",
}
_DOC_TYPE_NEW_LABEL_KEYS: dict[str, str] = {
    "invoice": "btn.new_invoice",
    "purchase_order": "btn.new_purchase_order",
    "bill": "btn.new_bill",
    "receipt": "btn.new_receipt",
    "credit_note": "btn.new_credit_note",
    "memo": "btn.new_memo",
    "consignment_in": "btn.new_consignment_in",
    "list": "btn.new_list",
}


def _doc_type_new_label(doc_type: str, lang: str = "en") -> str:
    """Return localised "New <DocType>" label."""
    key = _DOC_TYPE_NEW_LABEL_KEYS.get(doc_type)
    if key:
        return t(key, lang)
    return t("btn.new_document", lang)
_DOC_STATUSES = ["draft", "sent", "paid", "overdue", "void", "open", "awaiting_payment", "converted", "expired", "bill", "final"]

# Singular labels for doc detail pages / breadcrumbs
_DOC_TYPE_SINGULAR: dict[str, str] = {
    "invoice": "Invoice",
    "purchase_order": "Purchase Order",
    "bill": "Vendor Bill",
    "receipt": "Receipt",
    "credit_note": "Credit Note",
    "memo": "Consignment Out",
    "consignment_in": "Consignment In",
    "list": "List",
    "subscription_invoice": "Subscription Template",
    "subscription_po": "Subscription PO Template",
}


# Maps doc_type → sidebar nav key. Used by both list and detail pages so the
# sidebar highlight is always correct regardless of URL structure.
_DOC_TYPE_NAV_KEY: dict[str, str] = {
    "invoice": "invoices",
    "memo": "memos",
    "purchase_order": "purchase-orders",
    "bill": "vendor-bills",
    "consignment_in": "consignment-in",
    "credit_note": "credit-notes",
    "receipt": "receipts",
    "list": "lists",
    "subscription_invoice": "subscriptions_sales",
    "subscription_po": "subscriptions_purchasing",
}


def _doc_nav_key(doc_type: str) -> str:
    return _DOC_TYPE_NAV_KEY.get(doc_type, "invoices")


def _doc_section_label(doc_type: str) -> str:
    """Section label for breadcrumb (plural)."""
    return _DOC_TYPE_PAGE_LABELS.get(doc_type, "Documents")


# ---------------------------------------------------------------------------
# Inline fulfillment helpers (no celerp-fulfillment module needed)
# ---------------------------------------------------------------------------

def _render_fulfill_section(doc: dict):
    """Fulfill / Revert Fulfillment button.

    Revert: shows whenever fulfillment_status == "fulfilled" (no status restriction - even void docs).
    Fulfill: hidden only on draft and void statuses; shown on all other statuses.
    Requires celerp-inventory to be installed.
    """
    from celerp.modules.loader import loaded_modules
    from celerp_docs.doc_constants import UNFULFILLABLE_STATUSES, TEMPLATE_DOC_TYPES
    if not any(m["name"] == "celerp-inventory" for m in loaded_modules()):
        return ""
    if doc.get("doc_type") in TEMPLATE_DOC_TYPES:
        return ""  # Subscription templates are never fulfilled
    if doc.get("doc_type") in ("credit_note", "bill"):
        return ""  # CNs use Receive Returns; bills receive INTO stock, never deduct
    entity_id = doc.get("entity_id") or doc.get("id") or ""
    # CSS IDs cannot contain colons (e.g. "doc:PF-2604-0002") - sanitize for use as selector
    cid_safe = f"fulfill-toggle-{entity_id}".replace(":", "-")
    fs = doc.get("fulfillment_status") or "unfulfilled"
    if fs == "fulfilled":
        # Revert always shows when fulfilled, regardless of doc status
        return Div(
            Form(
                Button(t("btn.revert_fulfillment"),
                    cls="btn btn--warning btn--sm",
                    title="Undo fulfillment. Returns stock to inventory. If a pick instruction exists, it will be reopened. This action is logged.",
                ),
                hx_post=f"/docs/{entity_id}/unfulfill",
                hx_confirm="Revert fulfillment? This will return stock to inventory and reopen any associated pick instruction.",
                hx_target=f"#{cid_safe}",
                hx_swap="outerHTML",
            ),
            id=cid_safe,
        )
    # Fulfill button hidden on draft and void only
    if doc.get("status") in UNFULFILLABLE_STATUSES:
        return ""
    return Div(
        Button(t("btn.fulfill_deduct_inventory"),
            hx_post=f"/docs/{entity_id}/fulfill",
            hx_confirm="Mark this document as fulfilled?",
            hx_target=f"#{cid_safe}",
            hx_swap="outerHTML",
            cls="btn btn--primary btn--sm",
            title="Mark this document as fulfilled. Use this after goods have been handed off to the customer. This action affects inventory stock levels.",
        ),
        id=cid_safe,
    )


def _render_fulfillment_badge(doc: dict):
    """Fulfillment badge - shown when doc is fulfilled."""
    fs = doc.get("fulfillment_status") or ""
    if fs == "fulfilled":
        return Span(t("doc.fulfilled"), cls="badge badge--green")
    if fs == "partial":
        return Span(t("doc.partially_fulfilled"), cls="badge badge--amber")
    return None


def _render_receive_return_section(doc: dict):
    """Receive Returns button - shown on credit notes when celerp-inventory is installed.

    Mirrors _render_fulfill_section() but for the return direction.
    Hidden on draft/void. Shows badge once return_received_items is set.
    Clicking fires the receive-return endpoint with all CN line items as hidden fields (full qty, no inline editing).
    """
    from celerp.modules.loader import loaded_modules
    if not any(m["name"] == "celerp-inventory" for m in loaded_modules()):
        return ""
    if doc.get("doc_type") != "credit_note":
        return ""
    if doc.get("status") in ("draft", "void"):
        return ""

    entity_id = doc.get("entity_id") or doc.get("id") or ""
    cid_safe = f"receive-return-{entity_id}".replace(":", "-")

    # Already received - show Revert Return Stock button (GDR: user must be able to undo)
    received = doc.get("return_received_items") or []
    if received:
        undo_form = Form(
            Button(t("btn.revert_return_stock"),
                cls="btn btn--secondary btn--sm",
                title="Revert the received return. Disposes the returned inventory items and reverses the COGS journal entry.",
            ),
            hx_delete=f"/docs/{entity_id}/receive-return",
            hx_confirm="Revert the received return? This will archive the returned inventory items and reverse the accounting entry.",
            hx_target=f"#{cid_safe}",
            hx_swap="outerHTML",
        )
        return Div(undo_form, id=cid_safe)

    # Only show button if there are stocked line items to return
    line_items = doc.get("line_items") or []
    stocked = [li for li in line_items if (li.get("sku") or "") and (li.get("sell_by") or "") not in ("service", "hour")]
    if not stocked:
        return ""

    # Hidden fields carry sku + quantity only - backend resolves all other values
    hidden_fields = []
    for i, li in enumerate(stocked):
        hidden_fields += [
            Input(type="hidden", name=f"items[{i}][sku]",      value=li.get("sku", "")),
            Input(type="hidden", name=f"items[{i}][quantity]", value=str(li.get("quantity") or 0)),
        ]

    return Div(
        Form(
            *hidden_fields,
            Button(t("btn.receive_returns"),
                cls="btn btn--primary btn--sm",
                title="Receive returned goods back into inventory. Creates new inventory items and reverses COGS.",
                hx_post=f"/docs/{entity_id}/receive-return",
                hx_confirm="Receive all returned goods back into inventory? This will create new inventory items and reverse the COGS journal entry.",
                hx_encoding="application/x-www-form-urlencoded",
                hx_target=f"#{cid_safe}",
                hx_swap="outerHTML",
                hx_include="closest form",
            ),
        ),
        id=cid_safe,
    )



    """Fulfillment badge - shown when doc is fulfilled."""
    fs = doc.get("fulfillment_status") or ""
    if fs == "fulfilled":
        return Span(t("doc.fulfilled"), cls="badge badge--green")
    if fs == "partial":
        return Span(t("doc.partially_fulfilled"), cls="badge badge--amber")
    return None


def _render_receive_goods_section(doc: dict) -> FT:
    """Receive Goods button for bills - one-click, full quantity, no partial control."""
    from celerp.modules.loader import loaded_modules
    if not any(m["name"] == "celerp-inventory" for m in loaded_modules()):
        return ""
    if doc.get("doc_type") != "bill":
        return ""
    if doc.get("status") in ("draft", "void"):
        return ""

    entity_id = doc.get("entity_id") or doc.get("id") or ""
    cid_safe = f"receive-goods-{entity_id}".replace(":", "-")

    if doc.get("received_item_ids"):
        return Div(
            Button(t("btn.revert_goods_received"),
                hx_delete=f"/docs/{entity_id}/receive-goods",
                hx_confirm="Revert goods received? This will archive all created inventory items and reverse the accounting entry.",
                hx_target=f"#{cid_safe}",
                hx_swap="outerHTML",
                cls="btn btn--danger btn--sm",
            ),
            id=cid_safe,
        )

    if doc.get("received_items"):
        return Div(Span(t("doc.goods_received"), cls="badge badge--green"), id=cid_safe)

    line_items = doc.get("line_items") or []
    if not line_items:
        return ""

    stock_count = sum(1 for li in line_items if li.get("receive_as", "stock") == "stock")
    btn_label = f"Receive Goods ({stock_count} items)" if any("receive_as" in li for li in line_items) else "Receive Goods"

    return Div(
        Button(
            btn_label,
            hx_post=f"/docs/{entity_id}/receive-goods",
            hx_confirm="Receive all goods into stock? This will create inventory items for all line items at full quantity.",
            hx_target=f"#{cid_safe}",
            hx_swap="outerHTML",
            cls="btn btn--primary btn--sm",
            title="Receive all goods on this bill into inventory at full quantity.",
        ),
        id=cid_safe,
    )


def _doc_section_url(doc_type: str) -> str:
    """URL for the doc type's listing page."""
    if doc_type == "list":
        return "/lists"
    return f"/docs?type={doc_type}" if doc_type else "/docs"


def _doc_singular_label(doc_type: str) -> str:
    """Singular label for a doc type (e.g. 'Invoice', 'Purchase Order')."""
    return _DOC_TYPE_SINGULAR.get(doc_type, doc_type.replace("_", " ").title() if doc_type else "Document")




from datetime import date as _date, timedelta as _timedelta


def _calculate_due_date(issue_date: str | None, payment_terms_name: str | None, terms_list: list[dict]) -> str | None:
    """Return ISO due_date string given an issue_date + payment_terms name + company terms list.

    Returns None if any input is missing/invalid so callers can skip the patch.
    """
    if not issue_date or not payment_terms_name:
        return None
    term = next((item for item in terms_list if item.get("name") == payment_terms_name), None)
    if term is None:
        return None
    days = term.get("days")
    if days is None:
        return None
    try:
        base = _date.fromisoformat(str(issue_date)[:10])
    except (ValueError, TypeError):
        return None
    return (base + _timedelta(days=int(days))).isoformat()


def resolve_price(item: dict, price_list: str) -> float:
    """Deterministic price lookup. No fallback chain.

    Checks the price list name directly on the item, then the conventional
    {name.lower()}_price key (e.g. "retail_price" for "Retail").
    Returns 0.0 if no price is found for this list.
    """
    val = item.get(price_list)
    if val is not None:
        return float(val)
    conventional_key = f"{price_list.lower()}_price"
    val = item.get(conventional_key)
    if val is not None:
        return float(val)
    return 0.0


async def _line_items_from_inventory(token: str, entity_ids: list[str], price_list: str = "Retail") -> list[dict]:
    """Fetch inventory items and build doc/list line items."""
    line_items = []
    for eid in entity_ids:
        try:
            item = await api.get_item(token, eid)
        except APIError:
            continue
        sku = item.get("sku", "")
        name = item.get("name", "")
        sell_by = item.get("sell_by") or "piece"
        unit_price = resolve_price(item, price_list)
        qty = float(item.get("quantity", 1)) if float(item.get("quantity", 1)) > 0 else 1
        desc = f"{sku} - {name}" if sku else name

        line_items.append({
            "description": desc,
            "quantity": qty,
            "unit_price": unit_price,
            "unit": sell_by,
            "sku": sku,
            "price_list": price_list,
            "hs_code": item.get("hs_code") or None,
            "entity_id": eid,
            "allow_splitting": bool(item.get("allow_splitting")),
        })
    return line_items


async def _company_doc_taxes(token: str) -> list[dict]:
    """Fetch company sales taxes and return them as doc_taxes dicts for new documents."""
    try:
        taxes = await api.get_taxes(token)
    except Exception:
        return []
    return [
        {"code": tax.get("name", "Tax"), "rate": float(tax.get("rate", 0)), "order": i, "is_compound": bool(tax.get("is_compound"))}
        for i, tax in enumerate(taxes)
        if tax.get("rate")
    ]


def _send_to_option_list(items: list[dict], kind: str) -> FT:
    """Render the searchable option list for send-to-modal (docs, lists, or memos)."""
    if not items:
        return Div(P(t("doc.no_results"), cls="send-to-empty"), id="send-to-options")
    rows = []
    for d in items:
        eid = d.get("id") or d.get("entity_id", "")
        if kind == "memo":
            ref = d.get("memo_number") or eid.split(":")[-1][:8]
            contact = d.get("contact_name") or d.get("customer_name") or ""
            label = f"Memo {ref}"
        elif kind == "list":
            ref = d.get("ref_id") or eid.split(":")[-1][:8]
            contact = d.get("customer_name") or d.get("receiver") or ""
            label = f"List {ref}"
        else:
            ref = d.get("ref_id") or d.get("doc_number") or eid.split(":")[-1][:8]
            contact = d.get("contact_name") or ""
            label = ref
        status = d.get("status", "")
        rows.append(
            Div(
                Input(type="radio", name="target_id", value=eid, cls="send-to-radio"),
                Span(label, cls="send-to-ref"),
                Span(contact, cls="send-to-contact") if contact else None,
                Span(status, cls=f"badge badge--{status}") if status else None,
                cls="send-to-option",
            )
        )
    return Div(*rows, id="send-to-options")


def _send_to_modal(
    type_label: str,
    create_url: str,
    add_url: str,
    search_url: str,
    drafts: list[dict],
    hidden_items: list[FT],
    kind: str,
) -> FT:
    """Unified send-to modal: create new draft or add to existing doc/list/memo."""
    return Div(
        Div(
            H3(f"Send to {type_label}", cls="modal-title"),
            # Create new draft
            Form(
                *hidden_items,
                Button(f"Create new draft {type_label.lower()}", type="submit", cls="btn btn--primary btn--full"),
                hx_post=create_url,
                hx_target="#modal-container",
                hx_swap="innerHTML",
                cls="send-to-create",
            ),
            Hr(),
            P(t("doc.or_add_to_an_existing_one"), cls="send-to-subtitle"),
            # Search input
            Input(
                type="search",
                name="q",
                placeholder=f"Search by ref, customer...",
                hx_get=search_url,
                hx_trigger="input changed delay:300ms",
                hx_target="#send-to-options",
                hx_swap="outerHTML",
                cls="form-input send-to-search",
                autocomplete="off",
            ),
            # Options list (pre-loaded with recent drafts)
            _send_to_option_list(drafts, kind),
            # Add to selected
            Form(
                *hidden_items,
                Input(type="hidden", name="target_id", value="", id="send-to-target-hidden"),
                Button(f"Add to selected {type_label.lower()}", type="submit", cls="btn btn--secondary btn--full",
                       id="send-to-add-btn", disabled=True),
                hx_post=add_url,
                hx_target="#modal-container",
                hx_swap="innerHTML",
                cls="send-to-add-form",
            ),
            # JS: sync radio selection to the hidden input + enable button
            Script("""
(function(){
  var opts = document.getElementById('send-to-options');
  var hidden = document.getElementById('send-to-target-hidden');
  var btn = document.getElementById('send-to-add-btn');
  if (!opts || !hidden) return;
  document.addEventListener('change', function(e) {
    if (e.target.name === 'target_id' && e.target.type === 'radio') {
      hidden.value = e.target.value;
      if (btn) { btn.disabled = false; btn.classList.add('btn--active'); }
    }
  });
})();
"""),
            Button(t("btn.cancel"), cls="btn btn--ghost btn--full send-to-cancel",
                   onclick="document.getElementById('modal-container').innerHTML=''"),
            cls="modal-body send-to-modal",
        ),
        id="modal-container",
        cls="modal-overlay",
    )


# Compact SVG icons for CSV export/import (16x16, matching pair)
_ICON_CSV_EXPORT = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 18 15 15"/></svg>'
_ICON_CSV_IMPORT = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="12" x2="12" y2="18"/><polyline points="9 15 12 12 15 15"/></svg>'
_ICON_PRINT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="6 9 6 2 18 2 18 9"/>'
    '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
    '<rect x="6" y="14" width="12" height="8"/></svg>'
)

_DOC_PRINT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, sans-serif; font-size: 10pt; color: #111; background: white; padding: 20mm; }
.dp-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8mm; padding-bottom: 4mm; border-bottom: 2px solid #111; }
.dp-company-name { font-size: 14pt; font-weight: 700; margin-bottom: 2mm; }
.dp-company-sub { font-size: 9pt; color: #555; line-height: 1.5; }
.dp-doc-title { font-size: 18pt; font-weight: 700; text-align: right; text-transform: uppercase; letter-spacing: 0.03em; }
.dp-doc-meta { font-size: 9pt; text-align: right; margin-top: 2mm; line-height: 1.6; color: #333; }
.dp-doc-meta strong { color: #111; }
.dp-parties { display: flex; gap: 10mm; margin-bottom: 6mm; }
.dp-party { flex: 1; }
.dp-party-label { font-size: 8pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #888; margin-bottom: 1mm; }
.dp-party-name { font-size: 10pt; font-weight: 600; }
.dp-party-sub { font-size: 9pt; color: #444; line-height: 1.5; }
.dp-lines { width: 100%; border-collapse: collapse; margin-bottom: 4mm; font-size: 9pt; }
.dp-lines thead th { background: #f5f5f5; font-weight: 700; text-align: left; padding: 1.5mm 2mm; border-bottom: 1px solid #999; }
.dp-lines thead th.r { text-align: right; }
.dp-lines tbody td { padding: 1.5mm 2mm; border-bottom: 1px solid #eee; vertical-align: top; }
.dp-lines tbody td.r { text-align: right; }
.dp-lines tbody td.mono { font-family: 'Courier New', monospace; font-size: 8.5pt; }
.dp-totals { display: flex; flex-direction: column; align-items: flex-end; margin-bottom: 6mm; }
.dp-totals table { border-collapse: collapse; min-width: 60mm; }
.dp-totals td { padding: 1mm 2mm; font-size: 9.5pt; }
.dp-totals td.label { text-align: left; color: #555; }
.dp-totals td.amount { text-align: right; font-weight: 600; }
.dp-totals tr.grand td { border-top: 2px solid #111; font-size: 11pt; font-weight: 700; padding-top: 2mm; }
.dp-notes { margin-top: 4mm; font-size: 9pt; color: #444; border-top: 1px solid #ddd; padding-top: 3mm; }
.dp-notes-label { font-weight: 700; color: #111; margin-bottom: 1mm; }
.dp-footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 3mm 20mm; border-top: 1px solid #ddd; font-size: 8pt; color: #aaa; text-align: center; background: white; }
@page { margin: 0; size: A4 portrait; }
@media print { body { padding: 15mm; } }
"""


def _doc_print_view(doc: dict) -> FT:
    """Render a standalone printable HTML page for a document."""
    from ui.components.table import fmt_money, currency_symbol

    entity_id = doc.get("id") or doc.get("entity_id") or ""
    doc_type = doc.get("doc_type", "")
    doc_number = doc.get("doc_number") or doc.get("ref_id") or entity_id
    title = doc_type.replace("_", " ").title() if doc_type else "Document"
    issue_date = (doc.get("issue_date") or "")[:10]
    due_date = (doc.get("due_date") or "")[:10]
    currency = doc.get("currency") or "USD"

    company_name = doc.get("company_name") or ""
    company_address = doc.get("company_address") or ""
    company_tax_id = doc.get("company_tax_id") or ""
    company_email = doc.get("company_email") or ""
    company_phone = doc.get("company_phone") or ""

    contact_name = doc.get("contact_name") or doc.get("customer_name") or ""
    contact_company = doc.get("contact_company_name") or ""
    contact_address = doc.get("contact_billing_address") or doc.get("contact_address") or ""
    contact_tax_id = doc.get("contact_tax_id") or ""
    contact_email = doc.get("contact_email") or ""

    line_items = doc.get("line_items") or []

    def _money(v) -> str:
        try:
            return fmt_money(v, currency)
        except Exception:
            sym = currency_symbol(currency)
            return f"{sym}{float(v or 0):,.2f}"

    has_disc = any(li.get("discount_pct") for li in line_items)
    rows = []
    for li in line_items:
        qty = li.get("quantity") or li.get("qty") or 0
        price = li.get("unit_price") or li.get("price") or 0
        disc = li.get("discount_pct") or 0
        line_total = li.get("line_total") or (float(qty) * float(price) * (1 - float(disc) / 100))
        desc = li.get("description") or ""
        item_name = li.get("name") or li.get("item_name") or li.get("sku") or ""
        sku = li.get("sku") or ""
        rows.append(Tr(
            Td(sku, cls="mono"),
            Td(Div(Strong(item_name), P(desc, style="font-size:8pt;color:#555;") if desc and desc != item_name else None)),
            Td(str(qty), cls="r"),
            Td(_money(price), cls="r"),
            *([] if not has_disc else [Td(f"{disc}%" if disc else "", cls="r")]),
            Td(_money(line_total), cls="r"),
        ))

    headers = Tr(
        Th("SKU"), Th("Description"), Th("Qty", cls="r"), Th("Unit Price", cls="r"),
        *([] if not has_disc else [Th("Disc%", cls="r")]),
        Th("Amount", cls="r"),
    )

    subtotal = doc.get("subtotal") or 0
    tax_total = doc.get("tax_total") or 0
    grand_total = doc.get("grand_total") or doc.get("total") or 0
    notes_text = doc.get("notes") or doc.get("terms") or ""

    totals_rows = [Tr(Td("Subtotal", cls="label"), Td(_money(subtotal), cls="amount"))]
    if float(tax_total or 0):
        totals_rows.append(Tr(Td("Tax", cls="label"), Td(_money(tax_total), cls="amount")))
    totals_rows.append(Tr(Td("Total", cls="label"), Td(_money(grand_total), cls="amount"), cls="grand"))

    return Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(f"{title} {doc_number}"),
            Style(_DOC_PRINT_CSS),
        ),
        Body(
            Div(
                Div(
                    P(company_name, cls="dp-company-name"),
                    Div(
                        P(company_address) if company_address else None,
                        P(f"Tax ID: {company_tax_id}") if company_tax_id else None,
                        P(company_email) if company_email else None,
                        P(company_phone) if company_phone else None,
                        cls="dp-company-sub",
                    ),
                ),
                Div(
                    P(title, cls="dp-doc-title"),
                    Div(
                        P(Strong("No.: "), doc_number),
                        P(Strong("Date: "), issue_date) if issue_date else None,
                        P(Strong("Due: "), due_date) if due_date else None,
                        cls="dp-doc-meta",
                    ),
                ),
                cls="dp-header",
            ),
            Div(
                Div(
                    P("Bill To", cls="dp-party-label"),
                    P(contact_name, cls="dp-party-name") if contact_name else None,
                    Div(
                        P(contact_company) if contact_company and contact_company != contact_name else None,
                        P(contact_address) if contact_address else None,
                        P(f"Tax ID: {contact_tax_id}") if contact_tax_id else None,
                        P(contact_email) if contact_email else None,
                        cls="dp-party-sub",
                    ),
                ) if contact_name else None,
                cls="dp-parties",
            ),
            Table(Thead(headers), Tbody(*rows), cls="dp-lines") if rows else P("No line items.", style="font-size:9pt;color:#888;margin-bottom:4mm;"),
            Div(Table(*totals_rows), cls="dp-totals"),
            Div(P("Notes", cls="dp-notes-label"), P(notes_text), cls="dp-notes") if notes_text else None,
            Div(NotStr(f'Powered by <a href="https://celerp.com" style="color:#aaa;text-decoration:none;">celerp.com</a>  ·  {doc_number}'), cls="dp-footer"),
            Script("window.onload = function() { window.print(); }"),
        ),
    )



async def _doc_notes_section_response(token: str, entity_id: str, is_list: bool):
    """Fetch notes and return the rendered notes section (innerHTML target)."""
    from starlette.responses import Response as _Res
    tz = "UTC"
    try:
        _co = await api.get_company(token)
        tz = _co.get("timezone") or "UTC"
    except Exception:
        pass
    try:
        notes = await api.list_list_notes(token, entity_id) if is_list else await api.list_doc_notes(token, entity_id)
    except Exception:
        notes = []
    _base = f"/lists/{entity_id}" if is_list else f"/docs/{entity_id}"
    return _shared_notes_tab(
        entity_id=entity_id,
        notes=notes,
        add_url=f"{_base}/notes",
        edit_url=f"{_base}/notes/{{note_id}}/edit",
        delete_url=f"{_base}/notes/{{note_id}}",
        refresh_target=f"#notes-section-{_safe_id(entity_id)}",
        note_field="note",
        author_field="author_name",
        tz=tz,
    )


def setup_routes(app):

    @app.get("/docs")
    async def docs_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "") or request.query_params.get("doc_type", "")
        status = request.query_params.get("status", "")
        status_in = request.query_params.get("status_in", "")
        contact_id = request.query_params.get("contact_id", "")
        overdue_only = request.query_params.get("overdue_only", "") in ("1", "true")
        unfulfilled_only = request.query_params.get("unfulfilled_only", "") in ("1", "true")
        not_restocked = request.query_params.get("not_restocked", "") in ("1", "true")
        not_stocked = request.query_params.get("not_stocked", "") in ("1", "true")
        all_issued = request.query_params.get("all_issued", "") in ("1", "true")
        converted_to_type = request.query_params.get("converted_to_type", "")
        view = request.query_params.get("view", "")  # "drafts" = drafts-only mode
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "date")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
        except (ValueError, TypeError):
            per_page = _PER_PAGE

        # Date filter: use explicit URL param if set, otherwise fall back to
        # the per-company saved preference (default: last_12m).
        _has_explicit_date = (
            request.query_params.get("preset")
            or request.query_params.get("from")
            or request.query_params.get("to")
        )
        try:
            company = await api.get_company(token)
        except Exception:
            company = {}
        currency = company.get("currency") or None
        if _has_explicit_date:
            date_from, date_to, preset = _parse_dates(request)
        else:
            _default_preset = company.get("docs_default_preset") or "last_12m"
            if _default_preset == "all":
                date_from, date_to, preset = "", "", "all"
            else:
                date_from, date_to = _resolve_preset(_default_preset)
                preset = _default_preset

        # Drafts are segregated: only shown when ?view=drafts or explicit ?status=draft.
        # All other views exclude drafts by default (like email treats Drafts).
        is_drafts_view = view == "drafts" or status == "draft"
        effective_status = status
        if is_drafts_view:
            effective_status = "draft"
        elif not status and not status_in:
            effective_status = "exclude_draft"  # backend must support this param

        try:
            params = {"limit": per_page, "offset": (page - 1) * per_page}
            if q:
                params["q"] = q
            if contact_id:
                params["contact_id"] = contact_id
            if doc_type:
                params["doc_type"] = doc_type
            if is_drafts_view:
                params["status"] = "draft"
            elif status_in:
                params["status_in"] = status_in
                if overdue_only:
                    params["overdue_only"] = "1"
            elif all_issued:
                params["all_issued"] = "1"
                if overdue_only:
                    params["overdue_only"] = "1"
            elif effective_status == "exclude_draft":
                params["exclude_status"] = "draft"
            elif effective_status:
                params["status"] = effective_status
            if overdue_only and not status_in and not all_issued:
                params["overdue_only"] = "1"
            if unfulfilled_only:
                params["unfulfilled_only"] = "1"
            if not_restocked:
                params["not_restocked"] = "1"
            if not_stocked:
                params["not_stocked"] = "1"
            if converted_to_type:
                params["converted_to_type"] = converted_to_type
            if date_from and not is_drafts_view:
                params["date_from"] = date_from
            if date_to and not is_drafts_view:
                params["date_to"] = date_to
            docs_resp = None
            if is_drafts_view and doc_type == "purchase_order":
                # PO drafts view: fetch both purchase_order and bill drafts combined.
                import asyncio as _asyncio
                bill_params = {**params, "doc_type": "bill"}
                po_summary_params = {"doc_type": doc_type}
                bill_summary_params = {"doc_type": "bill"}
                po_resp, bill_resp, po_summary, bill_summary = await _asyncio.gather(
                    api.list_docs(token, params),
                    api.list_docs(token, bill_params),
                    api.get_doc_summary(token, doc_type=doc_type),
                    api.get_doc_summary(token, doc_type="bill"),
                )
                po_items = po_resp.get("items", []) if isinstance(po_resp, dict) else po_resp
                bill_items = bill_resp.get("items", []) if isinstance(bill_resp, dict) else bill_resp
                docs = po_items + bill_items
                draft_count = (
                    (po_summary.get("draft_count", 0) if isinstance(po_summary, dict) else 0)
                    + (bill_summary.get("draft_count", 0) if isinstance(bill_summary, dict) else 0)
                )
                summary = po_summary  # use PO summary for cards
            else:
                import asyncio as _asyncio
                docs_resp, summary = await _asyncio.gather(
                    api.list_docs(token, params),
                    api.get_doc_summary(token, doc_type=doc_type),
                )
                docs = docs_resp.get("items", []) if isinstance(docs_resp, dict) else docs_resp
                # Draft count comes from summary - no extra round-trip needed.
                draft_count = summary.get("draft_count", 0) if isinstance(summary, dict) else 0
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            docs, summary, draft_count = [], {}, 0

        extra = f"&q={q}&type={doc_type}&status={status}&view={view}".strip("&")
        total_count = summary.get("total_count", len(docs))
        lang = get_lang(request)
        page_title = _DOC_TYPE_PAGE_LABELS.get(doc_type, "Documents")
        new_label = _doc_type_new_label(doc_type, lang)
        search_url = f"/docs/search?type={doc_type}" if doc_type else "/docs/search"
        create_type = doc_type or "invoice"
        return base_shell(
            page_header(
                page_title,
                _drafts_tab(draft_count, is_drafts_view, doc_type, status=status, lang=lang),
                search_bar(
                    placeholder="Search doc number, contact...",
                    target="#doc-table",
                    url=search_url,
                ),
                Button(
                    new_label,
                    hx_post=f"/docs/create-blank?type={create_type}",
                    hx_swap="none",
                    cls="btn btn--primary",
                ),
                A(t("btn.export_csv"), href="/docs/export/csv", cls="btn btn--secondary"),
                A(t("doc.import_csv"), href="/docs/import", cls="btn btn--secondary"),
            ),
            *([] if is_drafts_view else [
                _date_filter_bar("/docs", date_from, date_to, preset, extra_params=f"&{extra}" if extra else "", lang=lang),
            ]),
            _summary_bar(summary, doc_type, currency, lang),
            _doc_status_cards(docs, status, summary, currency, doc_type=doc_type, lang=lang, status_in=status_in, overdue_only=overdue_only, unfulfilled_only=unfulfilled_only, not_restocked=not_restocked, not_stocked=not_stocked, all_issued=all_issued, converted_to_type=converted_to_type),
            _doc_table(
                docs,
                sort=sort,
                sort_dir=sort_dir,
                base_params={"q": q, "type": doc_type, "status": status, "contact_id": contact_id, "view": view, "page": str(page), "per_page": str(per_page)},
                doc_type=doc_type if not is_drafts_view else doc_type,
                lang=lang,
            ),
            pagination(page, total_count, per_page, "/docs", f"q={q}&type={doc_type}&status={status}&view={view}&sort={sort}&dir={sort_dir}".strip("&")),
            title=f"{page_title} - Celerp",
            nav_active=_doc_nav_key(doc_type),
            lang=lang,
            request=request,
        )

    @app.get("/docs/search")
    async def docs_search(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "") or request.query_params.get("doc_type", "")
        status = request.query_params.get("status", "")
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "date")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            params = {"limit": _PER_PAGE, "offset": (page - 1) * _PER_PAGE}
            if q:
                params["q"] = q
            if doc_type:
                params["doc_type"] = doc_type
            if status:
                params["status"] = status
            docs = (await api.list_docs(token, params)).get("items", [])
        except APIError as e:
            docs = []
        return _doc_table(
            docs,
            sort=sort,
            sort_dir=sort_dir,
            base_params={"q": q, "type": doc_type, "status": status, "page": str(page)},
            doc_type=doc_type,
            lang=get_lang(request),
        )

    @app.get("/docs/export/csv")
    async def docs_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        params: dict = {}
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "") or request.query_params.get("doc_type", "")
        status = request.query_params.get("status", "")
        if q:
            params["q"] = q
        if doc_type:
            params["doc_type"] = doc_type
        if status:
            params["status"] = status
        try:
            data = await api.export_docs_csv(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = b"error\n"
        from starlette.responses import Response
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=documents.csv"},
        )

    @app.post("/docs/create-blank")
    async def create_blank_doc(request: Request):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        doc_type = request.query_params.get("type", "invoice")
        try:
            result = await api.create_doc(token, {"doc_type": doc_type, "status": "draft"})
            entity_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            if e.status == 401:
                from starlette.responses import Response as _R
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            import json as _json
            from starlette.responses import Response as _R
            detail = e.detail if hasattr(e, "detail") and e.detail else "Failed to create document."
            return _R(
                "",
                status_code=200,
                headers={"HX-Trigger": _json.dumps({"flashError": detail})},
            )
        # Apply default T&C template in the background (non-blocking).
        # The user is already redirected; T&C is a nice-to-have not a blocker.
        import asyncio as _asyncio

        async def _apply_tc():
            try:
                tc_templates = await api.get_terms_conditions(token)
                default_tc = next(
                    (tc for tc in tc_templates
                     if doc_type in (tc.get("default_for") or [])),
                    None,
                )
                if default_tc and entity_id:
                    await api.patch_doc(token, entity_id, {
                        "terms_template": default_tc["name"],
                        "terms_text": default_tc.get("text", ""),
                    })
            except Exception:
                pass

        _asyncio.create_task(_apply_tc())
        from starlette.responses import Response as _R
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/from-items")
    async def doc_from_items_modal(request: Request):
        """Modal: choose to create new draft invoice or add to existing."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        # Fetch recent draft invoices for the picker
        try:
            drafts_resp = await api.list_docs(token, {"status": "draft", "doc_type": "invoice", "limit": 20})
            drafts = drafts_resp.get("items", [])
        except APIError:
            drafts = []
        hidden_items = [Input(type="hidden", name="selected", value=eid) for eid in entity_ids]
        return _send_to_modal("Invoice", "/docs/from-items/new", "/docs/from-items/add",
                              "/docs/from-items/search", drafts, hidden_items, "doc")

    @app.post("/docs/from-items/new")
    async def create_doc_from_items(request: Request):
        """Create a draft invoice pre-populated with line items from selected inventory items."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="modal-container")
        line_items = await _line_items_from_inventory(token, entity_ids)
        doc_taxes = await _company_doc_taxes(token)
        try:
            result = await api.create_doc(token, {
                "doc_type": "invoice",
                "status": "draft",
                "line_items": line_items,
                "doc_taxes": doc_taxes,
            })
            doc_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{doc_id}"})

    @app.post("/docs/from-items/add")
    async def add_items_to_doc(request: Request):
        """Append line items from selected inventory to an existing document."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_id = str(form.get("target_id", "")).strip()
        if not entity_ids or not target_id:
            return Div(P(t("label.no_items_or_target_selected"), cls="flash flash--warning"), id="modal-container")
        new_lines = await _line_items_from_inventory(token, entity_ids)
        try:
            doc = await api.get_doc(token, target_id)
            existing_lines = doc.get("line_items") or []
            combined = existing_lines + new_lines
            subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
            await api.patch_doc(token, target_id, {
                "line_items": combined,
                "subtotal": subtotal,
                "total": subtotal,
            })
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})

    @app.get("/docs/from-items/search")
    async def doc_from_items_search(request: Request):
        """HTMX search endpoint for the doc picker dropdown."""
        token = _token(request)
        if not token:
            return Div()
        q = request.query_params.get("q", "").strip()
        try:
            resp = await api.list_docs(token, {"doc_type": "invoice", "q": q, "limit": 20} if q else {"status": "draft", "doc_type": "invoice", "limit": 20})
            docs = resp.get("items", [])
        except APIError:
            docs = []
        return _send_to_option_list(docs, "doc")
    @app.get("/docs/catalog-lookup")
    async def doc_catalog_lookup(request: Request):
        """Lookup item by SKU or barcode. Returns {sku, description, unit_price} or {}."""
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        code = request.query_params.get("sku", "").strip()
        if not code:
            return JSONResponse({})
        price_list = request.query_params.get("price_list", "Retail").strip() or "Retail"
        is_credit_note = request.query_params.get("doc_type", "").strip() == "credit_note"

        def _extract(item: dict) -> dict:
            return {
                "sku": item.get("sku") or "",
                "description": item.get("name") or item.get("description") or "",
                "unit_price": resolve_price(item, price_list),
                "sell_by": item.get("sell_by") or None,
                "quantity": item.get("quantity") or 0,
                "hs_code": item.get("hs_code") or None,
                "entity_id": item.get("entity_id") or item.get("id") or None,
                "allow_splitting": bool(item.get("allow_splitting")),
                "category": item.get("category") or None,
                "cost_price": item.get("cost_price") or None,
                "wholesale_price": item.get("wholesale_price") or None,
                "barcode": item.get("barcode") or None,
            }

        try:
            if is_credit_note:
                # Credit notes: sold items first (returns), then active fallback
                for params in ({"sku": code, "limit": 1, "status": "sold"},
                               {"barcode": code, "limit": 1, "status": "sold"},
                               {"sku": code, "limit": 1},
                               {"barcode": code, "limit": 1}):
                    resp = await api.list_items(token, params)
                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                    if items:
                        return JSONResponse(_extract(items[0]))
            else:
                for params in ({"sku": code, "limit": 1},
                               {"barcode": code, "limit": 1},
                               {"q": code, "limit": 1}):
                    resp = await api.list_items(token, params)
                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                    if items:
                        return JSONResponse(_extract(items[0]))
        except Exception:
            pass
        return JSONResponse({})

    @app.get("/docs/catalog-search")
    async def doc_catalog_search(request: Request):
        """Search inventory items by SKU or name. Returns [{sku, description, unit_price, sell_by}]."""
        from starlette.responses import JSONResponse as _J
        token = _token(request)
        if not token:
            return _J({"error": "unauthorized"}, status_code=401)
        q = request.query_params.get("q", "").strip()
        price_list = request.query_params.get("price_list", "Retail").strip() or "Retail"
        is_credit_note = request.query_params.get("doc_type", "").strip() == "credit_note"
        if not q:
            return _J([])

        def _extract(item: dict) -> dict:
            return {
                "sku": item.get("sku") or "",
                "description": item.get("name") or item.get("description") or "",
                "unit_price": resolve_price(item, price_list),
                "sell_by": item.get("sell_by") or None,
                "hs_code": item.get("hs_code") or None,
                "quantity": item.get("quantity") or 0,
                "entity_id": item.get("entity_id") or item.get("id") or None,
                "allow_splitting": bool(item.get("allow_splitting")),
                "category": item.get("category") or None,
                "cost_price": item.get("cost_price") or None,
                "wholesale_price": item.get("wholesale_price") or None,
                "barcode": item.get("barcode") or None,
            }

        try:
            if is_credit_note:
                # Credit notes: search sold items first, then active, merge (sold first)
                resp_sold = await api.list_items(token, {"q": q, "limit": 10, "status": "sold"})
                sold = resp_sold.get("items", []) if isinstance(resp_sold, dict) else resp_sold
                resp_active = await api.list_items(token, {"q": q, "limit": 10})
                active = resp_active.get("items", []) if isinstance(resp_active, dict) else resp_active
                seen = set()
                items = []
                for item in sold + active:
                    key = item.get("sku") or item.get("entity_id")
                    if key and key not in seen:
                        seen.add(key)
                        items.append(item)
                return _J([_extract(i) for i in items[:10]])
            else:
                resp = await api.list_items(token, {"q": q, "limit": 10})
                items = resp.get("items", []) if isinstance(resp, dict) else resp
                return _J([_extract(i) for i in items])
        except Exception:
            return _J([])

    # ── Line item CSV export/import ─────────────────────────────────

    _CSV_COLUMNS = ["sku", "description", "quantity", "unit", "unit_price", "discount_pct", "tax_code", "tax_rate", "hs_code", "account_code"]
    _CSV_ALIASES: dict[str, str] = {
        "item": "sku", "item_code": "sku", "code": "sku", "product": "sku", "barcode": "sku",
        "name": "description", "desc": "description", "item_name": "description", "product_name": "description",
        "qty": "quantity", "amount": "quantity",
        "price": "unit_price", "rate": "unit_price", "unit price": "unit_price",
        "discount": "discount_pct", "disc": "discount_pct", "disc_pct": "discount_pct", "discount_percent": "discount_pct",
        "tax": "tax_rate", "tax_pct": "tax_rate", "vat": "tax_rate", "vat_rate": "tax_rate",
        "tax_name": "tax_code", "tax code": "tax_code",
        "hs": "hs_code", "hs code": "hs_code", "tariff": "hs_code",
        "account": "account_code", "gl_code": "account_code",
    }

    def _map_csv_header(header: str) -> str | None:
        """Map a CSV header to a canonical column name, or None if unmapped."""
        h = header.strip().lower().replace("-", "_").replace(" ", "_")
        if h in _CSV_COLUMNS:
            return h
        return _CSV_ALIASES.get(h)

    async def _export_line_items_csv(request: Request, entity_id: str):
        """Shared: export a doc/list's line items as CSV."""
        from starlette.responses import Response as _Resp
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return _Resp(content=f"Error: {e.detail}", status_code=e.status)
        line_items = doc.get("line_items") or []
        doc_ref = (doc.get("ref_id") or doc.get("doc_number") or entity_id).replace(" ", "_")

        import io as _io, csv as _csv
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        for li in line_items:
            writer.writerow([
                li.get("sku") or "",
                li.get("description") or li.get("name") or "",
                li.get("quantity", 0),
                li.get("unit") or "",
                li.get("unit_price", 0),
                li.get("discount_pct") or 0,
                li.get("tax_code") or "",
                li.get("tax_rate") or 0,
                li.get("hs_code") or "",
                li.get("account_code") or "",
            ])
        return _Resp(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{doc_ref}_items.csv"'},
        )

    @app.get("/docs/{entity_id}/items/csv")
    async def doc_items_export_csv(request: Request, entity_id: str):
        """Export a document's line items as CSV."""
        return await _export_line_items_csv(request, entity_id)

    @app.post("/docs/{entity_id}/items/csv")
    async def doc_items_import_csv(request: Request, entity_id: str):
        """Import line items from CSV and append to document."""
        from starlette.responses import JSONResponse as _J
        token = _token(request)
        if not token:
            return _J({"error": "unauthorized"}, status_code=401)
        form = await request.form()
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            return _J({"error": t("doc.csv_no_file")}, status_code=400)

        import io as _io, csv as _csv
        try:
            raw = (await upload.read()).decode("utf-8-sig")
        except Exception:
            return _J({"error": t("doc.csv_decode_error")}, status_code=400)
        reader = _csv.DictReader(_io.StringIO(raw))
        if not reader.fieldnames:
            return _J({"error": t("doc.csv_no_headers")}, status_code=400)

        # Map CSV headers to canonical names
        col_map: dict[str, str] = {}
        for h in reader.fieldnames:
            mapped = _map_csv_header(h)
            if mapped:
                col_map[h] = mapped

        if "sku" not in col_map.values() and "description" not in col_map.values():
            return _J({"error": t("doc.csv_missing_sku_or_description")}, status_code=400)

        # Parse rows
        new_lines: list[dict] = []
        price_list = form.get("price_list") or "Retail"
        for row in reader:
            mapped_row: dict[str, str] = {}
            for csv_col, canon in col_map.items():
                mapped_row[canon] = row.get(csv_col, "").strip()
            # Skip empty rows
            if not mapped_row.get("sku") and not mapped_row.get("description"):
                continue

            sku = mapped_row.get("sku", "")
            desc = mapped_row.get("description", "")
            qty_str = mapped_row.get("quantity", "1")
            price_str = mapped_row.get("unit_price", "")
            disc_str = mapped_row.get("discount_pct", "0")
            unit = mapped_row.get("unit", "")
            tax_code = mapped_row.get("tax_code", "")
            tax_rate_str = mapped_row.get("tax_rate", "0")
            hs_code = mapped_row.get("hs_code", "")
            account_code = mapped_row.get("account_code", "")

            try:
                qty = float(qty_str) if qty_str else 1
            except ValueError:
                qty = 1
            try:
                unit_price = float(price_str) if price_str else None
            except ValueError:
                unit_price = None
            try:
                discount_pct = float(disc_str) if disc_str else 0
            except ValueError:
                discount_pct = 0
            try:
                tax_rate = float(tax_rate_str) if tax_rate_str else 0
            except ValueError:
                tax_rate = 0

            # Resolve SKU against inventory catalog
            catalog_item: dict = {}
            if sku:
                try:
                    resp = await api.list_items(token, {"sku": sku, "limit": 1})
                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                    if items:
                        catalog_item = items[0]
                    else:
                        resp = await api.list_items(token, {"barcode": sku, "limit": 1})
                        items = resp.get("items", []) if isinstance(resp, dict) else resp
                        if items:
                            catalog_item = items[0]
                except Exception:
                    pass

            line = {
                "sku": sku or (catalog_item.get("sku") or ""),
                "description": desc or catalog_item.get("name") or "",
                "quantity": qty,
                "unit": unit or catalog_item.get("sell_by") or "",
                "unit_price": unit_price if unit_price is not None else resolve_price(catalog_item, price_list) if catalog_item else 0,
                "discount_pct": discount_pct,
                "tax_code": tax_code,
                "tax_rate": tax_rate,
                "hs_code": hs_code or catalog_item.get("hs_code") or "",
                "account_code": account_code,
                "entity_id": catalog_item.get("entity_id") or "",
                "allow_splitting": bool(catalog_item.get("allow_splitting")),
            }
            new_lines.append(line)

        if not new_lines:
            return _J({"error": t("doc.csv_no_valid_rows")}, status_code=400)

        # Append to existing line items
        try:
            doc = await api.get_doc(token, entity_id)
            existing = doc.get("line_items") or []
            combined = existing + new_lines
            subtotal = sum(
                float(l.get("quantity", 0)) * float(l.get("unit_price", 0)) * (1 - float(l.get("discount_pct", 0)) / 100)
                for l in combined
            )
            await api.patch_doc(token, entity_id, {
                "line_items": combined,
                "subtotal": subtotal,
                "total": subtotal,
            })
        except APIError as e:
            return _J({"error": str(e.detail)}, status_code=e.status)

        return _J({"ok": True, "imported": len(new_lines)})

    # Same export for lists
    @app.get("/lists/{entity_id}/print")
    async def list_print_view(request: Request, entity_id: str):
        """Standalone printable HTML for a list - auto-triggers window.print()."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            from starlette.responses import HTMLResponse as _HR
            return _HR(f"<p>Error: {e.detail}</p>", status_code=e.status)
        lst.setdefault("doc_type", "list")
        if not lst.get("contact_name"):
            lst["contact_name"] = lst.get("receiver") or lst.get("customer_name") or ""
        if not lst.get("issue_date"):
            lst["issue_date"] = lst.get("created_at") or lst.get("date")
        if not lst.get("company_name"):
            try:
                company = await api.get_company(token)
                lst.update({
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or "",
                    "company_phone": company.get("phone") or "",
                    "company_tax_id": company.get("tax_id") or "",
                    "company_email": company.get("email") or "",
                })
            except Exception:
                pass
        from starlette.responses import HTMLResponse as _HR
        from fasthtml.common import to_xml
        return _HR(to_xml(_doc_print_view(lst)))

    @app.get("/lists/{entity_id}/items/csv")
    async def list_items_export_csv(request: Request, entity_id: str):
        """Export a list's line items as CSV - delegates to shared handler."""
        return await _export_line_items_csv(request, entity_id)

    @app.post("/lists/{entity_id}/items/csv")
    async def list_items_import_csv(request: Request, entity_id: str):
        """Import line items from CSV and append to list - delegates to doc handler."""
        return await doc_items_import_csv(request, entity_id)

    @app.get("/docs/{entity_id}/print")
    async def doc_print_view(request: Request, entity_id: str):
        """Standalone printable HTML for a document - auto-triggers window.print()."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            from starlette.responses import HTMLResponse as _HR
            return _HR(f"<p>Error loading document: {e.detail}</p>", status_code=e.status)
        # Inject company fields
        if not doc.get("company_name"):
            try:
                company = await api.get_company(token)
                doc = {**doc,
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or "",
                    "company_phone": company.get("phone") or "",
                    "company_tax_id": company.get("tax_id") or "",
                    "company_email": company.get("email") or "",
                }
            except Exception:
                pass
        # Resolve contact
        cid = doc.get("contact_id")
        if cid and not doc.get("contact_name"):
            try:
                contact = await api.get_contact(token, cid)
                doc["contact_name"] = contact.get("name") or ""
                doc["contact_company_name"] = contact.get("company_name") or ""
                doc["contact_email"] = contact.get("email") or ""
                doc["contact_billing_address"] = contact.get("billing_address") or contact.get("address") or ""
                doc["contact_tax_id"] = contact.get("tax_id") or ""
            except Exception:
                pass
        from starlette.responses import HTMLResponse as _HR
        from fasthtml.common import to_xml
        return _HR(to_xml(_doc_print_view(doc)))

    @app.get("/docs/{entity_id}/pdf")
    async def doc_pdf_redirect(request: Request, entity_id: str):
        """Legacy PDF route - redirect to new print view."""
        return RedirectResponse(f"/docs/{entity_id}/print", status_code=302)

    @app.get("/docs/{entity_id}")
    async def doc_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            doc = await api.get_doc(token, entity_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if isinstance(e, APIError) and e.status == 404:
                from starlette.responses import HTMLResponse as _HR
                return _HR("<h2>Document not found</h2><p><a href='/docs'>Back to Documents</a></p>", status_code=404)
            doc = {}

        # Inject company fields so "My company info" box is populated
        if not doc.get("company_name"):
            try:
                company = await api.get_company(token)
                doc = {
                    **doc,
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or company.get("settings", {}).get("address") or "",
                    "company_phone": company.get("phone") or company.get("settings", {}).get("phone") or "",
                    "company_tax_id": company.get("tax_id") or company.get("settings", {}).get("tax_id") or "",
                    "company_email": company.get("email") or company.get("settings", {}).get("email") or "",
                }
            except Exception:
                pass

        # Resolve contact details if contact_id set but name missing
        cid = doc.get("contact_id")
        _resolved_contact: dict | None = None
        if cid and not doc.get("contact_name"):
            try:
                _resolved_contact = await api.get_contact(token, cid)
                doc["contact_name"] = _resolved_contact.get("name") or ""
                doc["contact_company_name"] = _resolved_contact.get("company_name") or ""
                doc["contact_email"] = _resolved_contact.get("email") or ""
                doc["contact_phone"] = _resolved_contact.get("phone") or ""
                doc["contact_tax_id"] = _resolved_contact.get("tax_id") or ""
            except Exception:
                pass

        # Resolve default billing/shipping address from contact if not yet stored on doc
        if cid and (not doc.get("contact_billing_address") or not doc.get("contact_shipping_address")):
            try:
                contact = _resolved_contact or await api.get_contact(token, cid)
                addresses = contact.get("addresses") or []
                def _resolve_addr(addr_type: str) -> str:
                    default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
                    if default:
                        return default.get("full_address") or default.get("address") or default.get("label") or ""
                    first = next((a for a in addresses if a.get("address_type") == addr_type), None)
                    if first:
                        return first.get("full_address") or first.get("address") or first.get("label") or ""
                    return contact.get(f"{addr_type}_address") or ""
                if not doc.get("contact_billing_address"):
                    doc["contact_billing_address"] = doc.get("contact_address") or _resolve_addr("billing")
                if not doc.get("contact_shipping_address"):
                    doc["contact_shipping_address"] = _resolve_addr("shipping")
                # Also resolve shipping attn from contact address
                if not doc.get("shipping_attn"):
                    default_ship = next((a for a in addresses if a.get("address_type") == "shipping" and a.get("is_default")), None)
                    first_ship = default_ship or next((a for a in addresses if a.get("address_type") == "shipping"), None)
                    if first_ship and first_ship.get("attn"):
                        doc["shipping_attn"] = first_ship["attn"]
            except Exception:
                pass
        # Backward compat: migrate contact_address → contact_billing_address
        if not doc.get("contact_billing_address") and doc.get("contact_address"):
            doc["contact_billing_address"] = doc["contact_address"]

        # Fetch locations for receive-goods dropdown (PO + consignment_in) + company address picker
        locations: list[dict] = []
        doc_type = doc.get("doc_type", "")
        company_locations: list[dict] = []
        try:
            loc_resp = await api.get_locations(token)
            _all_locs = loc_resp.get("items") or loc_resp.get("locations") or (loc_resp if isinstance(loc_resp, list) else [])
            if not isinstance(_all_locs, list):
                _all_locs = []
            locations = _all_locs if doc_type in ("purchase_order", "consignment_in", "bill") else []
            company_locations = _all_locs
        except Exception:
            locations = []

        item_categories: list[str] = []
        if doc_type in ("purchase_order", "bill", "consignment_in"):
            try:
                item_categories = await api.list_item_categories(token)
            except Exception:
                item_categories = []
            company_locations = []

        # Fetch document history (ledger entries)
        ledger: list[dict] = []
        try:
            ledger_resp = await api.list_ledger(token, {"entity_id": entity_id, "limit": 50})
            ledger = ledger_resp.get("items", []) if isinstance(ledger_resp, dict) else []
        except Exception:
            ledger = []

        doc_ref = doc.get("ref_id") or doc.get("doc_number") or doc.get("ref") or doc.get("external_id") or "Document"
        status = doc.get("status", "draft")

        # Fetch price lists for price list dropdown on doc detail
        price_lists: list[dict] = []
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            pass
        # Fetch T&C templates for dropdown on doc detail
        tc_templates: list[dict] = []
        try:
            tc_templates = await api.get_terms_conditions(token)
        except Exception:
            pass
        # Fetch company timezone for notes display
        tz: str = "UTC"
        company_currency: str = "USD"
        try:
            _co = await api.get_company(token)
            tz = _co.get("timezone") or "UTC"
            company_currency = _co.get("currency") or "USD"
        except Exception:
            pass
        company_taxes: list[dict] = []
        try:
            company_taxes = await api.get_taxes(token)
        except Exception:
            pass
        # Fetch bank accounts for payment section
        bank_accounts: list[dict] = []
        if doc_type in ("invoice", "bill", "credit_note"):
            try:
                ba_resp = await api.get_bank_accounts(token)
                bank_accounts = ba_resp.get("items", []) if isinstance(ba_resp, dict) else ba_resp
                if not isinstance(bank_accounts, list):
                    bank_accounts = []
            except Exception:
                pass
        # Fetch notes for doc (first-class entities)
        doc_notes: list[dict] = []
        try:
            doc_notes = await api.list_doc_notes(token, entity_id)
        except Exception:
            pass
        # Check relay connection for Send button visibility
        _relay_connected: bool = False
        try:
            _relay_status = await api.get_relay_status(token)
            _relay_connected = bool(_relay_status.get("connected"))
        except Exception:
            pass
        status_label = "Pro Forma" if doc_type == "invoice" and status == "draft" else status.replace("_", " ").title()
        type_label = _doc_singular_label(doc_type)
        section_label = _doc_section_label(doc_type)
        section_url = _doc_section_url(doc_type)
        back_url = _doc_section_url(doc_type)
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), (section_label, section_url), (f"{status_label} {doc_ref}", None)]),
            page_header(f"{type_label} - {status_label} {doc_ref}"),
            _doc_detail(doc, locations=locations, ledger=ledger, price_lists=price_lists, tc_templates=tc_templates, tz=tz, company_taxes=company_taxes, bank_accounts=bank_accounts, company_locations=company_locations, role=_get_role(request), item_categories=item_categories, notes=doc_notes, company_currency=company_currency, relay_connected=_relay_connected),
            title=f"{type_label} {doc_ref} - Celerp",
            nav_active=_doc_nav_key(doc_type),
            request=request,
        )

    @app.get("/docs/{entity_id}/field/{field}/display")
    async def doc_field_display(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        # Resolve contact fields to display names
        if field in ("contact_id", "commission_contact_id"):
            display_value = _resolve_contact_display(doc, field)
        elif field == "contact_company_name":
            display_value = doc.get("contact_company_name")
        else:
            display_value = doc.get(field)
        return _doc_display_cell(entity_id, field, display_value)

    @app.get("/docs/{entity_id}/field/{field}/edit")
    async def doc_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        value = str(doc.get(field, "") or "")

        restore_url = f"/docs/{entity_id}/field/{field}/display"
        esc_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        enter_js = "if(event.key==='Enter'){event.preventDefault();this.blur();}"
        blur_restore = f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}})"
        combobox_esc_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        if field == "status":
            # Status is a state-machine field; transitions happen via lifecycle buttons only.
            # Return a non-editable display to block direct manipulation via URL.
            return _doc_display_cell(entity_id, "status", value)
        elif field == "currency":
            # Currency is immutable after creation - show read-only display
            return _doc_display_cell(entity_id, "currency", value)
        elif field == "conversion_rate":
            # Editable on draft only; locked after finalization
            doc_status = doc.get("status", "draft")
            if doc_status != "draft":
                return _doc_display_cell(entity_id, "conversion_rate", value)
            input_el = Input(
                type="number", name="value", value=value, step="any", min="0",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
                onblur=f"if(!this.value.trim() && !this.dataset.dirty){{{blur_restore}}}",
                oninput="this.dataset.dirty='1'",
                data_orig=value,
                placeholder="e.g. 35.00",
            )
        elif field == "purchase_kind":
            opts = ["inventory", "expense", "asset"]
            input_el = Select(
                *[Option(o, value=o, selected=(o == value)) for o in opts],
                name="value",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change", cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js, onblur=blur_restore,
            )
        elif field in ("issue_date", "due_date", "valid_until"):
            display_value = value[:10] if value else ""
            if not display_value and field == "issue_date":
                from datetime import date
                display_value = date.today().isoformat()
            # Constrain pickers to prevent inverted issue/due date.
            # issue_date max = due_date (if set); due_date min = issue_date (if set).
            date_min = ""
            date_max = ""
            if field == "due_date":
                date_min = (doc.get("issue_date") or "")[:10]
            elif field == "issue_date":
                date_max = (doc.get("due_date") or "")[:10]
            input_el = Input(
                type="date", name="value", value=display_value,
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
                onblur=f"if(!this.value.trim() && !this.dataset.dirty){{{blur_restore}}}",
                oninput="this.dataset.dirty='1'",
                data_orig=value,
                **({} if not date_min else {"min": date_min}),
                **({} if not date_max else {"max": date_max}),
            )
        elif field == "price_list":
            # Searchable dropdown of company price lists
            try:
                price_lists = await api.get_price_lists(token)
            except APIError:
                price_lists = []
            pl_names = [pl.get("name", "") for pl in price_lists]
            input_el = Select(
                Option(t("doc._default"), value=""),
                *[Option(name, value=name, selected=(name == value)) for name in pl_names],
                name="value",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js,
            )
        elif field in ("contact_id", "commission_contact_id", "contact_company_name"):
            # Searchable contact picker.
            # - commission_contact_id: always vendor-only
            # - contact_id: customer docs → customers; vendor docs → vendors
            # - contact_company_name: same filtering as contact_id but selects by company_name → resolves contact_id
            _VENDOR_TYPES = ("purchase_order", "bill", "consignment_in")
            doc_type_for_filter = doc.get("doc_type", "")
            if field == "commission_contact_id":
                contact_filter = "vendor"
            elif doc_type_for_filter in _VENDOR_TYPES:
                contact_filter = "vendor"
            else:
                contact_filter = "customer"
            try:
                contact_resp = await api.list_contacts(token, {"limit": 500, "contact_type": contact_filter})
                contacts = contact_resp.get("items", [])
            except APIError:
                contacts = []
            if field == "contact_company_name":
                # Options are (entity_id, company_name) so selecting a company resolves the contact
                contact_opts = [
                    (c.get("entity_id") or c.get("id") or "", c.get("company_name") or c.get("name") or "")
                    for c in contacts if c.get("company_name")
                ]
                # Current value is the company_name string; find current contact_id for pre-selection
                current_contact_id = doc.get("contact_id") or ""
                pre_val = current_contact_id
                patch_url = f"/docs/{entity_id}/field/contact_id"
            else:
                contact_opts = [(c.get("entity_id") or c.get("id") or "", c.get("name") or c.get("entity_id") or c.get("id") or "") for c in contacts]
                contact_opts.append(("__new__", "+ Add new contact"))
                pre_val = value
                patch_url = f"/docs/{entity_id}/field/{field}"
            # Fix #1: wrap combobox in div so ESC keydown bubbles up and can restore the display cell
            input_el = Div(
                searchable_select(
                    name="value",
                    options=contact_opts,
                    value=pre_val,
                    placeholder="Search contacts...",
                    hx_patch=patch_url,
                    hx_target="closest .editable-cell",
                    hx_swap="outerHTML",
                    hx_trigger="change",
                ),
                onkeydown=combobox_esc_js,
            )
        elif field in ("contact_billing_address", "contact_shipping_address"):
            # Address dropdown from contact's saved addresses
            addr_type = "billing" if field == "contact_billing_address" else "shipping"
            contact_id = doc.get("contact_id") or ""
            addr_opts: list[tuple[str, str]] = []
            if contact_id:
                try:
                    contact = await api.get_contact(token, contact_id)
                    addresses = contact.get("addresses") or []
                    typed = [a for a in addresses if a.get("address_type") == addr_type]
                    for a in typed:
                        label = a.get("label") or a.get("line1") or a.get("street") or str(a)
                        addr_str = a.get("full_address") or a.get("address") or label
                        addr_opts.append((addr_str, addr_str))
                    # Only use top-level field if no typed addresses found
                    if not typed:
                        top_addr = contact.get(f"{addr_type}_address") or ""
                        if top_addr:
                            addr_opts.append((top_addr, top_addr))
                except Exception:
                    pass
            if not addr_opts:
                # Fall back to plain text input if no addresses available
                input_el = Input(
                    type="text", name="value", value=value,
                    hx_patch=f"/docs/{entity_id}/field/{field}",
                    hx_target="closest .editable-cell", hx_swap="outerHTML",
                    hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                    onkeydown=esc_js + enter_js,
                    oninput="this.dataset.dirty='1'",
                )
            else:
                auto_select = len(addr_opts) == 1 and not value
                input_el = Select(
                    Option("-- Select address --", value="", selected=not value and not auto_select),
                    *[Option(lbl, value=val, selected=(val == value) or (auto_select and i == 0)) for i, (val, lbl) in enumerate(addr_opts)],
                    name="value",
                    hx_patch=f"/docs/{entity_id}/field/{field}",
                    hx_target="closest .editable-cell", hx_swap="outerHTML",
                    hx_trigger="change" + (", load" if auto_select else ""), cls="cell-input cell-input--select", autofocus=True,
                    onkeydown=esc_js, onblur=blur_restore,
                )
        else:
            input_el = Input(
                type="text", name="value", value=value,
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
                onblur=f"if(!this.value.trim() && !this.dataset.dirty){{{blur_restore}}}",
                oninput="this.dataset.dirty='1'",
                data_orig=value,
            )
        return Div(input_el, cls="editable-cell editable-cell--editing")

    @app.patch("/docs/{entity_id}/field/{field}")
    async def doc_field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        if value == "__new__":
            from starlette.responses import Response as _R
            # Route to vendors page for vendor doc types and commission contacts
            _VENDOR_TYPES = ("purchase_order", "bill", "consignment_in")
            try:
                doc = await api.get_doc(token, entity_id)
                is_vendor_context = field == "commission_contact_id" or doc.get("doc_type") in _VENDOR_TYPES
            except APIError:
                is_vendor_context = field == "commission_contact_id"
            target = "/contacts/vendors" if is_vendor_context else "/contacts/customers"
            return _R("", status_code=204, headers={"HX-Redirect": target})
        try:
            patch = {field: value}
            # Auto-populate payment_terms and price_list from contact when contact_id changes
            if field == "contact_id" and value:
                try:
                    contact = await api.get_contact(token, value)
                    # Store contact details for display on the doc
                    contact_name = contact.get("name") or contact.get("display_name")
                    if contact_name:
                        patch["contact_name"] = contact_name
                    patch["contact_company_name"] = contact.get("company_name") or ""
                    patch["contact_email"] = contact.get("email") or ""
                    patch["contact_phone"] = contact.get("phone") or ""
                    # Billing address: prefer default billing address from addresses list, fall back to billing_address field
                    addresses = contact.get("addresses") or []
                    def _default_addr(addr_type: str) -> str:
                        default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
                        if default:
                            return default.get("full_address") or default.get("address") or default.get("label") or ""
                        first = next((a for a in addresses if a.get("address_type") == addr_type), None)
                        if first:
                            return first.get("full_address") or first.get("address") or first.get("label") or ""
                        return contact.get(f"{addr_type}_address") or ""
                    def _default_attn(addr_type: str) -> str:
                        default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
                        if default and default.get("attn"):
                            return default["attn"]
                        first = next((a for a in addresses if a.get("address_type") == addr_type), None)
                        return (first.get("attn") or "") if first else ""
                    patch["contact_billing_address"] = _default_addr("billing")
                    patch["contact_shipping_address"] = _default_addr("shipping")
                    patch["shipping_attn"] = _default_attn("shipping")
                    patch["contact_tax_id"] = contact.get("tax_id") or ""
                    contact_pt = contact.get("payment_terms")
                    if contact_pt:
                        patch["payment_terms"] = contact_pt
                        # Also recalculate due_date if issue_date is set
                        doc_pre = await api.get_doc(token, entity_id)
                        terms_list = await api.get_payment_terms(token)
                        new_due = _calculate_due_date(doc_pre.get("issue_date"), contact_pt, terms_list)
                        if new_due:
                            patch["due_date"] = new_due
                    # Auto-populate price_list from contact (fallback to company default)
                    contact_pl = contact.get("price_list")
                    if contact_pl:
                        patch["price_list"] = contact_pl
                    else:
                        try:
                            default_pl = await api.get_default_price_list(token)
                            patch["price_list"] = default_pl
                        except Exception:
                            pass
                    # Propagate contact currency to draft doc (enables foreign-currency workflow)
                    contact_currency = contact.get("currency")
                    if contact_currency and doc.get("status", "draft") == "draft":
                        patch["currency"] = contact_currency
                except APIError:
                    pass  # contact fetch failure → skip auto-populate
            # Auto-calculate due_date when payment_terms changes
            elif field == "payment_terms" and value:
                try:
                    doc_pre = await api.get_doc(token, entity_id)
                    terms_list = await api.get_payment_terms(token)
                    new_due = _calculate_due_date(doc_pre.get("issue_date"), value, terms_list)
                    if new_due:
                        patch["due_date"] = new_due
                except APIError:
                    pass
            # Auto-populate terms_text when terms_template changes
            elif field == "terms_template" and value:
                try:
                    tc_templates = await api.get_terms_conditions(token)
                    tmpl = next((tc for tc in tc_templates if tc.get("name") == value), None)
                    if tmpl:
                        patch["terms_text"] = tmpl.get("text", "")
                except (APIError, Exception):
                    pass
            # Resolve commission contact name for display
            elif field == "commission_contact_id" and value:
                try:
                    contact = await api.get_contact(token, value)
                    name = contact.get("name") or contact.get("display_name")
                    if name:
                        patch["commission_contact_name"] = name
                except APIError:
                    pass
            # ref_id edits go through /renumber (works on finalized docs; patch_doc rejects them)
            if field == "ref_id":
                await api.renumber_doc(token, entity_id, value)
            else:
                await api.patch_doc(token, entity_id, patch)
            # Reprice line items when price_list changed
            new_pl = patch.get("price_list")
            if new_pl:
                try:
                    doc_pre = await api.get_doc(token, entity_id)
                    lines = doc_pre.get("line_items") or []
                    if lines:
                        updated = []
                        repriced = 0
                        for line in lines:
                            sku = (line.get("sku") or "").strip()
                            if sku:
                                try:
                                    resp = await api.list_items(token, {"sku": sku, "limit": 1})
                                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                                    if items:
                                        new_price = resolve_price(items[0], new_pl)
                                        line = {**line, "unit_price": new_price, "price_list": new_pl}
                                        repriced += 1
                                except Exception:
                                    pass
                            updated.append(line)
                        if repriced:
                            await api.patch_doc(token, entity_id, {"line_items": updated})
                except Exception:
                    pass  # reprice failure is non-fatal
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        # Contact, price_list, terms_template, or currency changes affect multiple sections - full page refresh
        if field in ("contact_id", "price_list", "terms_template"):
            from starlette.responses import Response as _R
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        # Resolve contact fields to display names
        if field in ("contact_id", "commission_contact_id"):
            display_value = _resolve_contact_display(doc, field)
        else:
            display_value = doc.get(field)
        return _doc_display_cell(entity_id, field, display_value)

    @app.post("/docs/{entity_id}/field/{field}")
    async def doc_field_post(request: Request, entity_id: str, field: str):
        """Handle autosave of text fields (customer_note, internal_note) via hx_post."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401)
        form = await request.form()
        # Field name may come as the field name itself or as 'value'
        value = str(form.get(field, form.get("value", "")))
        try:
            await api.patch_doc(token, entity_id, {field: value})
        except APIError:
            pass  # silent autosave failure
        return _R("", status_code=204)

    @app.post("/docs/{entity_id}/notes")
    async def doc_add_note(request: Request, entity_id: str):
        """Add a note to a document."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        form = await request.form()
        note = str(form.get("note", "")).strip()
        if note:
            try:
                await api.add_doc_note(token, entity_id, note)
            except APIError:
                pass
        return await _doc_notes_section_response(token, entity_id, is_list=False)

    @app.get("/docs/{entity_id}/notes/{note_id}/edit")
    async def doc_note_edit_form(request: Request, entity_id: str, note_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        try:
            notes = await api.list_doc_notes(token, entity_id)
        except APIError:
            notes = []
        note = next((n for n in notes if (n.get("note_id") or n.get("id")) == note_id), {})
        return _shared_note_edit_form(
            note_id=note_id,
            current_text=note.get("note", ""),
            save_url=f"/docs/{entity_id}/notes/{note_id}",
            cancel_url=f"/docs/{entity_id}/notes/refresh",
            refresh_target=f"#notes-section-{_safe_id(entity_id)}",
        )

    @app.patch("/docs/{entity_id}/notes/{note_id}")
    async def doc_edit_note(request: Request, entity_id: str, note_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        form = await request.form()
        note = str(form.get("note", "")).strip()
        if note:
            try:
                await api.update_doc_note(token, entity_id, note_id, note)
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
        return await _doc_notes_section_response(token, entity_id, is_list=False)

    @app.delete("/docs/{entity_id}/notes/{note_id}")
    async def doc_delete_note(request: Request, entity_id: str, note_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        try:
            await api.delete_doc_note(token, entity_id, note_id)
        except APIError:
            pass
        return await _doc_notes_section_response(token, entity_id, is_list=False)

    @app.get("/docs/{entity_id}/notes/refresh")
    async def doc_notes_refresh(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        return await _doc_notes_section_response(token, entity_id, is_list=False)

    @app.post("/lists/{entity_id}/notes")
    async def list_add_note(request: Request, entity_id: str):
        """Add a note to a list."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        form = await request.form()
        note = str(form.get("note", "")).strip()
        if note:
            try:
                await api.add_list_note(token, entity_id, note)
            except APIError:
                pass
        return await _doc_notes_section_response(token, entity_id, is_list=True)

    @app.get("/lists/{entity_id}/notes/{note_id}/edit")
    async def list_note_edit_form(request: Request, entity_id: str, note_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        try:
            notes = await api.list_list_notes(token, entity_id)
        except APIError:
            notes = []
        note = next((n for n in notes if (n.get("note_id") or n.get("id")) == note_id), {})
        return _shared_note_edit_form(
            note_id=note_id,
            current_text=note.get("note", ""),
            save_url=f"/lists/{entity_id}/notes/{note_id}",
            cancel_url=f"/lists/{entity_id}/notes/refresh",
            refresh_target=f"#notes-section-{_safe_id(entity_id)}",
        )

    @app.patch("/lists/{entity_id}/notes/{note_id}")
    async def list_edit_note(request: Request, entity_id: str, note_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        form = await request.form()
        note = str(form.get("note", "")).strip()
        if note:
            try:
                await api.update_list_note(token, entity_id, note_id, note)
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
        return await _doc_notes_section_response(token, entity_id, is_list=True)

    @app.delete("/lists/{entity_id}/notes/{note_id}")
    async def list_delete_note(request: Request, entity_id: str, note_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        try:
            await api.delete_list_note(token, entity_id, note_id)
        except APIError:
            pass
        return await _doc_notes_section_response(token, entity_id, is_list=True)

    @app.get("/lists/{entity_id}/notes/refresh")
    async def list_notes_refresh(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _Res
            return _Res("", status_code=401)
        return await _doc_notes_section_response(token, entity_id, is_list=True)


    # T2: Save line items
    @app.post("/docs/{entity_id}/lines")
    async def save_doc_lines(request: Request, entity_id: str):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        lines = body.get("line_items", [])
        subtotal = body.get("subtotal", 0)
        tax = body.get("tax", 0)
        total = body.get("total", subtotal + tax)
        patch_data = {
            "line_items": lines,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
        }
        try:
            await api.patch_doc(token, entity_id, patch_data)
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        return JSONResponse({"ok": True})

    # T2b: Reprice line items from a given price list
    @app.post("/docs/{entity_id}/reprice")
    async def reprice_doc_lines(request: Request, entity_id: str):
        """Re-resolve unit_price for all line items that came from inventory.

        Body: {"price_list": "Retail"}

        Only lines with a `sku` field (i.e. sourced from inventory) are repriced.
        Lines without a sku (manually entered) are left unchanged.
        """
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        price_list = (body.get("price_list") or "").strip()
        if not price_list:
            return JSONResponse({"error": "price_list is required"}, status_code=400)
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        existing_lines: list[dict] = doc.get("line_items") or []
        if not existing_lines:
            return JSONResponse({"ok": True, "repriced": 0})
        repriced = 0
        updated_lines = []
        for line in existing_lines:
            sku = (line.get("sku") or "").strip()
            if sku:
                # Look up current item price via catalog endpoint (SKU lookup)
                try:
                    resp = await api.list_items(token, {"sku": sku, "limit": 1})
                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                    if items:
                        new_price = resolve_price(items[0], price_list)
                        line = {**line, "unit_price": new_price, "price_list": price_list}
                        repriced += 1
                except APIError:
                    pass  # leave line unchanged on lookup failure
            updated_lines.append(line)
        # Recalculate totals
        subtotal = sum(
            float(l.get("unit_price", 0)) * float(l.get("quantity", 0))
            for l in updated_lines
        )
        tax_rate = float(doc.get("tax_rate", 0) or 0)
        tax = round(subtotal * tax_rate / 100, 2)
        total = round(subtotal + tax, 2)
        try:
            await api.patch_doc(token, entity_id, {
                "line_items": updated_lines,
                "price_list": price_list,
                "subtotal": round(subtotal, 2),
                "tax": tax,
                "total": total,
            })
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        return JSONResponse({"ok": True, "repriced": repriced, "price_list": price_list})

    # T3: Document actions (finalize, void, send, mark_sent, unmark_sent)
    @app.post("/docs/{entity_id}/action/{action}")
    async def doc_action(request: Request, entity_id: str, action: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            if action == "finalize":
                await api.finalize_doc(token, entity_id)
            elif action == "send":
                sent_to = str(form.get("sent_to", "")).strip()
                if not sent_to:
                    return _R("", status_code=204, headers={"HX-Redirect": "/settings/general?tab=cloud-relay"})
                data: dict = {
                    "sent_to": sent_to,
                    "sent_via": "email",
                    "cc": str(form.get("cc", "")).strip() or None,
                    "bcc": str(form.get("bcc", "")).strip() or None,
                }
                await api.send_doc(token, entity_id, data=data)
            elif action == "mark_sent":
                await api.send_doc(token, entity_id, data={"sent_via": "manual"})
            elif action == "unmark_sent":
                await api.revert_doc_to_draft(token, entity_id, reason=None)
            elif action == "void":
                reason = str(form.get("reason", "")).strip() or None
                await api.void_doc(token, entity_id, reason)
            elif action == "revert_to_draft":
                reason = str(form.get("reason", "")).strip() or None
                await api.revert_doc_to_draft(token, entity_id, reason)
            elif action == "unvoid":
                await api.unvoid_doc(token, entity_id)
            elif action == "delete":
                await api.delete_doc(token, entity_id)
                doc_type = str(form.get("doc_type", "")).strip() or "invoice"
                if doc_type == "subscription_invoice":
                    return _R("", status_code=204, headers={"HX-Redirect": "/subscriptions?direction=sales"})
                if doc_type == "subscription_po":
                    return _R("", status_code=204, headers={"HX-Redirect": "/subscriptions?direction=purchasing"})
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs?type={doc_type}"})
            elif action == "create-credit-note":
                # Fetch source invoice and pre-populate CN with its line items (negated quantities)
                source = await api.get_doc(token, entity_id)
                line_items = [
                    {k: v for k, v in li.items() if k not in ("id", "line_total")}
                    for li in (source.get("line_items") or [])
                ]
                cn_payload = {
                    "doc_type": "credit_note",
                    "original_doc_id": entity_id,
                    "contact_id": source.get("contact_id"),
                    "line_items": line_items,
                    "subtotal": source.get("subtotal") or 0,
                    "tax": source.get("tax") or 0,
                    "total": source.get("total") or 0,
                }
                result = await api.create_doc(token, cn_payload)
                cn_id = result.get("entity_id") or result.get("id", "")
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{cn_id}"})
            else:
                return _R("", status_code=400)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            # Return error inline
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                hx_swap_oob="true", id="action-error",
            )
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # T4: Record payment
    @app.post("/docs/{entity_id}/payment")
    async def record_doc_payment(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            amount_str = str(form.get("amount", "0"))
            try:
                amount = float(amount_str)
            except ValueError:
                amount = 0.0
            payment_date = str(form.get("payment_date", "")).strip() or None
            method = str(form.get("method", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            bank_account = str(form.get("bank_account", "")).strip() or None
            conversion_rate_str = str(form.get("conversion_rate", "")).strip()
            conversion_rate = float(conversion_rate_str) if conversion_rate_str else None
            await api.record_payment(token, entity_id, {
                "amount": amount,
                "method": method,
                "reference": reference,
                "payment_date": payment_date,
                "bank_account": bank_account,
                "conversion_rate": conversion_rate,
            })
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                id="payment-error",
            )
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # T1: Convert quotation to invoice
    @app.post("/docs/{entity_id}/convert")
    async def convert_doc_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.convert_doc(token, entity_id)
            target_id = result.get("target_doc_id", entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})

    # T2: Receive PO goods
    @app.post("/docs/{entity_id}/receive")
    async def receive_po_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            location_id = str(form.get("location_id", "") or form.get("location_name", "")).strip()
            notes = str(form.get("notes", "")).strip() or None
            received_items = []
            idx = 0
            while f"item_id_{idx}" in form or f"sku_{idx}" in form:
                item_id = str(form.get(f"item_id_{idx}", "")).strip() or None
                sku = str(form.get(f"sku_{idx}", "")).strip() or None
                name = str(form.get(f"name_{idx}", "")).strip() or None
                try:
                    qty = float(str(form.get(f"qty_{idx}", "0")))
                except ValueError:
                    qty = 0.0
                if qty > 0:
                    item = {"po_line_index": idx, "quantity_received": qty}
                    if item_id:
                        item["item_id"] = item_id
                    if sku:
                        item["sku"] = sku
                    if name:
                        item["name"] = name
                    received_items.append(item)
                idx += 1
            data = {"location_id": location_id, "received_items": received_items}
            if notes:
                data["notes"] = notes
            await api.receive_po(token, entity_id, data)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # T7: Refund payment
    @app.post("/docs/{entity_id}/refund")
    async def refund_payment_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            try:
                amount = float(str(form.get("amount", "0")))
            except ValueError:
                amount = 0.0
            method = str(form.get("method", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            await api.refund_payment(token, entity_id, {
                "amount": amount,
                "method": method,
                "reference": reference,
            })
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="refund-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # ---- Payment management routes ----

    @app.post("/docs/{entity_id}/void-payment")
    async def void_payment_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            payment_index = int(form.get("payment_index", -1))
            void_reason = str(form.get("void_reason", "")).strip()
            await api.void_payment(token, entity_id, payment_index, void_reason)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="payment-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/{entity_id}/apply-credit")
    async def apply_credit_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            target_doc_id = str(form.get("target_doc_id", "")).strip()
            amount = float(form.get("amount", 0))
            date = str(form.get("date", "")).strip() or None
            await api.apply_credit_note(token, entity_id, target_doc_id, amount, date)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Span(str(e.detail), cls="flash flash--error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/{entity_id}/refund-credit")
    async def refund_credit_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            amount = float(form.get("amount", 0))
            date = str(form.get("date", "")).strip() or None
            method = str(form.get("method", "")).strip() or None
            bank_account = str(form.get("bank_account", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            await api.refund_credit_note(token, entity_id, amount, date, method, bank_account, reference)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="payment-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/bulk-payment")
    async def bulk_payment_route(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            doc_ids = [v.strip() for v in form.getlist("doc_ids") if v.strip()]
            amount = float(form.get("amount", 0))
            payment_date = str(form.get("payment_date", "")).strip() or None
            method = str(form.get("method", "")).strip() or None
            bank_account = str(form.get("bank_account", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            await api.bulk_payment(token, doc_ids, amount, payment_date, method, bank_account, reference)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="bulk-payment-error")
        # Refresh the page
        doc_type = str(form.get("doc_type", "invoice")).strip()
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs?type={doc_type}"})

    @app.get("/docs/bulk-payment-panel")
    async def bulk_payment_panel(request: Request):
        """HTMX endpoint: render inline bulk payment panel for selected docs."""
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized")), id="bulk-payment-panel")
        doc_ids_raw = request.query_params.get("doc_ids", "")
        doc_ids = [d.strip() for d in doc_ids_raw.split(",") if d.strip()]
        if not doc_ids:
            return Div(P(t("doc.no_documents_selected")), id="bulk-payment-panel")

        docs = []
        for did in doc_ids:
            try:
                docs.append(await api.get_doc(token, did))
            except APIError:
                pass
        if not docs:
            return Div(P(t("doc.could_not_load_selected_documents")), id="bulk-payment-panel")

        # Validate same contact
        contact_ids = set(d.get("contact_id") or "" for d in docs)
        contact_ids.discard("")
        if len(contact_ids) > 1:
            return Div(
                P(t("doc._all_selected_documents_must_be_from_the_same_cont"), cls="flash flash--error"),
                id="bulk-payment-panel",
            )

        # Fetch bank accounts for dropdown
        bank_accounts = []
        try:
            bank_accounts = (await api.get_bank_accounts(token)).get("items", [])
        except Exception:
            pass

        contact_name = docs[0].get("contact_name") or ""
        doc_type = docs[0].get("doc_type") or "invoice"
        currency = docs[0].get("currency") or "USD"

        # Filter to payable docs and sort by due date
        payable = [d for d in docs if d.get("status") not in ("draft", "void", "paid") and float(d.get("amount_outstanding") or d.get("outstanding_balance") or 0) > 0]
        payable.sort(key=lambda d: d.get("due_date") or d.get("issue_date") or "")
        skipped = len(docs) - len(payable)
        total_outstanding = sum(float(d.get("amount_outstanding") or d.get("outstanding_balance") or 0) for d in payable)

        alloc_rows = []
        for d in payable:
            eid = d.get("entity_id") or d.get("id", "")
            doc_num = d.get("doc_number") or d.get("ref_id") or eid
            due = d.get("due_date") or "--"
            outstanding = float(d.get("amount_outstanding") or d.get("outstanding_balance") or 0)
            alloc_rows.append(Tr(
                Td(doc_num),
                Td(str(due)[:10]),
                Td(fmt_money(outstanding, currency), cls="cell--number"),
                Td(Span("--", cls="alloc-amount"), cls="cell--number"),
                data_outstanding=str(outstanding),
                data_doc_id=eid,
            ))

        from datetime import date as _d
        today = _d.today().isoformat()
        _methods = [Option(t("doc.cash"), value="cash"), Option(t("doc.bank_transfer"), value="transfer"),
                    Option(t("doc.card"), value="card"), Option(t("doc.check"), value="check"), Option(t("doc.other"), value="other")]
        _bank_opts = _bank_account_options(bank_accounts, default_code=bank_accounts[0].get("chart_account_code") if bank_accounts else None)

        panel = Div(
            H3(f"Bulk Payment — {contact_name} ({len(payable)} document{'s' if len(payable) != 1 else ''})", cls="section-title"),
            P(f"{skipped} document(s) skipped (already paid or draft).", cls="text-muted") if skipped else "",
            P(f"Total Outstanding: {fmt_money(total_outstanding, currency)}", cls="total-label--final"),
            Table(
                Thead(Tr(Th(t("th.document")), Th(t("th.due_date")), Th(t("th.outstanding")), Th(t("th.allocation")))),
                Tbody(*alloc_rows),
                cls="data-table data-table--compact", id="bulk-alloc-table",
            ),
            Form(
                *hidden_ids,
                Input(type="hidden", name="doc_type", value=doc_type),
                Div(
                    Div(Label(t("label.amount"), cls="form-label"),
                        Input(type="number", name="amount", value=f"{total_outstanding:.2f}", step="0.01",
                              min="0", cls="form-input", id="bulk-pay-amount",
                              oninput="celerpUpdateBulkAlloc()"), cls="form-group"),
                    Div(Label(t("th.date"), cls="form-label"),
                        Input(type="date", name="payment_date", value=today, cls="form-input"), cls="form-group"),
                    Div(Label(t("label.method"), cls="form-label"),
                        Select(*_methods, name="method", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.bank_account"), cls="form-label"),
                        Select(*_bank_opts, name="bank_account", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.reference"), cls="form-label"),
                        Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Span("", id="bulk-payment-error"),
                Div(
                    Button(t("btn.save_payment"), type="submit", cls="btn btn--primary"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--ghost",
                           onclick="document.getElementById('bulk-payment-panel').innerHTML=''"),
                    cls="form-actions",
                ),
                hx_post="/docs/bulk-payment", hx_swap="none", cls="form-card",
            ),
            Script(f"""
function celerpUpdateBulkAlloc() {{
    const amount = parseFloat(document.getElementById('bulk-pay-amount')?.value || 0);
    let remaining = amount;
    document.querySelectorAll('#bulk-alloc-table tbody tr').forEach(row => {{
        const outstanding = parseFloat(row.dataset.outstanding || 0);
        const alloc = Math.min(remaining, outstanding);
        remaining = Math.max(0, remaining - alloc);
        row.querySelector('.alloc-amount').textContent = alloc > 0 ? '{currency_symbol(currency)}' + alloc.toFixed(2) : '--';
    }});
}}
celerpUpdateBulkAlloc();
"""),
            id="bulk-payment-panel",
            cls="bulk-payment-panel",
        )
        return panel

    @app.get("/payments")
    async def payments_list_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        tab = request.query_params.get("tab", "all")
        q = request.query_params.get("q", "")
        method_filter = request.query_params.get("method", "")
        contact_filter = request.query_params.get("contact", "")
        date_from, date_to, preset = _parse_dates(request)

        try:
            company = await api.get_company(token)
        except Exception:
            company = {}
        currency = company.get("currency") or None

        # Fetch all docs and extract payments
        try:
            docs_resp = await api.list_docs(token, {"limit": 5000, "exclude_status": "draft"})
            all_docs = docs_resp.get("items", []) if isinstance(docs_resp, dict) else docs_resp
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            all_docs = []

        # Build flat payment list from all docs
        payments_list: list[dict] = []
        for d in all_docs:
            eid = d.get("entity_id") or d.get("id", "")
            doc_number = d.get("doc_number") or d.get("ref_id") or eid
            doc_type = d.get("doc_type", "")
            contact_name = d.get("contact_name") or d.get("contact_id") or ""
            contact_id = d.get("contact_id") or ""
            doc_currency = d.get("currency") or currency
            for p in (d.get("payments") or []):
                payments_list.append({
                    "doc_id": eid,
                    "doc_number": doc_number,
                    "doc_type": doc_type,
                    "contact_name": contact_name,
                    "contact_id": contact_id,
                    "date": p.get("payment_date") or p.get("recorded_at", "")[:10],
                    "method": p.get("method", ""),
                    "bank_account": p.get("bank_account", ""),
                    "reference": p.get("reference", ""),
                    "amount": float(p.get("amount", 0)),
                    "status": p.get("status", "active"),
                    "currency": doc_currency,
                    "index": p.get("index", 0),
                })

        # Apply filters
        if tab == "received":
            payments_list = [p for p in payments_list if p["doc_type"] in ("invoice", "credit_note")]
        elif tab == "sent":
            payments_list = [p for p in payments_list if p["doc_type"] in ("bill", "purchase_order")]
        elif tab == "voided":
            payments_list = [p for p in payments_list if p["status"] == "voided"]

        if q:
            q_lower = q.lower()
            payments_list = [p for p in payments_list if q_lower in p["doc_number"].lower() or q_lower in p["reference"].lower()]
        if method_filter:
            payments_list = [p for p in payments_list if p["method"] == method_filter]
        if contact_filter:
            c_lower = contact_filter.lower()
            payments_list = [p for p in payments_list if c_lower in p["contact_name"].lower()]
        if date_from:
            payments_list = [p for p in payments_list if p["date"] >= date_from]
        if date_to:
            payments_list = [p for p in payments_list if p["date"] <= date_to]

        # Sort newest first
        payments_list.sort(key=lambda p: p["date"], reverse=True)

        # Summary cards
        active_payments = [p for p in payments_list if p["status"] == "active"]
        total_received = sum(p["amount"] for p in active_payments if p["doc_type"] in ("invoice", "credit_note"))
        total_sent = sum(p["amount"] for p in active_payments if p["doc_type"] in ("bill", "purchase_order"))

        # Build tabs
        tab_cls = lambda t: f"category-tab {'category-tab--active' if tab == t else ''}"
        extra_params = f"&q={q}&method={method_filter}&contact={contact_filter}" if any([q, method_filter, contact_filter]) else ""
        tabs = Div(
            A(t("doc.all"), href=f"/payments?tab=all{extra_params}", cls=tab_cls("all")),
            A(t("doc.received"), href=f"/payments?tab=received{extra_params}", cls=tab_cls("received")),
            A(t("doc.sent"), href=f"/payments?tab=sent{extra_params}", cls=tab_cls("sent")),
            A(t("doc.voided"), href=f"/payments?tab=voided{extra_params}", cls=tab_cls("voided")),
            cls="category-tabs",
        )

        # Summary
        summary_bar = Div(
            Span(f"Received: {fmt_money(total_received, currency)}", cls="val-chip"),
            Span(f"Sent: {fmt_money(total_sent, currency)}", cls="val-chip"),
            Span(f"Net: {fmt_money(total_received - total_sent, currency)}", cls="val-chip val-chip--alert"),
            cls="valuation-bar",
        )

        # Filters
        _methods_opts = [Option(t("doc.all_methods"), value=""), Option(t("doc.cash"), value="cash"),
                         Option(t("btn.transfer"), value="transfer"), Option(t("doc.card"), value="card"),
                         Option(t("doc.check"), value="check"), Option(t("doc.credit_note"), value="credit_note"),
                         Option(t("doc.other"), value="other")]
        filter_bar = Div(
            Input(type="search", name="q", value=q, placeholder="Search doc# or reference...",
                  cls="form-input form-input--sm", style="max-width:200px;",
                  onchange=f"window.location='/payments?tab={tab}&q='+this.value+'&method={method_filter}&contact={contact_filter}'"),
            Input(type="text", name="contact", value=contact_filter, placeholder="Contact...",
                  cls="form-input form-input--sm", style="max-width:200px;",
                  onchange=f"window.location='/payments?tab={tab}&q={q}&method={method_filter}&contact='+this.value"),
            Select(*_methods_opts, name="method",
                   cls="form-input form-input--sm", style="max-width:150px;",
                   onchange=f"window.location='/payments?tab={tab}&q={q}&method='+this.value+'&contact={contact_filter}'"),
            cls="filter-bar",
            style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:1rem;",
        )

        # Table
        def _pay_row(p: dict) -> FT:
            voided = p["status"] == "voided"
            row_cls = "data-row" + (" payment-voided" if voided else "")
            return Tr(
                Td(format_value(p["date"], "date")),
                Td(A(p["doc_number"], href=f"/docs/{p['doc_id']}", cls="table-link")),
                Td(p["contact_name"] or EMPTY),
                Td(format_value(p["method"], "badge")),
                Td(p["reference"] or EMPTY),
                Td(fmt_money(p["amount"], p.get("currency")), cls="cell--number"),
                Td(
                    Span(t("doc.voided"), cls="badge badge--void") if voided
                    else Span(t("th.active"), cls="badge badge--green"),
                ),
                cls=row_cls,
            )

        payment_table = Table(
            Thead(Tr(Th(t("th.date")), Th(t("th.document")), Th(t("page.contact_detail")), Th(t("label.method")), Th(t("label.reference")), Th(t("label.amount")), Th(t("th.status")))),
            Tbody(*[_pay_row(p) for p in payments_list]) if payments_list else Tbody(Tr(Td(t("doc.no_payments_found"), colspan="7", cls="empty-state-msg"))),
            cls="data-table", id="payments-table",
        )

        lang = get_lang(request)
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Payments", None)]),
            page_header("Payments"),
            tabs,
            _date_filter_bar("/payments", date_from, date_to, preset, extra_params=f"&tab={tab}{extra_params}", lang=lang),
            summary_bar,
            filter_bar,
            payment_table,
            title="Payments - Celerp",
            nav_active="payments",
            lang=lang,
            request=request,
        )

    @app.get("/docs/{entity_id}/open-invoices")
    async def doc_open_invoices(request: Request, entity_id: str):
        """HTMX endpoint: fetch open invoices for same contact (for credit note application picker)."""
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse([], status_code=401)
        try:
            doc = await api.get_doc(token, entity_id)
            contact_id = doc.get("contact_id")
            if not contact_id:
                return JSONResponse([])
            resp = await api.list_docs(token, {"contact_id": contact_id, "doc_type": "invoice", "limit": 100})
            invoices = resp.get("items", [])
            # Filter to open invoices with outstanding > 0
            open_inv = []
            for inv in invoices:
                outstanding = float(inv.get("amount_outstanding") or inv.get("outstanding_balance") or 0)
                if inv.get("status") not in ("draft", "void", "paid") and outstanding > 0:
                    open_inv.append({
                        "id": inv.get("entity_id") or inv.get("id", ""),
                        "doc_number": inv.get("doc_number") or inv.get("ref_id") or "",
                        "outstanding": outstanding,
                        "contact_name": inv.get("contact_name") or "",
                    })
            return JSONResponse(open_inv)
        except Exception:
            return JSONResponse([])

    @app.post("/docs/{entity_id}/share")
    async def create_share_link_route(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.create_share_link(token, entity_id)
            share_url = result.get("url") or result.get("token", "")
            return Span(
                Input(type="text", value=share_url, readonly=True,
                      cls="form-input form-input--inline share-url-input",
                      onclick="this.select()"),
                " ",
                A(t("doc.open"), href=share_url, target="_blank", cls="btn btn--secondary btn--xs"),
                cls="share-result",
            )
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error")

    # -----------------------------------------------------------------------
    # Fulfillment toggle routes
    # -----------------------------------------------------------------------

    @app.post("/docs/{entity_id}/fulfill")
    async def doc_fulfill(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.fulfill_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                hx_swap_oob="true", id="action-error",
            )
        # Re-fetch doc and return updated toggle
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return _render_fulfill_section(doc)

    @app.post("/docs/{entity_id}/unfulfill")
    async def doc_unfulfill(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.unfulfill_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                hx_swap_oob="true", id="action-error",
            )
        # Re-fetch doc and return updated section
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return _render_fulfill_section(doc)

    @app.post("/docs/{entity_id}/receive-return")
    async def doc_receive_return(request: Request, entity_id: str):
        import re as _re
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        cid_safe = f"receive-return-{entity_id}".replace(":", "-")
        form = await request.form()
        items_raw: dict[int, dict] = {}
        for key, value in form.multi_items():
            m = _re.match(r"items\[(\d+)\]\[(\w+)\]", key)
            if m:
                idx, field = int(m.group(1)), m.group(2)
                items_raw.setdefault(idx, {})[field] = value
        items = []
        for idx in sorted(items_raw):
            row = items_raw[idx]
            try:
                qty = float(row.get("quantity") or 0)
            except (ValueError, TypeError):
                qty = 0.0
            if qty <= 0:
                continue
            items.append({"sku": row.get("sku", ""), "quantity": qty})
        if not items:
            return Div(Span(t("doc.no_valid_quantities_entered"), cls="flash flash--error"), id=cid_safe)
        try:
            await api.receive_return(token, entity_id, items)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id=cid_safe)
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return _render_receive_return_section(doc)

    @app.delete("/docs/{entity_id}/receive-return")
    async def doc_undo_receive_return(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        cid_safe = f"receive-return-{entity_id}".replace(":", "-")
        try:
            await api.undo_receive_return(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id=cid_safe)
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return _render_receive_return_section(doc)

    @app.post("/docs/{entity_id}/receive-goods")
    async def doc_receive_goods(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401)
        cid_safe = f"receive-goods-{entity_id}".replace(":", "-")
        try:
            doc = await api.get_doc(token, entity_id)
            line_items = doc.get("line_items") or []
            await api.receive_goods(token, entity_id, line_items)
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                from starlette.responses import Response as _R2
                return _R2("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id=cid_safe)
        return _render_receive_goods_section(doc)

    @app.delete("/docs/{entity_id}/receive-goods")
    async def doc_undo_receive_goods(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        cid_safe = f"receive-goods-{entity_id}".replace(":", "-")
        try:
            await api.undo_receive_goods(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id=cid_safe)
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return _render_receive_goods_section(doc)

    # ── Doc file management (upload / delete / tag / description / download) ──

    @app.post("/docs/{entity_id}/files")
    async def doc_upload_file(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            try:
                doc = await api.get_doc(token, entity_id)
            except APIError:
                doc = {"entity_id": entity_id}
            return _doc_files_section("doc", entity_id, _enrich_doc_files(doc))
        description = str(form.get("description", "")).strip()
        document_tag = str(form.get("document_tag", "")).strip()
        content = await file.read()
        filename = getattr(file, "filename", "upload")
        content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
        try:
            await api.upload_doc_file(token, entity_id, content, filename, content_type, description, document_tag)
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_files_section("doc", entity_id, _enrich_doc_files(doc))

    @app.delete("/docs/{entity_id}/files/{file_id}")
    async def doc_delete_file(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.delete_doc_file(token, entity_id, file_id)
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_files_section("doc", entity_id, _enrich_doc_files(doc))

    @app.post("/docs/{entity_id}/files/{file_id}/tag")
    async def doc_tag_file(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        document_tag = str(form.get("document_tag", "")).strip()
        try:
            await api.tag_doc_file(token, entity_id, file_id, document_tag)
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_files_section("doc", entity_id, _enrich_doc_files(doc))

    @app.post("/docs/{entity_id}/files/{file_id}/description")
    async def doc_patch_file_description(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        description = str(form.get("description", "")).strip()
        try:
            await api.patch_doc_file_description(token, entity_id, file_id, description)
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_files_section("doc", entity_id, _enrich_doc_files(doc))

    @app.get("/docs/{entity_id}/files/_section")
    async def doc_files_section(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        qp = request.query_params
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_files_section("doc", entity_id, _enrich_doc_files(doc),
            page=int(qp.get("page", "1") or "1"),
            sort_dir=qp.get("sort_dir", "desc"),
            tag_filter=qp.get("tag_filter", ""),
            date_from=qp.get("date_from", ""),
            date_to=qp.get("date_to", ""),
            search=qp.get("search", ""),
        )

    @app.get("/docs/{entity_id}/files/{file_id}/download")
    async def doc_download_file(request: Request, entity_id: str, file_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            r = await api.download_doc_file(token, entity_id, file_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _R(content=r.content, status_code=r.status_code, headers=dict(r.headers))

    @app.get("/lists")
    async def lists_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        list_type = request.query_params.get("type", "")
        status = request.query_params.get("status", "")
        view = request.query_params.get("view", "")
        converted_to_type_list = request.query_params.get("converted_to_type", "")
        all_issued_list = request.query_params.get("all_issued", "") in ("1", "true")
        page = int(request.query_params.get("page", 1))
        is_drafts_view = view == "drafts" or status == "draft"
        effective_status = "draft" if is_drafts_view else ("exclude_draft" if not status else status)
        try:
            params: dict = {"limit": _PER_PAGE, "offset": (page - 1) * _PER_PAGE}
            if q:
                params["q"] = q
            if list_type:
                params["list_type"] = list_type
            if all_issued_list:
                params["all_issued"] = "1"
            elif effective_status == "exclude_draft":
                params["exclude_status"] = "draft"
            elif effective_status:
                params["status"] = effective_status
            if converted_to_type_list:
                params["converted_to_type"] = converted_to_type_list
            result = await api.list_lists(token, params)
            lists = result.get("items", [])
            filtered_total = result.get("total", len(lists))
            draft_count = (await api.list_lists(token, {"status": "draft", "limit": 1})).get("total", 0)
            summary = await api.get_list_summary(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            lists, summary, draft_count, filtered_total = [], {}, 0, 0
        lang = get_lang(request)
        return base_shell(
            page_header(
                t("page.lists", lang),
                _list_drafts_tab(draft_count, is_drafts_view, list_type),
                search_bar(placeholder="Search ref, customer...", target="#list-table", url="/lists/search"),
                Button(t("page.new_list"), hx_post="/lists/create-blank", hx_swap="none", cls="btn btn--primary", title="Create blank draft"),
                A(t("btn.export_csv"), href="/lists/export/csv", cls="btn btn--secondary"),
                A(t("doc.import_csv"), href="/lists/import", cls="btn btn--secondary"),
            ),
            _list_type_tabs(list_type),
            _list_status_cards(summary, status, converted_to_type=converted_to_type_list),
            _list_table(lists, lang=lang),
            pagination(page, filtered_total, _PER_PAGE, "/lists",
                       f"q={q}&type={list_type}&status={status}&view={view}".strip("&")),
            title="Lists - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.get("/lists/new")
    async def lists_new_redirect(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            result = await api.create_list(token, {"list_type": "quotation", "status": "draft"})
            return RedirectResponse(f"/lists/{result.get('entity_id') or result.get('id', '')}", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/lists", status_code=302)

    @app.post("/lists/new")
    async def lists_new_post_redirect(request: Request):
        return RedirectResponse("/lists", status_code=302)

    @app.get("/lists/search")
    async def lists_search(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        list_type = request.query_params.get("type", "")
        status = request.query_params.get("status", "")
        try:
            params: dict = {"limit": _PER_PAGE}
            if q:
                params["q"] = q
            if list_type:
                params["list_type"] = list_type
            if status:
                params["status"] = status
            lists = (await api.list_lists(token, params)).get("items", [])
        except APIError as e:
            logger.warning("API error on lists_search: %s", e.detail)
            lists = []
        return _list_table(lists, lang=get_lang(request))

    @app.get("/lists/export/csv")
    async def lists_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            data = await api.export_lists_csv(token)
        except APIError as e:
            logger.warning("API error on lists_export_csv: %s", e.detail)
            data = b"error\n"
        from starlette.responses import Response
        return Response(content=data, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=lists.csv"})

    @app.post("/lists/create-blank")
    async def create_blank_list(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.create_list(token, {"list_type": "quotation", "status": "draft"})
            entity_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            logger.warning("API error on create_blank_list: %s", e.detail)
            return _R("", status_code=500)
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{entity_id}"})

    @app.post("/lists/from-items")
    async def list_from_items_modal(request: Request):
        """Modal: choose to create new draft list or add to existing."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            drafts_resp = await api.list_lists(token, {"status": "draft", "limit": 20})
            drafts = drafts_resp.get("items", [])
        except APIError:
            drafts = []
        hidden_items = [Input(type="hidden", name="selected", value=eid) for eid in entity_ids]
        return _send_to_modal("List", "/lists/from-items/new", "/lists/from-items/add",
                              "/lists/from-items/search", drafts, hidden_items, "list")

    @app.post("/lists/from-items/new")
    async def create_list_from_items(request: Request):
        """Create a draft list pre-populated with line items from selected inventory items."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="modal-container")
        line_items = await _line_items_from_inventory(token, entity_ids)
        try:
            result = await api.create_list(token, {
                "list_type": "quotation",
                "status": "draft",
                "line_items": line_items,
            })
            list_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{list_id}"})

    @app.post("/lists/from-items/add")
    async def add_items_to_list(request: Request):
        """Append line items from selected inventory to an existing list."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_id = str(form.get("target_id", "")).strip()
        if not entity_ids or not target_id:
            return Div(P(t("label.no_items_or_target_selected"), cls="flash flash--warning"), id="modal-container")
        new_lines = await _line_items_from_inventory(token, entity_ids)
        try:
            lst = await api.get_list(token, target_id)
            existing_lines = lst.get("line_items") or []
            combined = existing_lines + new_lines
            subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
            await api.patch_list(token, target_id, {
                "line_items": combined,
                "subtotal": subtotal,
                "total": subtotal,
            })
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{target_id}"})

    @app.get("/lists/from-items/search")
    async def list_from_items_search(request: Request):
        """HTMX search endpoint for the list picker dropdown."""
        token = _token(request)
        if not token:
            return Div()
        q = request.query_params.get("q", "").strip()
        try:
            resp = await api.list_lists(token, {"q": q, "limit": 20} if q else {"status": "draft", "limit": 20})
            items = resp.get("items", [])
        except APIError:
            items = []
        return _send_to_option_list(items, "list")
    @app.get("/lists/{entity_id}")
    async def list_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 404:
                from starlette.responses import HTMLResponse as _HR
                return _HR("<h2>List not found</h2><p><a href='/lists'>Back to Lists</a></p>", status_code=404)
            lst = {}

        # Inject doc_type so _doc_detail() treats it as a list
        lst.setdefault("doc_type", "list")

        # Map list "receiver"/"customer_name" → standard contact fields
        if not lst.get("contact_name"):
            lst["contact_name"] = lst.get("receiver") or lst.get("customer_name") or lst.get("customer_id") or ""
        if not lst.get("issue_date"):
            lst["issue_date"] = lst.get("created_at") or lst.get("date")

        # Inject company fields
        if not lst.get("company_name"):
            try:
                company = await api.get_company(token)
                lst.update({
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or company.get("settings", {}).get("address") or "",
                    "company_phone": company.get("phone") or company.get("settings", {}).get("phone") or "",
                    "company_tax_id": company.get("tax_id") or company.get("settings", {}).get("tax_id") or "",
                    "company_email": company.get("email") or company.get("settings", {}).get("email") or "",
                })
            except Exception:
                pass

        # Fetch price lists
        price_lists: list[dict] = []
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            pass

        # Fetch company timezone for notes display
        tz: str = "UTC"
        try:
            _co = await api.get_company(token)
            tz = _co.get("timezone") or "UTC"
        except Exception:
            pass
        company_taxes: list[dict] = []
        try:
            company_taxes = await api.get_taxes(token)
        except Exception:
            pass

        list_notes: list[dict] = []
        try:
            list_notes = await api.list_list_notes(token, entity_id)
        except Exception:
            pass

        ref = lst.get("ref_id") or entity_id
        status = lst.get("status", "draft")
        status_label = status.replace("_", " ").title()
        list_type_label = (lst.get("list_type") or "List").replace("_", " ").title()
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Lists", "/lists"), (f"{status_label} {ref}", None)]),
            page_header(f"{list_type_label} - {status_label} {ref}"),
            _doc_detail(lst, price_lists=price_lists, tz=tz, company_taxes=company_taxes, role=_get_role(request), notes=list_notes),
            title=f"List {ref} - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.get("/lists/{entity_id}/field/{field}/edit")
    async def list_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        value = str(lst.get(field, "") or "")
        restore_url = f"/lists/{entity_id}/field/{field}/display"
        patch_url = f"/lists/{entity_id}/field/{field}"
        esc_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        enter_js = "if(event.key==='Enter'){event.preventDefault();this.blur();}"
        if field == "list_type":
            input_el = Select(
                *[Option(lt.replace("_", " ").title(), value=lt, selected=(lt == value)) for lt in _LIST_TYPES],
                name="value",
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML", hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js,
            )
        elif field in _LIST_DATE_FIELDS or field in ("issue_date",):
            input_el = Input(
                type="date", name="value", value=value[:10] if value else "",
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
            )
        elif field == "status":
            _list_statuses = ["draft", "sent", "accepted", "completed", "void", "converted"]
            input_el = Select(
                *[Option(s.replace("_", " ").title(), value=s, selected=(s == value)) for s in _list_statuses],
                name="value",
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change", cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js,
            )
        else:
            input_el = Input(
                type="text", name="value", value=value,
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
            )
        return Div(input_el, cls="editable-cell editable-cell--editing")

    @app.get("/lists/{entity_id}/field/{field}/display")
    async def list_field_display(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        return _doc_display_cell(entity_id, field, lst.get(field), "list")

    @app.patch("/lists/{entity_id}/field/{field}")
    async def list_field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        try:
            await api.patch_list(token, entity_id, {field: value})
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_display_cell(entity_id, field, lst.get(field), "list")

    @app.post("/lists/{entity_id}/lines")
    async def save_list_lines(request: Request, entity_id: str):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        patch_data = {
            "line_items": body.get("line_items", []),
            "subtotal": body.get("subtotal", 0),
            "tax": body.get("tax", 0),
            "total": body.get("total", 0),
        }
        try:
            await api.patch_list(token, entity_id, patch_data)
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/lists/{entity_id}/action/{action}")
    async def list_action(request: Request, entity_id: str, action: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            if action == "send":
                await api.send_list(token, entity_id)
            elif action == "accept":
                await api.accept_list(token, entity_id)
            elif action == "complete":
                await api.complete_list(token, entity_id)
            elif action == "void":
                reason = str(form.get("reason", "")).strip() or None
                await api.void_list(token, entity_id, reason)
            elif action == "revert_to_draft":
                reason = str(form.get("reason", "")).strip() or None
                await api.revert_list_to_draft(token, entity_id, reason)
            elif action == "delete":
                await api.delete_list(token, entity_id)
                list_type = str(form.get("list_type", "")).strip() or "quotation"
                return _R("", status_code=204, headers={"HX-Redirect": f"/lists?type={list_type}"})
            elif action == "duplicate":
                result = await api.duplicate_list(token, entity_id)
                return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{result.get('id') or result.get('entity_id')}"})
            elif action == "convert-invoice":
                result = await api.convert_list(token, entity_id, "invoice")
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{result['target_doc_id']}"})
            elif action == "convert-memo":
                result = await api.convert_list(token, entity_id, "memo")
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{result['target_doc_id']}"})
            else:
                return _R("", status_code=400)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), hx_swap_oob="true", id="action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{entity_id}"})


def _doc_table(
    docs: list[dict],
    sort: str = "date",
    sort_dir: str = "desc",
    base_params: dict[str, str] | None = None,
    doc_type: str = "",
    lang: str = "en",
) -> FT:
    # Per-doc-type empty-state labels: (no_docs_key, create_btn_key)
    _EMPTY_STATE_KEYS: dict[str, tuple[str, str]] = {
        "invoice": ("label.no_documents_yet", "btn.new_invoice"),
        "memo": ("label.no_memos_yet", "btn.new_memo"),
        "bill": ("label.no_bills_yet", "btn.new_bill"),
        "purchase_order": ("label.no_purchase_orders_yet", "btn.new_purchase_order"),
        "consignment_in": ("label.no_consignment_in_yet", "btn.new_consignment_in"),
        "receipt": ("label.no_receipts_yet", "btn.new_receipt"),
        "credit_note": ("label.no_credit_notes_yet", "btn.new_credit_note"),
        "list": ("label.no_lists_yet", "btn.new_list"),
    }
    if not docs:
        dt_slug = doc_type if doc_type else "invoice"
        empty_keys = _EMPTY_STATE_KEYS.get(dt_slug, ("label.no_documents_yet", "btn.new_document"))
        return Div(
            empty_state_cta(t(empty_keys[0], lang), t(empty_keys[1], lang), f"/docs/create-blank?type={dt_slug}", hx_post=True),
            id="doc-table",
        )

    # Checkboxes for invoice/bill types (not lists, not quotations, not memos)
    show_checkboxes = doc_type in ("invoice", "bill")

    sort_keys = {
        "number": lambda d: str(d.get("doc_number") or d.get("ref") or ""),
        "type": lambda d: str(d.get("doc_type") or ""),
        "contact": lambda d: str(d.get("contact_name") or d.get("contact_id") or ""),
        "date": lambda d: str(d.get("issue_date") or d.get("created_at") or ""),
        "due": lambda d: str(d.get("due_date") or d.get("payment_due_date") or ""),
        "total": lambda d: float(d.get("total_amount") if d.get("total_amount") is not None else (d.get("total") or 0) or 0),
        "outstanding": lambda d: float(d.get("outstanding_balance") if d.get("outstanding_balance") is not None else (d.get("amount_outstanding") or 0) or 0),
        "status": lambda d: str(d.get("status") or ""),
    }
    key_fn = sort_keys.get(sort, sort_keys["date"])
    docs = sorted(docs, key=key_fn, reverse=(sort_dir == "desc"))

    def _th(label: str, key: str) -> FT:
        next_dir = "asc" if (sort == key and sort_dir == "desc") else "desc"
        marker = " ▲" if (sort == key and sort_dir == "asc") else (" ▼" if sort == key else "")

        params = dict(base_params or {})
        params["sort"] = key
        params["dir"] = next_dir
        href = f"/docs?{urlencode({k: v for k, v in params.items() if v not in (None, '', [])})}"
        return Th(A(f"{label}{marker}", href=href, cls="sort-link"))

    def _row(d: dict) -> FT:
        eid = d.get("entity_id") or d.get("id", "")
        doc_number = d.get("doc_number") or d.get("ref") or d.get("ref_id") or eid
        contact = d.get("contact_name") or d.get("contact_id") or d.get("contact_external_id")
        issue_date = d.get("issue_date") or d.get("created_at")
        due_date = d.get("due_date") or d.get("payment_due_date")
        total_amount = d.get("total_amount") if d.get("total_amount") is not None else d.get("total")
        outstanding_amount = d.get("outstanding_balance") if d.get("outstanding_balance") is not None else d.get("amount_outstanding")
        outstanding = float(outstanding_amount or 0)
        checkbox_td = [Td(Input(type="checkbox", cls="doc-row-select", value=eid,
                     data_contact_id=d.get("contact_id") or "",
                     data_outstanding=str(outstanding)),
                     cls="col-checkbox")] if show_checkboxes else []
        return Tr(
            *checkbox_td,
            Td(A(doc_number or EMPTY, href=f"/docs/{eid}", cls="table-link")),
            Td(format_value(d.get("doc_type"), "badge")),
            Td(format_value(contact)),
            Td(format_value(issue_date, "date")),
            Td(format_value(due_date, "date")),
            Td(format_value(total_amount, "money"), cls="cell--number"),
            Td(
                format_value(outstanding_amount, "money"),
                cls=f"cell--number {'cell--alert' if outstanding > 0 and d.get('doc_type') == 'invoice' else ''}",
            ),
            Td(format_value(d.get("status"), "badge")),
            id=f"doc-{eid}",
            cls="data-row",
        )

    checkbox_th = [Th(Input(type="checkbox", id="doc-select-all", title="Select all"), cls="col-checkbox")] if show_checkboxes else []

    # Bulk payment action bar (hidden by default, shown when checkboxes selected)
    bulk_bar = ""
    if show_checkboxes:
        bulk_bar = Div(
            Span(t("doc.0_selected"), id="doc-bulk-count", cls="bulk-count"),
            Button(t("btn.record_payment"), type="button", id="doc-bulk-pay-btn", cls="btn btn--primary btn--sm",
                   style="display:none;",
                   hx_get="/docs/bulk-payment-panel", hx_target="#bulk-payment-panel", hx_swap="innerHTML",
                   hx_include="this"),
            Div(id="bulk-payment-panel"),
            cls="bulk-action-bar", id="doc-bulk-bar",
        )

    bulk_js = ""
    if show_checkboxes:
        bulk_js = Script(f"""
(function() {{
    var table = document.getElementById('doc-table');
    if (!table) return;
    var selectAll = document.getElementById('doc-select-all');
    var countEl = document.getElementById('doc-bulk-count');
    var payBtn = document.getElementById('doc-bulk-pay-btn');
    function getSelected() {{
        return Array.from(table.querySelectorAll('.doc-row-select:checked'));
    }}
    function updateBar() {{
        var sel = getSelected();
        var n = sel.length;
        if (countEl) countEl.textContent = n + ' selected';
        if (payBtn) {{
            payBtn.style.display = n > 0 ? '' : 'none';
            // Build doc_ids param for HTMX request
            var ids = sel.map(cb => cb.value).join(',');
            payBtn.setAttribute('hx-vals', JSON.stringify({{doc_ids: ids}}));
            htmx.process(payBtn);
        }}
    }}
    if (selectAll) {{
        selectAll.addEventListener('change', function() {{
            table.querySelectorAll('.doc-row-select').forEach(cb => cb.checked = selectAll.checked);
            updateBar();
        }});
    }}
    table.addEventListener('change', function(e) {{
        if (e.target && e.target.classList.contains('doc-row-select')) updateBar();
    }});
}})();
""")

    return Div(
        bulk_bar,
        Table(
            Thead(Tr(
                *checkbox_th,
                _th("Number", "number"), _th("Type", "type"), _th("Contact", "contact"), _th("Date", "date"), _th("Due", "due"),
                _th("Total", "total"), _th("Outstanding", "outstanding"), _th("Status", "status"),
            )),
            Tbody(*[_row(d) for d in docs]),
            cls="data-table",
        ),
        bulk_js,
        id="doc-table",
    )


def _resolve_contact_display(doc: dict, field: str) -> str:
    """Resolve a contact field to its display name. DRY helper for all contact display contexts."""
    _NAME_MAP = {
        "contact_id": "contact_name",
        "commission_contact_id": "commission_contact_name",
    }
    name_field = _NAME_MAP.get(field)
    if name_field:
        name = doc.get(name_field)
        if name:
            return name
    raw = doc.get(field) or ""
    # If the raw value looks like an entity_id (contact:uuid), return "--" to signal unresolved
    if raw.startswith("contact:"):
        return "--"
    return raw


def _doc_display_cell(entity_id: str, field: str, value, doc_type: str = "") -> FT:
    _prefix = "/lists" if doc_type == "list" else "/docs"
    # Status is a state-machine field; transitions happen via lifecycle buttons only.
    if field == "status":
        return Div(
            format_value(value, "badge"),
            cls="editable-cell",
        )
    return Div(
        format_value(value, "badge" if field in {"purchase_kind"} else ("money" if field in {"total_amount", "tax_amount", "outstanding_balance"} else "date" if field in {"issue_date", "due_date"} else "text")),
        hx_get=f"{_prefix}/{entity_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        title="Click to edit",
        cls="editable-cell",
    )


def _tc_dropdown(entity_id: str, doc: dict, tc_templates: list[dict], doc_type: str, is_draft: bool) -> list:
    """Build T&C template dropdown + terms_text for a doc detail page."""
    # Filter templates by doc_type
    relevant = [tc for tc in tc_templates if doc_type in (tc.get("doc_types") or [])]
    current = doc.get("terms_template") or ""
    options = []
    for item in relevant:
        options.append(Option(item["name"], value=item["name"], selected=(item["name"] == current)))
    options.append(Option(t("label._add_new"), value="__add_new__"))
    settings_url = "/settings/sales?tab=terms-conditions" if doc_type not in ("purchase_order", "bill", "consignment_in") else "/settings/purchasing?tab=terms-conditions"
    # JS: if user picks __add_new__, navigate to settings
    select_js = f"if(this.value==='__add_new__'){{window.location.href='{settings_url}';return false;}}"
    return [
        Div(
            Div(t("doc.template"), cls="form-label"),
            Select(
                *options,
                name="value",
                hx_patch=f"/docs/{entity_id}/field/terms_template",
                hx_target="closest .doc-section",
                hx_swap="outerHTML",
                hx_trigger="change",
                cls="form-select",
                onchange=select_js,
            ) if is_draft else P(current or "--", cls="meta-value"),
            cls="form-group",
        ),
    ]


def _bank_account_options(bank_accounts: list[dict] | None, default_code: str | None = None) -> list:
    """Return Option elements for every active bank account.

    DRY: used in _payment_section (invoice/bill/credit-note forms) and the
    bulk-pay modal. Key names match _bank_to_dict in celerp-accounting routes:
    chart_account_code and bank_name.
    default_code: pre-selects the matching option (pass first account's code).
    """
    return [
        Option(
            f"{ba.get('chart_account_code', '')} - {ba.get('bank_name', '')}",
            value=ba.get("chart_account_code", ""),
            selected=(ba.get("chart_account_code") == default_code),
        )
        for ba in (bank_accounts or [])
    ]


def _payment_section(doc: dict, bank_accounts: list[dict] | None = None, is_manager: bool = True) -> FT:
    """Shared payment/credit section for invoices, bills, and credit notes.

    DRY: one function, different labels based on doc_type.
    Hidden for drafts, void docs, and non-payable doc types.
    """
    entity_id = doc.get("entity_id") or doc.get("id") or ""
    doc_type = doc.get("doc_type", "")
    status = doc.get("status", "draft")
    currency = doc.get("currency") or "USD"

    # Determine if this doc type should show payments
    _PAYABLE_TYPES = ("invoice", "bill", "credit_note")
    if doc_type not in _PAYABLE_TYPES:
        return Span()
    if status in ("draft", "void"):
        return Span()

    is_credit_note = doc_type == "credit_note"
    is_bill = doc_type == "bill"

    # Labels
    section_title = "Credit Application" if is_credit_note else "Payments"
    section_icon = "\U0001f4b3" if is_credit_note else "\U0001f4b0"
    add_label = "Apply to Invoice" if is_credit_note else ("Record Payment" if is_bill else "Receive Payment")

    payments = doc.get("payments") or []
    total_val = float(doc.get("total") or doc.get("total_amount") or 0)
    amount_paid = float(doc.get("amount_paid") or 0)
    outstanding = float(doc.get("amount_outstanding") or doc.get("outstanding_balance") or 0)

    from datetime import date as _d
    today = _d.today().isoformat()

    # --- Payment history table ---
    history_rows = []
    for i, p in enumerate(payments):
        voided = p.get("status") == "voided"
        p_date = p.get("payment_date") or p.get("recorded_at", "")[:10]
        p_method = p.get("method", "")
        p_bank = p.get("bank_account", "")
        p_ref = p.get("reference", "")
        p_amount = float(p.get("amount", 0))
        p_source = p.get("source_doc_id") or ""
        p_target = p.get("target_doc_id") or ""

        # For credit notes, show "Applied To" instead of bank
        if is_credit_note and p_target:
            link_col = Td(A(p_target.split(":")[-1][:12], href=f"/docs/{p_target}", cls="table-link"))
        elif p_method == "credit_note" and p_source:
            link_col = Td(A(f"CN {p_source.split(':')[-1][:12]}", href=f"/docs/{p_source}", cls="table-link"))
        else:
            link_col = Td(p_bank or EMPTY)

        row_cls = "data-row" + (" payment-voided" if voided else "")
        void_cell = Td("")
        if voided:
            void_reason = p.get("void_reason") or ""
            void_cell = Td(Span(t("doc.voided"), cls="badge badge--void", title=void_reason))
        elif not voided and is_manager:
            void_cell = Td(
                Details(
                    Summary("🗑", cls="btn btn--ghost btn--xs", title="Void this payment"),
                    Form(
                        Input(type="hidden", name="payment_index", value=str(i)),
                        Input(type="text", name="void_reason", placeholder="Reason...", cls="form-input form-input--sm"),
                        Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger btn--xs"),
                        hx_post=f"/docs/{entity_id}/void-payment", hx_swap="none",
                        cls="inline-form inline-form--compact",
                    ),
                    cls="void-inline",
                ),
            )
        else:
            void_cell = Td()

        history_rows.append(Tr(
            Td(format_value(p_date, "date")),
            Td(format_value(p_method, "badge")),
            link_col,
            Td(p_ref or EMPTY),
            Td(fmt_money(p_amount, currency), cls="cell--number"),
            void_cell,
            cls=row_cls,
        ))

    history_table = ""
    if history_rows:
        _hist_header = "Applied To" if is_credit_note else "Bank"
        history_table = Table(
            Thead(Tr(Th(t("th.date")), Th(t("label.method")), Th(_hist_header), Th(t("label.reference")), Th(t("label.amount")), Th(""))),
            Tbody(*history_rows),
            cls="data-table data-table--compact",
        )

    # --- Summary line ---
    if is_credit_note:
        summary_line = Div(
            Span(f"Credit Total: {fmt_money(total_val, currency)}", cls="total-label"),
            Span(f"Applied: {fmt_money(amount_paid, currency)}", cls="total-label"),
            Span(f"Remaining: {fmt_money(outstanding, currency)}",
                 cls="total-value" + (" total-value--alert" if outstanding > 0 else " total-value--success")),
            cls="payment-summary",
        )
    else:
        paid_label = f"Total Paid: {fmt_money(amount_paid, currency)} / {fmt_money(total_val, currency)}"
        outstanding_label = "Paid in Full" if outstanding <= 0.005 else f"Outstanding: {fmt_money(outstanding, currency)}"
        outstanding_cls = "total-value--success" if outstanding <= 0.005 else "total-value--alert"
        summary_line = Div(
            Span(paid_label, cls="total-label"),
            Span(outstanding_label, cls=f"total-value {outstanding_cls}"),
            cls="payment-summary",
        )

    # --- Add Payment / Apply Credit form ---
    # Only show form if there's outstanding balance
    add_form = ""
    if outstanding > 0.005:
        _methods = [Option(t("doc.cash"), value="cash"), Option(t("doc.bank_transfer"), value="transfer"),
                    Option(t("doc.card"), value="card"), Option(t("doc.check"), value="check"), Option(t("doc.other"), value="other")]
        _bank_opts = _bank_account_options(bank_accounts, default_code=bank_accounts[0].get("chart_account_code") if bank_accounts else None)

        if is_credit_note:
            add_form = Div(
                # Apply to Invoice form
                Div(
                    H4(t("page.apply_to_invoice"), cls="form-subtitle"),
                    Form(
                        Div(
                            Div(Label(t("label.invoice"), cls="form-label"),
                                Select(
                                    Option(t("doc.loading_invoices"), value=""),
                                    name="target_doc_id", cls="form-input", id="cn-invoice-picker",
                                ), cls="form-group"),
                            Div(Label(t("label.amount"), cls="form-label"),
                                Input(type="number", name="amount", value=f"{outstanding:.2f}",
                                      step="0.01", min="0", cls="form-input", id="cn-apply-amount"), cls="form-group"),
                            Div(Label(t("th.date"), cls="form-label"),
                                Input(type="date", name="date", value=today, cls="form-input"), cls="form-group"),
                            cls="form-row",
                        ),
                        Span("", id="payment-error"),
                        Button(t("btn.apply"), type="submit", cls="btn btn--primary btn--sm"),
                        hx_post=f"/docs/{entity_id}/apply-credit",
                        hx_target="#payment-error",
                        hx_swap="innerHTML",
                        cls="form-card",
                    ),
                    cls="payment-form-section",
                ),
                # Refund to Customer form
                Div(
                    H4(t("page.refund_to_customer"), cls="form-subtitle"),
                    Form(
                        Div(
                            Div(Label(t("label.amount"), cls="form-label"),
                                Input(type="number", name="amount", value=f"{outstanding:.2f}",
                                      step="0.01", min="0", cls="form-input"), cls="form-group"),
                            Div(Label(t("th.date"), cls="form-label"),
                                Input(type="date", name="date", value=today, cls="form-input"), cls="form-group"),
                            Div(Label(t("label.method"), cls="form-label"),
                                Select(*_methods, name="method", cls="form-input"), cls="form-group"),
                            Div(Label(t("label.bank_account"), cls="form-label"),
                                Select(*_bank_opts, name="bank_account", cls="form-input"), cls="form-group"),
                            Div(Label(t("label.conversion_rate"), cls="form-label"),
                                Input(type="number", name="conversion_rate", value="1.0000",
                                      step="0.0001", min="0.0001", cls="form-input"),
                                P(t("doc.rate_at_which_refund_was_issued_10_if_no_conversio"),
                                  cls="form-hint"),
                                cls="form-group"),
                            Div(Label(t("label.reference"), cls="form-label"),
                                Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                            cls="form-row",
                        ),
                        Span("", id="payment-error"),
                        Button(t("btn.refund"), type="submit", cls="btn btn--secondary btn--sm"),
                        hx_post=f"/docs/{entity_id}/refund-credit", hx_swap="none", cls="form-card",
                    ),
                    cls="payment-form-section",
                ),
                # JS to populate invoice picker
                Script(f"""
(function() {{
    fetch('/docs/{entity_id}/open-invoices')
        .then(r => r.json())
        .then(invoices => {{
            const sel = document.getElementById('cn-invoice-picker');
            if (!sel) return;
            sel.innerHTML = '<option value="">-- Select Invoice --</option>';
            invoices.forEach(inv => {{
                const opt = document.createElement('option');
                opt.value = inv.id;
                opt.textContent = inv.doc_number + ' — ' + inv.contact_name + ' — Outstanding: ' + inv.outstanding.toFixed(2);
                sel.appendChild(opt);
            }});
            sel.addEventListener('change', function() {{
                const inv = invoices.find(i => i.id === sel.value);
                if (inv) {{
                    const amtEl = document.getElementById('cn-apply-amount');
                    if (amtEl) amtEl.value = Math.min({outstanding}, inv.outstanding).toFixed(2);
                }}
            }});
        }})
        .catch(() => {{}});
}})();
"""),
            )
        else:
            # Standard payment form for invoices and bills
            add_form = Div(
                H4(add_label, cls="form-subtitle"),
                Form(
                    Div(
                        Div(Label(t("label.amount"), cls="form-label"),
                            Input(type="number", name="amount", value=f"{outstanding:.2f}",
                                  step="0.01", min="0", cls="form-input"), cls="form-group"),
                        Div(Label(t("th.date"), cls="form-label"),
                            Input(type="date", name="payment_date", value=today, cls="form-input"), cls="form-group"),
                        Div(Label(t("label.method"), cls="form-label"),
                            Select(*_methods, name="method", cls="form-input"), cls="form-group"),
                        Div(Label(t("label.bank_account"), cls="form-label"),
                            Select(*_bank_opts, name="bank_account", cls="form-input"), cls="form-group"),
                        Div(Label(t("label.conversion_rate"), cls="form-label"),
                            Input(type="number", name="conversion_rate", value="1.0000",
                                  step="0.0001", min="0.0001", cls="form-input"),
                            P(t("doc.rate_at_which_payment_was_received_10_if_no_conver"),
                              cls="form-hint"),
                            cls="form-group"),
                        Div(Label(t("label.reference"), cls="form-label"),
                            Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                        cls="form-row",
                    ),
                    Span("", id="payment-error"),
                    Button(t("btn.save_payment"), type="submit", cls="btn btn--primary btn--sm"),
                    hx_post=f"/docs/{entity_id}/payment", hx_swap="none", cls="form-card",
                ),
                cls="payment-form-section",
            )

    return Div(
        Div(Span(section_icon, cls="section-icon"), H3(section_title, cls="section-title"), cls="section-header"),
        history_table,
        summary_line,
        add_form,
        cls="doc-section payment-section",
    )


def _company_address_picker(doc_id: str, current_address: str, company_locations: list) -> FT:
    """Render address as a location picker dropdown if locations exist, else a plain editable cell."""
    if not company_locations:
        # Fallback: plain editable cell (no locations configured)
        display = current_address or "--"
        return _doc_display_cell(doc_id, "company_address", display)

    def _addr_text(loc: dict) -> str:
        return unwrap_address(loc.get("address")) or loc.get("name") or ""

    options = [Option("-- select address --", value="", selected=(not current_address))]
    for loc in company_locations:
        addr_text = _addr_text(loc)
        options.append(Option(
            loc.get("name") or addr_text,
            value=addr_text,
            selected=(addr_text == current_address),
        ))
    # Free-text option if current_address doesn't match any location
    known = {_addr_text(l) for l in company_locations}
    if current_address and current_address not in known and current_address != "--":
        options.append(Option(f"Custom: {current_address[:40]}", value=current_address, selected=True))

    return Div(
        Select(
            *options,
            name="value",
            hx_patch=f"/docs/{doc_id}/field/company_address",
            hx_target="closest .editable-cell",
            hx_swap="outerHTML",
            hx_trigger="change",
            cls="cell-input cell-input--select",
        ),
        cls="editable-cell editable-cell--editing",
    )



def _li_bulk_toolbar(entity_id: str, is_list: bool, labels_only: bool = False) -> FT:
    """Bulk action toolbar for line items. Hidden until JS detects 1+ checked rows.
    labels_only=True: finalized docs - only Print Labels action, no delete.
    Two-stage: select action → confirm button appears. Print Labels only shown when
    celerp-labels is installed (slot-driven, DRY)."""
    from celerp.modules.slots import get as get_slot
    labels_action = next(
        (a for a in get_slot("bulk_action") if a.get("_module") == "celerp-labels"),
        None,
    )
    if not labels_only:
        options = [
            Option(t("doc.action"), value="", disabled=True, selected=True),
            Option(t("btn.delete_selected"), value="li-delete"),
        ]
        if labels_action:
            options.append(Option(t("doc.print_labels"), value="mod:labels_print-bulk"))
    else:
        options = [
            Option(t("doc.action"), value="", disabled=True, selected=True),
            Option(t("doc.print_labels"), value="mod:labels_print-bulk"),
        ]
    children = [
        Span(t("doc.0_rows_selected"), id="li-bulk-count", cls="bulk-count"),
        Select(*options, id="li-bulk-select", cls="form-input form-input--sm",
               onchange="liBulkActionSelected(this.value)"),
    ]
    if not labels_only:
        children.append(
            Button(t("btn.delete_selected"), type="button", id="li-bulk-delete-btn",
                   cls="btn btn--danger btn--sm", style="display:none",
                   onclick="liBulkDeleteConfirmed()"),
        )
    children += [
        Button(t("doc.print_labels"), type="button", id="li-bulk-labels-btn",
               cls="btn btn--secondary btn--sm", style="display:none",
               onclick="liBulkLabelsConfirmed()"),
        Div(id="li-bulk-context"),
    ]
    return Div(
        *children,
        id="li-bulk-toolbar",
        cls="bulk-toolbar",
        style="display:none",
    )


def _doc_detail(doc: dict, locations: list | None = None, ledger: list | None = None, price_lists: list | None = None, tc_templates: list | None = None, tz: str = "UTC", company_taxes: list | None = None, bank_accounts: list | None = None, company_locations: list | None = None, role: str = "owner", item_categories: list | None = None, notes: list | None = None, company_currency: str = "USD", suppress_doc_actions: bool = False, extra_left_actions: list | None = None, extra_right_actions: list | None = None, suppress_pdf: bool = False, relay_connected: bool = False) -> FT:
    def _pick(*keys: str):
        for k in keys:
            if k in doc and doc.get(k) is not None:
                return doc.get(k)
        return None

    line_items = doc.get("line_items", [])
    entity_id = doc.get("entity_id") or doc.get("id") or ""
    status = doc.get("status", "draft")
    doc_type = doc.get("doc_type", "")
    is_draft = status == "draft"
    ref = _pick("ref_id", "doc_number", "ref", "external_id") or entity_id
    from celerp.services.auth import ROLE_LEVELS as _RL
    _user_level = _RL.get(role, _RL["owner"])
    _is_manager = _user_level >= _RL["manager"]
    _is_operator = _user_level >= _RL["operator"]

    def _cell(field: str, value) -> FT:
        """Editable display cell, routing to the correct /docs/ or /lists/ URL."""
        return _doc_display_cell(entity_id, field, value, doc_type)

    contact_value = _resolve_contact_display(doc, "contact_id")
    issue_date_value = _pick("issue_date", "created_at")
    due_date_value = _pick("due_date", "payment_due_date")
    total_value = _pick("total_amount", "total")
    tax_value = _pick("tax_amount", "tax")
    outstanding_value = _pick("outstanding_balance", "amount_outstanding")
    subtotal_value = _pick("subtotal")
    discount_value = _pick("discount_amount") or 0
    currency = doc.get("currency") or "USD"
    is_list = doc_type == "list"
    # Lists use /lists/ endpoints; docs use /docs/
    _base = f"/lists/{entity_id}" if is_list else f"/docs/{entity_id}"

    # --- List type selector (shown above action buttons for lists) ---
    list_type_selector = ""
    if is_list:
        _current_lt = doc.get("list_type") or "quotation"
        if is_draft:
            list_type_selector = Div(
                Span(t("doc.list_type"), cls="meta-label"),
                Select(
                    *[Option(lt.replace("_", " ").title(), value=lt, selected=(lt == _current_lt)) for lt in _LIST_TYPES],
                    name="value",
                    hx_patch=f"/lists/{entity_id}/field/list_type",
                    hx_swap="none",
                    cls="form-select",
                ),
                cls="list-type-bar",
                style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.75rem;",
            )
        else:
            list_type_selector = Div(
                Span(t("doc.list_type"), cls="meta-label"),
                Span(_current_lt.replace("_", " ").title(), cls=f"badge badge--{_current_lt}"),
                cls="list-type-bar",
                style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.75rem;",
            )

    # --- Action buttons ---
    # action_btns_left:  primary workflow actions (left-aligned)
    # action_btns_right: destructive/void/delete (right side, before print group)
    # action_btns_print: PDF + CSV export/import – always far-right, preceded by "|" separator
    action_btns_left = []
    action_btns_right = []
    action_btns_print = []
    if doc_type == "invoice" and status in ("partial", "paid"):
        action_btns_left.append(
            Button(t("btn.create_credit_note"),
                hx_post=f"/docs/{entity_id}/action/create-credit-note",
                hx_swap="none",
                cls="btn btn--secondary btn--sm",
                title="Create a credit note from this invoice. The CN will be pre-populated with this invoice's line items.",
            )
        )
    if doc_type == "invoice" and status in ("sent", "final", "partial", "awaiting_payment"):
        pass  # Payment section is now a separate component rendered below
    if doc_type == "quotation" and status not in ("void", "converted"):
        action_btns_left.append(
            Button(t("btn.convert"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--primary")
        )
    # Issued memos can be converted to invoices (customer keeps goods)
    if doc_type == "memo" and status in ("final", "sent", "received", "partially_received"):
        action_btns_left.append(
            Button(t("btn.convert"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--secondary")
        )
    # Issued consignment_in can be converted to vendor bills (vendor keeps goods)
    if doc_type == "consignment_in" and status in ("final", "sent", "received", "partially_received"):
        action_btns_left.append(
            Button(t("btn.convert_to_vendor_bill"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--secondary")
        )
    # List-specific lifecycle buttons
    if is_list:
        if status == "draft":
            action_btns_left.append(Button(t("btn.send"), hx_post=f"/lists/{entity_id}/action/send", hx_swap="none", cls="btn btn--primary"))
        if status == "sent":
            action_btns_left.append(Button(t("btn.accept"), hx_post=f"/lists/{entity_id}/action/accept", hx_swap="none", cls="btn btn--primary"))
        if status == "accepted":
            action_btns_left.append(Button(t("btn.complete"), hx_post=f"/lists/{entity_id}/action/complete", hx_swap="none", cls="btn btn--primary"))
        if status not in ("void", "converted"):
            action_btns_left.append(Button(t("btn.convert"), hx_post=f"/lists/{entity_id}/action/convert-invoice", hx_swap="none", cls="btn btn--secondary"))
            action_btns_left.append(Button(t("btn.convert_to_memo"), hx_post=f"/lists/{entity_id}/action/convert-memo", hx_swap="none", cls="btn btn--secondary"))
        action_btns_left.append(Button(t("btn.duplicate"), hx_post=f"/lists/{entity_id}/action/duplicate", hx_swap="none", cls="btn btn--secondary"))
    if status in ("draft", "sent") and not is_list:
        _finalize_labels = {
            "invoice": "Issue Invoice",
            "purchase_order": "Convert to Bill",
            "memo": "Issue Memo",
            "consignment_in": "Issue Consignment In",
            "credit_note": "Issue Credit Note",
            "receipt": "Issue Receipt",
        }
        finalize_label = _finalize_labels.get(doc_type, "Finalize")
        if _is_manager and not suppress_doc_actions:
            action_btns_left.append(
                Button(finalize_label,
                       onclick=f"event.preventDefault();(async()=>{{await _celerpPersist();htmx.ajax('POST','/docs/{entity_id}/action/finalize',{{swap:'none'}});}})();",
                       cls="btn btn--primary")
            )
    if status not in ("void", "draft") and _is_manager and not suppress_doc_actions:
        action_btns_right.append(
            Details(
                Summary(t("btn.void"), cls="btn btn--danger"),
                Form(
                    Input(type="text", name="reason", placeholder="Void reason...", cls="form-input form-input--inline"),
                    Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger"),
                    hx_post=f"{_base}/action/void", hx_swap="none", cls="inline-form",
                ),
                cls="void-section",
            )
        )
    # "Revert to Draft" button - only from final/sent with no payments and no received items
    amount_paid_for_revert = float(doc.get("amount_paid") or 0)
    has_received_items = bool(doc.get("received_items"))
    if status in ("final", "sent") and amount_paid_for_revert == 0 and not has_received_items and _is_manager and not suppress_doc_actions:
        action_btns_right.append(
            Details(
                Summary(t("doc.revert_to_draft"), cls="btn btn--secondary"),
                Form(
                    Input(type="text", name="reason", placeholder="Reason (optional)...", cls="form-input form-input--inline"),
                    Button(t("btn.confirm_revert"), type="submit", cls="btn btn--secondary"),
                    hx_post=f"{_base}/action/revert_to_draft", hx_swap="none", cls="inline-form",
                ),
                cls="void-section",
            )
        )
    # "Unvoid" button - only from void with pre_void_status set
    if status == "void" and doc.get("pre_void_status") and _is_manager and not suppress_doc_actions:
        action_btns_right.append(
            Details(
                Summary(t("doc.unvoid"), cls="btn btn--secondary"),
                Form(
                    P(f"Restore to '{doc['pre_void_status']}' status?", cls="text-muted"),
                    Button(t("btn.confirm_unvoid"), type="submit", cls="btn btn--secondary"),
                    hx_post=f"{_base}/action/unvoid", hx_swap="none", cls="inline-form",
                ),
                cls="void-section",
            )
        )
    if status == "draft" and _is_manager:
        action_btns_right.append(
            Details(
                Summary(t("btn.delete"), cls="btn btn--danger"),
                Form(
                    Input(type="hidden", name="doc_type", value=doc_type),
                    P(t("doc.permanently_delete_this_draft_this_cannot_be_undon"), cls="text-muted"),
                    Button(t("btn.confirm_delete"), type="submit", cls="btn btn--danger"),
                    hx_post=f"{_base}/action/delete", hx_swap="none", cls="inline-form",
                ),
                cls="void-section",
            )
        )
    # Refund is now handled via credit notes + void in the payment section
    # Send (relay modal) + Mark as Sent - hidden for internal/receiving doc types (bill, consignment_in)
    from celerp_docs.doc_constants import NO_SEND_DOC_TYPES, NO_SEND_STATUSES
    _can_send = (
        not is_list
        and not suppress_doc_actions
        and doc_type not in NO_SEND_DOC_TYPES
    )
    if _can_send:
        # Send via relay - modal popup, only when relay connected and status allows it
        if relay_connected and status not in NO_SEND_STATUSES:
            contact_email = doc.get("contact_email") or ""
            doc_number = doc.get("ref_id") or doc.get("doc_number") or ""
            company_name = doc.get("company_name") or "Your Company"
            _type_label_send = doc_type.replace("_", " ").title()
            default_subject = f"{_type_label_send} #{doc_number} from {company_name}" if doc_number else ""
            default_body = f"Please find attached {_type_label_send} #{doc_number}." if doc_number else ""
            modal_id = f"send-modal-{entity_id.replace(':', '-')}"
            action_btns_left.append(
                Button(t("btn.send"), type="button",
                       onclick=f"document.getElementById('{modal_id}').showModal()",
                       cls="btn btn--secondary"),
            )
            # Dialog rendered at the bottom of the page via extra content - inject as sibling
            # We append it to the page by including it in the actions area; dialog CSS positions it correctly
            action_btns_left.append(
                Dialog(
                    Div(
                        H3(t("btn.send"), cls="modal-dialog__title"),
                        Button("✕", type="button",
                               onclick=f"document.getElementById('{modal_id}').close()",
                               cls="modal-dialog__close", aria_label="Close"),
                        cls="modal-dialog__header",
                    ),
                    Form(
                        Div(
                            Label(t("label.to_email"), cls="form-label"),
                            Input(type="text", name="sent_to", value=contact_email,
                                  placeholder="recipient@example.com", cls="form-input"),
                            P(t("doc.send_multiple_hint"), cls="form-hint"),
                            cls="form-group",
                        ),
                        Div(
                            Label("CC", cls="form-label"),
                            Input(type="text", name="cc", placeholder="cc@example.com", cls="form-input"),
                            cls="form-group",
                        ),
                        Div(
                            Label("BCC", cls="form-label"),
                            Input(type="text", name="bcc", placeholder="bcc@example.com", cls="form-input"),
                            cls="form-group",
                        ),
                        Div(
                            Label(t("label.subject"), cls="form-label"),
                            Input(type="text", name="subject", value=default_subject, cls="form-input"),
                            cls="form-group",
                        ),
                        Div(
                            Label(t("label.message"), cls="form-label"),
                            Textarea(default_body, name="message", rows="4", cls="form-input"),
                            cls="form-group",
                        ),
                        Div(
                            Button(t("btn.cancel"), type="button",
                                   onclick=f"document.getElementById('{modal_id}').close()",
                                   cls="btn btn--ghost"),
                            Button(t("btn.send_document"), type="submit", cls="btn btn--primary"),
                            cls="modal-dialog__actions",
                        ),
                        hx_post=f"/docs/{entity_id}/action/send",
                        hx_swap="none",
                        hx_on__htmx_after_request=f"if(event.detail.successful)document.getElementById('{modal_id}').close()",
                    ),
                    id=modal_id,
                    cls="modal-dialog",
                )
            )
        # Mark as Sent (manual, no relay needed) - only on draft
        if status == "draft":
            action_btns_left.append(
                Button(t("btn.mark_as_sent"), hx_post=f"/docs/{entity_id}/action/mark_sent",
                       hx_swap="none", cls="btn btn--secondary")
            )
        # Unmark Sent - only when status == "sent"
        if status == "sent":
            action_btns_left.append(
                Button(t("btn.unmark_sent"), hx_post=f"/docs/{entity_id}/action/unmark_sent",
                       hx_swap="none", cls="btn btn--secondary")
            )
    # PDF + CSV buttons → print group (hidden entirely when suppress_pdf is set)
    if not suppress_pdf:
        _print_href = f"/lists/{entity_id}/print" if is_list else f"/docs/{entity_id}/print"
        action_btns_print.append(A(NotStr(_ICON_PRINT), href=_print_href, target="_blank", cls="btn btn--ghost btn--icon", title=t("btn.print")))
        # CSV line items export/import icons
        action_btns_print.append(
            A(NotStr(_ICON_CSV_EXPORT), href=f"{_base}/items/csv",
              cls="btn btn--ghost btn--icon", title=t("doc.export_line_items_csv")),
        )
        if is_draft:
            action_btns_print.append(
                Button(NotStr(_ICON_CSV_IMPORT), type="button",
                       cls="btn btn--ghost btn--icon", title=t("doc.import_line_items_csv"),
                       onclick="document.getElementById('csv-import-input').click()"),
            )
    action_btns_print.append(Span("", id="share-result"))
    action_btns_print.append(Span("", id="action-error"))

    # --- Slot: doc_detail_actions (module-contributed action buttons - go left) ---
    from celerp.modules.slots import get as _get_slot
    for _contrib in _get_slot("doc_detail_actions"):
        _render_path = _contrib.get("render", "")
        if _render_path:
            try:
                _mod_path, _fn_name = _render_path.rsplit(":", 1)
                import importlib as _il
                _render_fn = getattr(_il.import_module(_mod_path), _fn_name)
                _el = _render_fn(doc)
                if _el is not None:
                    action_btns_left.append(_el)
            except Exception:
                pass

    # --- Inventory section action buttons (rendered above line items, not in the top bar) ---
    _fulfill_el = _render_fulfill_section(doc)
    _receive_return_el = _render_receive_return_section(doc)
    _receive_goods_el = _render_receive_goods_section(doc)

    # --- Slot: doc_detail_badges (module-contributed status badges) ---
    _slot_badges = []
    _fulfill_badge = _render_fulfillment_badge(doc)
    if _fulfill_badge is not None:
        _slot_badges.append(_fulfill_badge)
    for _contrib in _get_slot("doc_detail_badges"):
        _render_path = _contrib.get("render", "")
        if _render_path:
            try:
                _mod_path, _fn_name = _render_path.rsplit(":", 1)
                import importlib as _il
                _render_fn = getattr(_il.import_module(_mod_path), _fn_name)
                _el = _render_fn(doc)
                if _el is not None:
                    _slot_badges.append(_el)
            except Exception:
                pass

    # --- PO/consignment_in receive (per-line form); bills use _receive_goods_el above ---
    po_receive_section = ""
    if doc_type in ("purchase_order", "consignment_in") and status in ("awaiting_payment", "finalized", "sent", "final", "partially_received"):
        po_items = doc.get("line_items", [])
        if po_items:
            receive_rows = []
            for i, li in enumerate(po_items):
                qty_ordered = float(li.get("quantity", 0) or 0)
                qty_received = float(li.get("quantity_received", 0) or 0)
                qty_remaining = max(0, qty_ordered - qty_received)
                desc = str(li.get("description", "") or li.get("sku", "") or f"Item {i + 1}")
                receive_rows.append(Tr(
                    Td(desc),
                    Td(str(qty_ordered)),
                    Td(str(qty_received)),
                    Td(
                        Input(type="hidden", name=f"item_id_{i}", value=li.get("item_id", "") or ""),
                        Input(type="number", name=f"qty_{i}", value=str(qty_remaining),
                              step="any", min="0", max=str(qty_remaining),
                              cls="form-input form-input--sm"),
                    ) if qty_remaining > 0 else Td(Span(t("doc.fully_received"), cls="badge badge--green")),
                ))
            loc_opts = [Option(loc.get("name", ""), value=loc.get("name", "")) for loc in (locations or [])]
            po_receive_section = Details(
                Summary(t("doc.receive_goods"), cls="btn btn--secondary"),
                Form(
                    Table(
                        Thead(Tr(Th(t("th.item")), Th(t("th.ordered")), Th(t("doc.received")), Th(t("th.qty_to_receive")))),
                        Tbody(*receive_rows),
                        cls="data-table data-table--compact",
                    ),
                    Div(Label(t("th.location"), cls="form-label"),
                        Select(*loc_opts, name="location_name", cls="form-input") if loc_opts else
                        Input(type="text", name="location_name", placeholder="Location", cls="form-input"),
                        cls="form-group"),
                    Div(Label(t("th.notes"), cls="form-label"),
                        Textarea("", name="notes", rows="2", cls="form-input"), cls="form-group"),
                    Span("", id="action-error"),
                    Button(t("btn.record_receipt"), type="submit", cls="btn btn--primary"),
                    hx_post=f"/docs/{entity_id}/receive", hx_swap="none", cls="form-card",
                ),
                cls="receive-section",
            )

    # --- Price list bar (positioned in line items section) ---
    _pl_names = [pl.get("name", "") for pl in (price_lists or []) if pl.get("name")]
    _current_pl = doc.get("price_list") or ""
    if doc_type in ("purchase_order", "bill"):
        _pl_bar = ""  # Vendor docs use cost price, not price lists
    elif is_draft and _pl_names:
        _pl_select = Select(
            *[Option(name, value=name, selected=(name == _current_pl)) for name in _pl_names],
            id="doc-price-list",
            cls="cell-input cell-input--select",
            onchange=f"celerpReprice(this.value)",
        )
        _pl_bar = Div(
            Span(t("doc.price_list"), cls="meta-label"),
            _pl_select,
            cls="price-list-bar",
            style="display:flex;align-items:center;gap:0.5rem;justify-content:flex-end;max-width:250px;margin-left:auto;margin-bottom:0.5rem;",
        )
    else:
        _pl_bar = Div(
            Span(t("doc.price_list"), cls="meta-label"),
            Span(_current_pl or "-", cls="meta-value"),
            cls="price-list-bar",
            style="display:flex;align-items:center;gap:0.5rem;justify-content:flex-end;max-width:250px;margin-left:auto;margin-bottom:0.5rem;",
        ) if _current_pl else ""

    # --- Line items section ---
    line_body_id = "line-body"
    if is_draft:
        def _sku_input(val: str = "", entity_id: str = "") -> FT:
            eye_cls = "item-link item-link--active" if entity_id else "item-link item-link--inactive"
            eye_href = f"/inventory/{entity_id}" if entity_id else "#"
            eye = A("👁", href=eye_href, target="_blank" if entity_id else "",
                     cls=eye_cls, data_name="item_link",
                     title="View item details" if entity_id else "No linked item",
                     onclick="" if entity_id else "event.preventDefault();")
            return Div(
                eye,
                Input(type="text", value=val, data_name="sku", placeholder="SKU...",
                      cls="cell-input cell-input--sm catalog-ac-input",
                      autocomplete="off",
                      title="Type to search catalog or enter custom description",
                      oninput="celerpAcSearch(this,'sku')",
                      onblur="celerpAcBlur(this)",
                      onkeydown="celerpAcKey(event,this)"),
                Div(cls="catalog-ac-list", style="display:none"),
                cls="catalog-ac-wrap",
            )

        def _desc_input(val: str = "") -> FT:
            return Div(
                Input(type="text", value=val, data_name="description", placeholder="Description…",
                      cls="cell-input cell-input--sm catalog-ac-input",
                      autocomplete="off",
                      title="Type to search catalog or enter custom description",
                      oninput="celerpAcSearch(this,'description')",
                      onblur="celerpAcBlur(this)",
                      onkeydown="celerpAcKey(event,this)"),
                Div(cls="catalog-ac-list", style="display:none"),
                cls="catalog-ac-wrap",
            )

        import json as _json
        _taxes_list = company_taxes or []
        _default_tax = next((tax for tax in _taxes_list if tax.get("is_default")), None)
        _default_tax_value = f"{_default_tax.get('name', '')}|{float(_default_tax.get('rate', 0))}" if _default_tax else "|0"

        def _tax_select(current_rate: float = 0.0, current_code: str = "", current_label: str = "") -> FT:
            """Build tax <select> + hidden custom-rate input + hidden label for a line item."""
            # Determine selected value: match by code first, then by rate
            selected_val = "|0"
            is_custom = False
            for tax in _taxes_list:
                tcode = tax.get("name", "")
                trate = float(tax.get("rate", 0))
                if current_code and tcode == current_code:
                    selected_val = f"{tcode}|{trate}"
                    break
                if not current_code and trate == current_rate and current_rate != 0:
                    selected_val = f"{tcode}|{trate}"
                    break
            else:
                if current_rate != 0 and not any(float(tax.get("rate", 0)) == current_rate for tax in _taxes_list):
                    selected_val = "|custom"
                    is_custom = True

            options = [Option(t("doc.no_tax"), value="|0", selected=(selected_val == "|0"))]
            for tax in _taxes_list:
                tcode = tax.get("name", "")
                trate = float(tax.get("rate", 0))
                val = f"{tcode}|{trate}"
                options.append(Option(f"{tcode} ({trate}%)", value=val, selected=(selected_val == val)))
            options.append(Option(t("doc.custom"), value="|custom", selected=is_custom))

            custom_input = Input(
                type="number", value=str(current_rate) if is_custom else "0",
                step="0.01", data_name="tax_rate_custom",
                cls="cell-input cell-input--xs",
                style=("display:inline-block;" if is_custom else "display:none;"),
            )
            return Div(
                Select(*options, data_name="tax_select",
                       cls="cell-input cell-input--select cell-input--xs",
                       onchange="celerpTaxChange(this)",
                       onblur="celerpAutoSave()"),
                custom_input,
                Input(type="hidden", value=current_label, data_name="tax_label"),
                style="display:flex;gap:2px;align-items:center;",
            )

        def _li_editable_row(li: dict, idx: int) -> FT:
            qty = li.get("quantity", 0)
            price = li.get("unit_price", 0)
            discount_pct = float(li.get("discount_pct") or 0)
            discounted = float(qty or 0) * float(price or 0) * (1 - discount_pct / 100)
            line_tot = discounted
            li_entity_id = li.get("entity_id") or li.get("item_id") or ""
            li_allow_splitting = "1" if li.get("allow_splitting") else ""
            account_cell = Td(Input(type="text", value=li.get("account_code", "") or "",
                         data_name="account_code", placeholder="e.g. 1130",
                         cls="cell-input cell-input--xs",
                         onblur="celerpAutoSave()")) if doc_type in ("purchase_order", "bill") else None

            _show_category = doc_type in ("bill", "purchase_order", "consignment_in")
            _show_receive_as = doc_type in ("bill", "purchase_order", "consignment_in")
            _cats = item_categories or []
            if _show_category and is_draft:
                _cat_val = li.get("category") or ""
                _cat_options = [Option("", value="")]
                for c in _cats:
                    _cat_options.append(Option(c, value=c, selected=(c == _cat_val)))
                _cat_options.append(Option(t("label._add_new"), value="__add_new__"))
                category_cell = Td(Select(
                    *_cat_options,
                    data_name="category",
                    cls="cell-input cell-input--select cell-input--xs",
                    onchange="if(this.value==='__add_new__'){window.open('/settings/inventory?tab=category-library','_blank');this.value='';}else{celerpAutoSave();}",
                ))
            elif _show_category:
                category_cell = Td(li.get("category") or "--")
            else:
                category_cell = None

            if _show_receive_as and is_draft:
                _ra_val = li.get("receive_as", "stock")
                receive_as_cell = Td(Select(
                    Option(t("doc.stock"), value="stock", selected=(_ra_val == "stock")),
                    Option(t("doc.expense"), value="expense", selected=(_ra_val == "expense")),
                    data_name="receive_as",
                    cls="cell-input cell-input--select cell-input--xs",
                    onchange="celerpAutoSave()",
                ))
            elif _show_receive_as:
                receive_as_cell = Td(li.get("receive_as", "stock").capitalize())
            else:
                receive_as_cell = None

            cells = [
                Td(Input(type="checkbox", cls="li-select", value=li_entity_id or ""), cls="col-checkbox li-checkbox-cell"),
                Td(_sku_input(li.get("sku", "") or "", li_entity_id), cls="col-sku"),
                Td(_desc_input(li.get("description", "") or li.get("name", "")), cls="col-desc"),
            ]
            if category_cell:
                cells.append(category_cell)
            if receive_as_cell:
                cells.append(receive_as_cell)
            cells.extend([
                Td(Input(type="number", value=str(qty), step="any",
                         data_name="quantity", oninput="celerpUpdateTotals()",
                         onblur="celerpQtyBlur(this); celerpAutoSave()",
                         cls="cell-input cell-input--xs"), cls="col-qty"),
                Td(Span(li.get("unit", "") or "", data_name="unit", cls="meta-value meta-value--muted",
                         style="font-size:12px;display:inline-block;min-width:40px;"), cls="col-unit"),
                Td(Input(type="number", value=str(price), step="0.01",
                         data_name="unit_price", oninput="celerpUpdateTotals()",
                         onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs"), cls="col-unit-price"),
                Td(Input(type="number", value=str(discount_pct) if discount_pct else "0", step="0.01",
                         data_name="discount_pct", oninput="celerpUpdateTotals()",
                         onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs"), cls="col-disc"),
                Td(_tax_select(float(li.get("tax_rate", 0) or 0), li.get("tax_code", "") or "",
                              ((li.get("taxes") or [{}])[0].get("label", "") if li.get("taxes") else "")), cls="col-tax"),
            ])
            if account_cell:
                cells.append(account_cell)
            cells.extend([
                Td(Input(type="number", value=str(round(line_tot, 2)), step="0.01",
                         cls="cell-input line-total",
                         oninput="celerpLineTotalInput(this)",
                         onblur="celerpAutoSave()",
                         data_name="line_total"),
                   Input(type="hidden", value=li.get("hs_code", "") or "", data_name="hs_code"),
                   Input(type="hidden", value=li_entity_id, data_name="entity_id"),
                   Input(type="hidden", value=li_allow_splitting, data_name="allow_splitting"),
                   Input(type="hidden", value=str(li.get("item_quantity") or qty), data_name="item_quantity"),
                   cls="cell--number col-total"),
            ])
            return Tr(*cells)

        def _li_empty_row() -> FT:
            _show_category = doc_type in ("bill", "purchase_order", "consignment_in")
            _show_receive_as = doc_type in ("bill", "purchase_order", "consignment_in")
            _cats = item_categories or []
            if _show_category:
                _cat_options = [Option("", value="")]
                for c in _cats:
                    _cat_options.append(Option(c, value=c))
                _cat_options.append(Option(t("label._add_new"), value="__add_new__"))
                _cat_cell = Td(Select(
                    *_cat_options,
                    data_name="category",
                    cls="cell-input cell-input--select cell-input--xs",
                    onchange="if(this.value==='__add_new__'){window.open('/settings/inventory?tab=category-library','_blank');this.value='';}else{celerpAutoSave();}",
                ))
            else:
                _cat_cell = None

            if _show_receive_as:
                _ra_cell = Td(Select(
                    Option(t("doc.stock"), value="stock", selected=True),
                    Option(t("doc.expense"), value="expense"),
                    data_name="receive_as",
                    cls="cell-input cell-input--select cell-input--xs",
                    onchange="celerpAutoSave()",
                ))
            else:
                _ra_cell = None

            cells = [
                Td(Input(type="checkbox", cls="li-select", value=""), cls="col-checkbox li-checkbox-cell"),
                Td(_sku_input(), cls="col-sku"), Td(_desc_input(), cls="col-desc"),
            ]
            if _cat_cell:
                cells.append(_cat_cell)
            if _ra_cell:
                cells.append(_ra_cell)
            cells.extend([
                Td(Input(type="number", value="1", step="any", data_name="quantity",
                         oninput="celerpUpdateTotals()", onblur="celerpQtyBlur(this); celerpAutoSave()",
                         cls="cell-input cell-input--xs"), cls="col-qty"),
                Td(Span("", data_name="unit", cls="meta-value meta-value--muted",
                         style="font-size:12px;display:inline-block;min-width:40px;"), cls="col-unit"),
                Td(Input(type="number", value="0", step="0.01", data_name="unit_price",
                         oninput="celerpUpdateTotals()", onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs"), cls="col-unit-price"),
                Td(Input(type="number", value="0", step="0.01", data_name="discount_pct",
                         oninput="celerpUpdateTotals()", onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs"), cls="col-disc"),
                Td(_tax_select(), cls="col-tax"),
                Td(Input(type="number", value="0", step="0.01",
                         cls="cell-input line-total",
                         oninput="celerpLineTotalInput(this)",
                         onblur="celerpAutoSave()",
                         data_name="line_total"),
                   Input(type="hidden", value="", data_name="hs_code"),
                   Input(type="hidden", value="", data_name="entity_id"),
                   Input(type="hidden", value="", data_name="allow_splitting"),
                   Input(type="hidden", value="", data_name="item_quantity"),
                   cls="cell--number col-total"),
            ])
            return Tr(*cells)

        rows = [_li_editable_row(li, i) for i, li in enumerate(line_items)]
        if not rows:
            rows = [_li_empty_row()]

        _line_headers = [Th(Input(type="checkbox", id="li-select-all"), cls="col-checkbox li-checkbox-cell"), Th(t("th.skuitem"), cls="col-sku"), Th(t("th.description"), cls="col-desc")]
        if doc_type in ("bill", "purchase_order", "consignment_in"):
            _line_headers.append(Th(t("th.category")))
            _line_headers.append(Th(t("th.type")))
        _line_headers.extend([Th(t("th.qty"), cls="col-qty"), Th(t("th.unit"), cls="col-unit"), Th(t("th.unit_price"), cls="col-unit-price"), Th(t("th.disc"), cls="col-disc"), Th(t("th.tax"), cls="col-tax")])
        if doc_type in ("purchase_order", "bill"):
            _line_headers.append(Th(t("th.account")))
        _line_headers.extend([Th(t("th.total"), cls="cell--number col-total")])

        # CSV import hidden file input + JS handler
        _csv_import_el = Div(
            Input(type="file", id="csv-import-input", accept=".csv,.tsv,.txt",
                  style="display:none",
                  onchange=f"celerpCsvImport(this, '{entity_id}')"),
            cls="csv-import-hidden",
        )

        # AI dropzone — only on draft bills/expenses when celerp-ai is loaded
        from celerp.modules.slots import get as _get_slots_ai
        _ai_loaded = any(c.get("key") == "ai" for c in _get_slots_ai("nav"))
        _ai_dropzone: list = []
        if doc_type in ("bill", "expense") and _ai_loaded:
            _ai_dropzone = [
                Div(
                    Div("✨", cls="ai-dropzone__icon"),
                    Div("Drop PDF or receipt image here to auto-fill this bill", cls="ai-dropzone__text"),
                    Div(t("doc.powered_by_celerp_ai_operator"), cls="ai-dropzone__sub"),
                    cls="ai-dropzone",
                    id=f"ai-dropzone-{entity_id}",
                    title="Auto-fill line items from receipts or invoices",
                ),
                Script(f"""
(function() {{
  var dz = document.getElementById('ai-dropzone-{entity_id}');
  if (!dz) return;
  function handleFile(file) {{
    var fd = new FormData(); fd.append('file', file);
    dz.classList.add('ai-dropzone--over');
    fetch('/docs/{entity_id}/files', {{method:'POST',body:fd}})
      .then(function(r) {{ dz.classList.remove('ai-dropzone--over'); }})
      .catch(function() {{ dz.classList.remove('ai-dropzone--over'); }});
  }}
  dz.addEventListener('click', function() {{
    var inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.pdf,image/*';
    inp.onchange = function() {{ if (inp.files.length) handleFile(inp.files[0]); }};
    inp.click();
  }});
  dz.addEventListener('dragover', function(e) {{ e.preventDefault(); dz.classList.add('ai-dropzone--over'); }});
  dz.addEventListener('dragleave', function() {{ dz.classList.remove('ai-dropzone--over'); }});
  dz.addEventListener('drop', function(e) {{ e.preventDefault(); if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]); }});
}})();
"""),
            ]

        lines_section = Div(
            *_ai_dropzone,
            _csv_import_el,
            Template(_li_empty_row(), id="line-row-tpl"),
            Div(
                Span("📷", cls="scan-bar-icon"),
                Input(type="text", id="scan-bar-input", placeholder="Scan barcode or type SKU and press Enter",
                      cls="scan-bar-input", autocomplete="off", autofocus=False),
                Span("", id="scan-bar-status", cls="scan-bar-status"),
                cls="scan-bar",
            ),
            Div(
                _pl_bar,
                cls="line-toolbar",
            ),
            _li_bulk_toolbar(entity_id, is_list),
            Div(
                Table(
                    Thead(Tr(*_line_headers)),
                    Tbody(*rows, id=line_body_id),
                    cls="data-table doc-lines",
                ),
                cls="table-scroll-wrap",
            ),
            Div(
                Button(t("btn._add_item"), type="button", cls="btn btn--secondary",
                       onclick="celerpAddLine()"),
                Span("", id="save-status", cls="save-status"),
                cls="line-actions gap-sm",
            ),
            Script(f"""
const _CELERP_EID = {repr(entity_id)};
const _CELERP_BASE = {'"/lists/"' if is_list else '"/docs/"'};
const _CELERP_TAXES = {_json.dumps(_taxes_list)};
const _CELERP_DEFAULT_TAX = {repr(_default_tax_value)};
/* ── Price list / doc-type helpers ── */
const _CELERP_DOC_TYPE = {repr(doc_type)};
function _celerpPriceListParam() {{
    const plSelect = document.getElementById('doc-price-list');
    return plSelect ? '&price_list=' + encodeURIComponent(plSelect.value) : '';
}}
function _celerpDocTypeParam() {{
    return _CELERP_DOC_TYPE ? '&doc_type=' + encodeURIComponent(_CELERP_DOC_TYPE) : '';
}}
/* ── Barcode scan bar ── */
(function() {{
    const scanInput = document.getElementById('scan-bar-input');
    const scanStatus = document.getElementById('scan-bar-status');
    if (!scanInput) return;
    scanInput.addEventListener('keydown', async function(e) {{
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const code = scanInput.value.trim();
        if (!code) return;
        scanStatus.textContent = 'Looking up...';
        scanStatus.className = 'scan-bar-status';
        try {{
            const resp = await fetch('/docs/catalog-lookup?sku=' + encodeURIComponent(code) + _celerpPriceListParam() + _celerpDocTypeParam());
            if (!resp.ok) throw new Error('lookup failed');
            const data = await resp.json();
            if (data.description || data.sku) {{
                // Add a new line with the item data
                const tpl = document.getElementById('line-row-tpl').content.cloneNode(true);
                const row = tpl.querySelector('tr') || tpl.children[0];
                if (row) {{
                    const d = {{...data, sku: data.sku || code}};
                    celerpFillRow(row, d);
                }}
                document.getElementById('{line_body_id}').appendChild(tpl);
                celerpUpdateTotals();
                celerpAutoSave();
                scanStatus.textContent = '✓ ' + (data.sku || code);
                scanStatus.className = 'scan-bar-status scan-bar-status--ok';
            }} else {{
                scanStatus.textContent = '✗ Not found: ' + code;
                scanStatus.className = 'scan-bar-status scan-bar-status--err';
            }}
        }} catch (err) {{
            scanStatus.textContent = '✗ Lookup error';
            scanStatus.className = 'scan-bar-status scan-bar-status--err';
        }}
        scanInput.value = '';
        scanInput.focus();
        setTimeout(() => {{ scanStatus.textContent = ''; }}, 3000);
    }});
}})();
function celerpFillRow(row, data) {{
    const descEl = row.querySelector('[data-name="description"]');
    const priceEl = row.querySelector('[data-name="unit_price"]');
    const unitEl = row.querySelector('[data-name="unit"]');
    const qtyEl = row.querySelector('[data-name="quantity"]');
    const skuEl = row.querySelector('[data-name="sku"]');
    const hsCodeEl = row.querySelector('[data-name="hs_code"]');
    const entityIdEl = row.querySelector('[data-name="entity_id"]');
    const allowSplitEl = row.querySelector('[data-name="allow_splitting"]');
    const itemQtyEl = row.querySelector('[data-name="item_quantity"]');
    if (skuEl && data.sku) skuEl.value = data.sku;
    if (descEl && data.description) descEl.value = data.description;
    if (hsCodeEl && data.hs_code) hsCodeEl.value = data.hs_code;
    if (priceEl && data.unit_price != null) priceEl.value = data.unit_price;
    if (unitEl && data.sell_by) unitEl.textContent = data.sell_by;
    if (entityIdEl) entityIdEl.value = data.entity_id || '';
    if (allowSplitEl) allowSplitEl.value = data.allow_splitting ? '1' : '';
    if (itemQtyEl && data.quantity) itemQtyEl.value = data.quantity;
    const receiveAsEl = row.querySelector('[data-name="receive_as"]');
    if (receiveAsEl) receiveAsEl.value = data.entity_id ? 'stock' : 'expense';
    const categoryEl = row.querySelector('[data-name="category"]');
    if (categoryEl && data.category) {{
        // Ensure option exists before setting value (category may not be in inventory yet)
        if (categoryEl.tagName === 'SELECT') {{
            const exists = Array.from(categoryEl.options).some(o => o.value === data.category);
            if (!exists) {{
                const opt = new Option(data.category, data.category);
                // Insert before the last "Add new" option if present
                const addNewIdx = Array.from(categoryEl.options).findIndex(o => o.value === '__add_new__');
                if (addNewIdx >= 0) categoryEl.insertBefore(opt, categoryEl.options[addNewIdx]);
                else categoryEl.appendChild(opt);
            }}
        }}
        categoryEl.value = data.category;
    }}
    // Set quantity: if allow_splitting is false, use full item quantity
    if (qtyEl) {{
        if (!data.allow_splitting && data.quantity > 0) {{
            qtyEl.value = data.quantity;
        }} else if (data.quantity > 0 && (!qtyEl.value || qtyEl.value === '1')) {{
            qtyEl.value = data.quantity;
        }}
    }}
    // Update eye icon link
    const linkEl = row.querySelector('[data-name="item_link"]');
    if (linkEl) {{
        if (data.entity_id) {{
            linkEl.href = '/inventory/' + data.entity_id;
            linkEl.target = '_blank';
            linkEl.className = 'item-link item-link--active';
            linkEl.title = 'View item details';
            linkEl.onclick = null;
        }} else {{
            linkEl.href = '#';
            linkEl.target = '';
            linkEl.className = 'item-link item-link--inactive';
            linkEl.title = 'No linked item';
            linkEl.onclick = (e) => e.preventDefault();
        }}
    }}
    // Set tax dropdown: use item tax_code/tax_rate, fall back to company default
    const taxSel = row.querySelector('[data-name="tax_select"]');
    if (taxSel) {{
        let matched = false;
        if (data.tax_code) {{
            for (const opt of taxSel.options) {{
                if (opt.value.split('|')[0] === data.tax_code) {{ taxSel.value = opt.value; matched = true; break; }}
            }}
        }}
        if (!matched && data.tax_rate != null && data.tax_rate > 0) {{
            for (const opt of taxSel.options) {{
                if (parseFloat(opt.value.split('|')[1]) === data.tax_rate && opt.value !== '|custom') {{
                    taxSel.value = opt.value; matched = true; break;
                }}
            }}
        }}
        if (!matched) taxSel.value = _CELERP_DEFAULT_TAX;
        celerpTaxChange(taxSel);
    }}
}}
/* ── Catalog autocomplete ── */
let _celerpAcTimer = null;
async function celerpAcSearch(input, field) {{
    const q = input.value.trim();
    const wrap = input.parentElement;
    const list = wrap.querySelector('.catalog-ac-list');
    if (!q || q.length < 2) {{ list.style.display = 'none'; return; }}
    clearTimeout(_celerpAcTimer);
    _celerpAcTimer = setTimeout(async () => {{
        const pl = _celerpPriceListParam();
        const resp = await fetch('/docs/catalog-search?q=' + encodeURIComponent(q) + pl + _celerpDocTypeParam());
        if (!resp.ok) return;
        const items = await resp.json();
        list.innerHTML = '';
        items.forEach(item => {{
            const opt = document.createElement('div');
            opt.className = 'catalog-ac-option';
            const label = field === 'sku'
                ? (item.sku || '') + (item.description ? ' – ' + item.description : '')
                : (item.description || '') + (item.sku ? ' [' + item.sku + ']' : '');
            opt.textContent = label;
            opt.addEventListener('mousedown', e => {{
                e.preventDefault();
                const row = input.closest('tr');
                celerpFillRow(row, {{...item, description: item.description}});
                list.style.display = 'none';
                celerpUpdateTotals();
                celerpAutoSave();
            }});
            list.appendChild(opt);
        }});
        // Always append a custom-entry option at the bottom
        const _expenseTypes = ['bill', 'purchase_order', 'consignment_in'];
        const _customLabel = _expenseTypes.includes(_CELERP_DOC_TYPE)
            ? '✏ Use as expense: "' + q + '"'
            : '✏ Use as custom entry: "' + q + '"';
        const custom = document.createElement('div');
        custom.className = 'catalog-ac-option catalog-ac-option--custom';
        custom.textContent = _customLabel;
        custom.addEventListener('mousedown', e => {{
            e.preventDefault();
            list.style.display = 'none';
            // Auto-set receive_as=expense only for purchasing-side documents
            const row = input.closest('tr');
            if (row && _expenseTypes.includes(_CELERP_DOC_TYPE)) {{
                const raEl = row.querySelector('[data-name="receive_as"]');
                if (raEl) raEl.value = 'expense';
            }}
        }});
        list.appendChild(custom);
        list.style.display = 'block';
    }}, 250);
}}
function celerpAcBlur(input) {{
    const list = input.parentElement.querySelector('.catalog-ac-list');
    // If cursor moved to a dropdown option (mousedown), let that handler fire first
    setTimeout(() => {{ list.style.display = 'none'; }}, 200);
    // If this is the SKU field and no entity_id linked yet, attempt a silent exact lookup
    if (input.dataset.name === 'sku') {{
        const row = input.closest('tr');
        const eidEl = row ? row.querySelector('[data-name="entity_id"]') : null;
        if (row && eidEl && !eidEl.value && input.value.trim()) {{
            const sku = input.value.trim();
            const pl = _celerpPriceListParam();
            fetch('/docs/catalog-search?q=' + encodeURIComponent(sku) + pl + _celerpDocTypeParam())
              .then(r => r.ok ? r.json() : [])
              .then(items => {{
                const exact = items.find(i => i.sku && i.sku.toLowerCase() === sku.toLowerCase());
                if (exact && exact.entity_id) celerpFillRow(row, exact);
              }});
        }}
    }}
    celerpAutoSave();
}}
function celerpAcKey(e, input) {{
    const list = input.parentElement.querySelector('.catalog-ac-list');
    const opts = list.querySelectorAll('.catalog-ac-option');
    const active = list.querySelector('.catalog-ac-option--active');
    if (e.key === 'ArrowDown') {{
        e.preventDefault();
        const next = active ? active.nextElementSibling : opts[0];
        if (active) active.classList.remove('catalog-ac-option--active');
        if (next) next.classList.add('catalog-ac-option--active');
    }} else if (e.key === 'ArrowUp') {{
        e.preventDefault();
        const prev = active ? active.previousElementSibling : opts[opts.length - 1];
        if (active) active.classList.remove('catalog-ac-option--active');
        if (prev) prev.classList.add('catalog-ac-option--active');
    }} else if (e.key === 'Enter' && active) {{
        e.preventDefault();
        active.dispatchEvent(new MouseEvent('mousedown'));
    }} else if (e.key === 'Escape') {{
        list.style.display = 'none';
    }}
}}
function celerpLineTotalInput(input) {{
    const row = input.closest('tr');
    if (!row) return;
    const tot = parseFloat(input.value);
    if (isNaN(tot)) return;
    const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
    const discPct = parseFloat(row.querySelector('[data-name="discount_pct"]')?.value || 0);
    const factor = qty * (1 - discPct / 100);
    if (factor === 0) return;
    const unitPriceEl = row.querySelector('[data-name="unit_price"]');
    if (unitPriceEl) unitPriceEl.value = (tot / factor).toFixed(2);
    celerpUpdateTotals();
}}
function celerpQtyBlur(input) {{
    const row = input.closest('tr');
    if (!row) return;
    const allowSplit = row.querySelector('[data-name="allow_splitting"]');
    const itemQtyEl = row.querySelector('[data-name="item_quantity"]');
    const entityIdEl = row.querySelector('[data-name="entity_id"]');
    if (!allowSplit || !entityIdEl || !entityIdEl.value) return;
    // allow_splitting = "1" means splittable, empty/"" means not splittable
    if (allowSplit.value === '1') return;
    const itemQty = parseFloat(itemQtyEl?.value || 0);
    const currentQty = parseFloat(input.value || 0);
    if (itemQty > 0 && currentQty !== itemQty) {{
        const eid = entityIdEl.value;
        const msg = 'Allow splitting is set to false for this item, so you cannot sell less than the full quantity (' + itemQty + '). '
            + 'You can modify this in the item details page: /inventory/' + eid;
        alert(msg);
        // Per UX rules: do NOT revert the value or make readonly - just warn
    }}
}}
function celerpTaxChange(sel) {{
    const customInput = sel.parentElement.querySelector('[data-name="tax_rate_custom"]');
    if (!customInput) return;
    if (sel.value === '|custom') {{
        customInput.style.display = 'inline-block';
        customInput.oninput = () => celerpUpdateTotals();
        customInput.onblur = () => celerpAutoSave();
    }} else {{
        customInput.style.display = 'none';
    }}
    celerpUpdateTotals();
}}
function _celerpTaxRate(row) {{
    const sel = row.querySelector('[data-name="tax_select"]');
    if (!sel) return 0;
    if (sel.value === '|custom') {{
        return parseFloat(row.querySelector('[data-name="tax_rate_custom"]')?.value || 0);
    }}
    return parseFloat(sel.value.split('|')[1] || 0);
}}
function _celerpTaxCode(row) {{
    const sel = row.querySelector('[data-name="tax_select"]');
    if (!sel || sel.value === '|custom' || sel.value === '|0') return '';
    return sel.value.split('|')[0];
}}
function _celerpEditTaxLabel(key, rate, labelEl) {{
    const currentText = labelEl.textContent.replace(/:$/, '').replace(/\s*\(\d+(\.\d+)?%\)$/, '');
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentText;
    input.className = 'cell-input cell-input--xs';
    input.style.width = '120px';
    input.style.display = 'inline';
    const commit = () => {{
        const newLabel = input.value.trim() || 'Custom';
        // Update all matching line rows' hidden tax_label inputs
        document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
            const sel = row.querySelector('[data-name="tax_select"]');
            if (sel && sel.value === '|custom') {{
                const rowRate = parseFloat(row.querySelector('[data-name="tax_rate_custom"]')?.value || 0);
                if (rowRate === rate) {{
                    const lbl = row.querySelector('[data-name="tax_label"]');
                    if (lbl) lbl.value = newLabel;
                }}
            }}
        }});
        celerpUpdateTotals();
        celerpAutoSave();
    }};
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', e => {{
        if (e.key === 'Enter') {{ e.preventDefault(); input.blur(); }}
        if (e.key === 'Escape') {{ e.preventDefault(); input.value = currentText; input.blur(); }}
    }});
    labelEl.textContent = '';
    labelEl.appendChild(input);
    input.focus();
    input.select();
}}
function celerpAddLine() {{
    const tpl = document.getElementById('line-row-tpl').content.cloneNode(true);
    const row = tpl.querySelector('tr') || tpl.children[0];
    if (row) {{
        const taxSel = row.querySelector('[data-name="tax_select"]');
        if (taxSel && _CELERP_DEFAULT_TAX) {{
            taxSel.value = _CELERP_DEFAULT_TAX;
            celerpTaxChange(taxSel);
        }}
    }}
    document.getElementById('{line_body_id}').appendChild(tpl);
    celerpUpdateTotals();
}}
function celerpUpdateTotals() {{
    const _cur = {repr(currency)};
    function _fmt(n) {{
        try {{ return new Intl.NumberFormat('en-US', {{style:'currency',currency:_cur}}).format(n); }}
        catch(e) {{ return _cur + ' ' + n.toLocaleString('en-US', {{minimumFractionDigits:2}}); }}
    }}
    let sub = 0;
    let grossSub = 0;
    let totalDiscount = 0;
    const taxByCode = {{}};
    document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
        const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
        const price = parseFloat(row.querySelector('[data-name="unit_price"]')?.value || 0);
        const discPct = parseFloat(row.querySelector('[data-name="discount_pct"]')?.value || 0);
        const gross = qty * price;
        const discAmt = gross * discPct / 100;
        const tot = gross - discAmt;
        grossSub += gross;
        totalDiscount += discAmt;
        const totalEl = row.querySelector('.line-total');
        if (totalEl && totalEl !== document.activeElement) {{
            totalEl.value = tot.toFixed(2);
        }}
        sub += tot;
        const rate = _celerpTaxRate(row);
        if (rate !== 0) {{
            const code = _celerpTaxCode(row);
            const key = code || ('custom_' + rate);
            const customLabel = row.querySelector('[data-name="tax_label"]')?.value || '';
            const label = code
                ? ((_CELERP_TAXES.find(t => t.name === code) || {{}}).name || code) + ' (' + rate + '%)'
                : (customLabel || 'Custom') + ' (' + rate + '%)';
            if (!taxByCode[key]) taxByCode[key] = {{label, amount: 0, isCustom: !code, rate}};
            taxByCode[key].amount += tot * rate / 100;
        }}
    }});
    // Gross subtotal + discount breakdown (only when line discounts exist)
    const grossEl = document.getElementById('doc-gross-subtotal');
    const discEl = document.getElementById('doc-line-discount');
    if (totalDiscount > 0.005) {{
        if (!grossEl) {{
            // Insert gross subtotal + discount rows before the net subtotal
            const subEl = document.getElementById('doc-subtotal');
            if (subEl) {{
                const subRow = subEl.closest('.total-row');
                if (subRow) {{
                    const discRow = document.createElement('div');
                    discRow.className = 'total-row';
                    discRow.innerHTML = '<span class="total-label">Discount:</span><span class="total-value" id="doc-line-discount">-' + _fmt(totalDiscount) + '</span>';
                    subRow.parentNode.insertBefore(discRow, subRow);
                    const grossRow = document.createElement('div');
                    grossRow.className = 'total-row';
                    grossRow.innerHTML = '<span class="total-label">Subtotal:</span><span class="total-value" id="doc-gross-subtotal">' + _fmt(grossSub) + '</span>';
                    discRow.parentNode.insertBefore(grossRow, discRow);
                    // Relabel net subtotal
                    const lbl = subRow.querySelector('.total-label');
                    if (lbl) lbl.textContent = 'Net Subtotal:';
                }}
            }}
        }} else {{
            grossEl.textContent = _fmt(grossSub);
            if (discEl) discEl.textContent = '-' + _fmt(totalDiscount);
            // Ensure label says Net Subtotal
            const subEl = document.getElementById('doc-subtotal');
            if (subEl) {{
                const lbl = subEl.closest('.total-row')?.querySelector('.total-label');
                if (lbl) lbl.textContent = 'Net Subtotal:';
            }}
        }}
    }} else {{
        // Remove gross/discount rows if no discount
        if (grossEl) grossEl.closest('.total-row')?.remove();
        if (discEl) discEl.closest('.total-row')?.remove();
        const subEl = document.getElementById('doc-subtotal');
        if (subEl) {{
            const lbl = subEl.closest('.total-row')?.querySelector('.total-label');
            if (lbl) lbl.textContent = 'Subtotal:';
        }}
    }}
    const subEl = document.getElementById('doc-subtotal');
    if (subEl) subEl.textContent = _fmt(sub);
    // Update per-code tax rows
    const taxContainer = document.getElementById('doc-tax-rows');
    if (taxContainer) {{
        taxContainer.innerHTML = '';
        let totalTax = 0;
        Object.entries(taxByCode).forEach(([key, t]) => {{
            totalTax += t.amount;
            const row = document.createElement('div');
            row.className = 'total-row';
            if (t.isCustom) {{
                const lbl = document.createElement('span');
                lbl.className = 'total-label total-label--editable';
                lbl.textContent = t.label + ':';
                lbl.title = 'Double-click to rename';
                lbl.style.cursor = 'pointer';
                lbl.addEventListener('dblclick', () => _celerpEditTaxLabel(key, t.rate, lbl));
                const val = document.createElement('span');
                val.className = 'total-value';
                val.textContent = _fmt(t.amount);
                row.appendChild(lbl);
                row.appendChild(val);
            }} else {{
                row.innerHTML = '<span class="total-label">' + t.label + ':</span><span class="total-value">' + _fmt(t.amount) + '</span>';
            }}
            taxContainer.appendChild(row);
        }});
        const totEl = document.getElementById('doc-total');
        if (totEl) totEl.textContent = _fmt(sub + totalTax);
    }} else {{
        const totEl = document.getElementById('doc-total');
        if (totEl) totEl.textContent = _fmt(sub);
    }}
}}
function _celerpCollectLines() {{
    const lines = [];
    document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
        const desc = row.querySelector('[data-name="description"]')?.value;
        const sku = row.querySelector('[data-name="sku"]')?.value;
        const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
        const unitEl = row.querySelector('[data-name="unit"]'); const unit = unitEl ? (unitEl.value || unitEl.textContent || '').trim() : '';
        const price = parseFloat(row.querySelector('[data-name="unit_price"]')?.value || 0);
        const discPct = parseFloat(row.querySelector('[data-name="discount_pct"]')?.value || 0);
        const rate = _celerpTaxRate(row);
        const code = _celerpTaxCode(row);
        const hsCode = row.querySelector('[data-name="hs_code"]')?.value || null;
        const entityId = row.querySelector('[data-name="entity_id"]')?.value || null;
        const taxLabel = row.querySelector('[data-name="tax_label"]')?.value || '';
        const categoryEl = row.querySelector('[data-name="category"]');
        const category = categoryEl ? (categoryEl.value || categoryEl.textContent || '').trim() || null : null;
        const receiveAsEl = row.querySelector('[data-name="receive_as"]');
        const receiveAs = receiveAsEl ? (receiveAsEl.value || receiveAsEl.textContent || '').trim().toLowerCase() || null : null;
        const accountCode = row.querySelector('[data-name="account_code"]')?.value || null;
        const allowSplitting = row.querySelector('[data-name="allow_splitting"]')?.value === '1';
        if (desc || sku || price) {{
            const discounted = qty * price * (1 - discPct / 100);
            const taxList = rate !== 0 ? [{{code: code, rate: rate, amount: 0, order: 0, is_compound: false, label: taxLabel}}] : [];
            lines.push({{description: desc || '', sku: sku || '', quantity: qty || 1, unit,
                         unit_price: price, discount_pct: discPct, tax_rate: rate, taxes: taxList,
                         line_total: discounted, hs_code: hsCode || undefined,
                         entity_id: entityId || undefined,
                         ...(category ? {{category}} : {{}}),
                         ...(receiveAs ? {{receive_as: receiveAs}} : {{}}),
                         ...(accountCode ? {{account_code: accountCode}} : {{}}),
                         allow_splitting: allowSplitting}});
        }}
    }});
    return lines;
}}
async function _celerpPersist() {{
    const lines = _celerpCollectLines();
    if (!lines.length) return;
    const subtotal = lines.reduce((s, l) => s + l.line_total, 0);
    const tax = lines.reduce((s, l) => s + l.line_total * (l.tax_rate / 100), 0);
    const statusEl = document.getElementById('save-status');
    const resp = await fetch(_CELERP_BASE + _CELERP_EID + '/lines', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{line_items: lines, subtotal, tax, total: subtotal + tax}})
    }});
    if (resp.ok) {{
        statusEl.textContent = '✓';
        setTimeout(() => {{ statusEl.textContent = ''; }}, 1500);
    }} else {{
        statusEl.textContent = '✗ Save failed';
        statusEl.style.color = 'red';
    }}
}}
/* Auto-save on blur away from any row cell */
let _celerpSaveTimer = null;
function celerpAutoSave() {{
    clearTimeout(_celerpSaveTimer);
    _celerpSaveTimer = setTimeout(_celerpPersist, 400);
}}
async function celerpReprice(priceList) {{
    /* Save current lines first, then reprice via API and reload */
    await _celerpPersist();
    const resp = await fetch(_CELERP_BASE + _CELERP_EID + '/reprice', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{price_list: priceList}})
    }});
    if (resp.ok) {{
        window.location.reload();
    }} else {{
        const err = await resp.json().catch(() => ({{}}));
        alert(err.error || 'Reprice failed');
    }}
}}
/* ── CSV import ── */
async function celerpCsvImport(input, entityId) {{
    const file = input.files && input.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    const plSelect = document.getElementById('doc-price-list');
    if (plSelect) formData.append('price_list', plSelect.value);
    try {{
        const resp = await fetch(_CELERP_BASE + entityId + '/items/csv', {{
            method: 'POST', body: formData
        }});
        const data = await resp.json();
        if (resp.ok && data.ok) {{
            window.location.reload();
        }} else {{
            alert(data.error || 'Import failed');
        }}
    }} catch (err) {{
        alert('Import failed: ' + err.message);
    }}
    input.value = '';
}}
/* ── Line-item bulk select ── */
(function(){{
  var table=document.querySelector('.doc-lines');
  var toolbar=document.getElementById('li-bulk-toolbar');
  var countEl=document.getElementById('li-bulk-count');
  var sel=document.getElementById('li-bulk-select');
  function _n(){{return table?table.querySelectorAll('tbody .li-select:checked').length:0;}}
  function _update(){{
    var n=_n();
    if(countEl) countEl.textContent=n+' row'+(n===1?'':'s')+' selected';
    if(toolbar) toolbar.style.display=n>0?'flex':'none';
    if(sel&&n===0) sel.value='';
  }}
  if(table) table.addEventListener('change',function(e){{
    if(e.target&&e.target.classList.contains('li-select')) _update();
  }});
  var sa=document.getElementById('li-select-all');
  if(sa) sa.addEventListener('change',function(){{
    if(table) table.querySelectorAll('tbody .li-select').forEach(function(cb){{cb.checked=sa.checked;}});
    _update();
  }});
  var deleteBtn=document.getElementById('li-bulk-delete-btn');
  var labelsBtn=document.getElementById('li-bulk-labels-btn');
  function _hideBtns(){{
    if(deleteBtn) deleteBtn.style.display='none';
    if(labelsBtn) labelsBtn.style.display='none';
  }}
  window.liBulkActionSelected=function(action){{
    _hideBtns();
    if(!action) return;
    if(action==='li-delete'){{
      if(deleteBtn) deleteBtn.style.display='';
    }} else if(action.startsWith('mod:')){{
      if(labelsBtn) labelsBtn.style.display='';
    }}
  }};
  window.liBulkDeleteConfirmed=function(){{
    if(table) table.querySelectorAll('tbody .li-select:checked').forEach(function(cb){{cb.closest('tr').remove();}});
    celerpUpdateTotals(); celerpAutoSave();
    if(sel) sel.value='';
    _hideBtns(); _update();
  }};
  window.liBulkLabelsConfirmed=function(){{
    var ids=[];
    if(table) table.querySelectorAll('tbody .li-select:checked').forEach(function(cb){{
      var row=cb.closest('tr');
      var eidInput=row?row.querySelector('[data-name="entity_id"]'):null;
      var id=(eidInput&&eidInput.value)||cb.value||'';
      if(!id){{var skuEl=row?row.querySelector('[data-name="sku"]'):null;var sku=skuEl?skuEl.value.trim():'';if(sku)id='sku:'+sku;}}
      if(id) ids.push(id);
    }});
    if(!ids.length){{alert('The selected rows have no linked inventory items. Only items picked from the product catalog can be label-printed.');return;}}
    var form=document.createElement('form');
    form.method='POST';form.action='/labels/print-bulk';form.target='_blank';
    ids.forEach(function(id){{
      var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
      form.appendChild(inp);
    }});
    document.body.appendChild(form);form.submit();setTimeout(function(){{form.remove();}},100);
  }};
  window.liActionChanged=function(action){{ window.liBulkActionSelected(action); }};
}})();
"""),
            cls="lines-section",
        )
    else:
        _is_vendor_doc = doc_type in ("bill", "purchase_order", "consignment_in")
        # Show checkboxes + bulk toolbar on finalized docs when celerp-labels is installed
        from celerp.modules.slots import get as _get_slot_labels_fin
        _fin_labels_active = any(a.get("_module") == "celerp-labels" for a in _get_slot_labels_fin("bulk_action"))
        _fin_show_bulk = _fin_labels_active and bool(line_items)

        def _li_row(li: dict) -> FT:
            qty = float(li.get("quantity", 0) or 0)
            price = float(li.get("unit_price", 0) or 0)
            discount_pct = float(li.get("discount_pct") or 0)
            discounted = qty * price * (1 - discount_pct / 100) if discount_pct else qty * price
            line_total = float(li.get("line_total", 0) or 0) or discounted
            cells = []
            if _fin_show_bulk:
                li_eid = li.get("entity_id") or li.get("item_id") or ""
                li_sku = li.get("sku") or ""
                cells.append(Td(
                    Input(type="checkbox", cls="li-select", value=li_eid, data_sku=li_sku),
                    Input(type="hidden", value=li_eid, data_name="entity_id"),
                    cls="col-checkbox li-checkbox-cell",
                ))
            cells += [
                Td(format_value(li.get("description") or li.get("name"))),
                Td(format_value(li.get("sku") or None)),
            ]
            if _is_vendor_doc:
                cells.append(Td(format_value(li.get("category") or None)))
                cells.append(Td(format_value(li.get("receive_as", "stock").capitalize())))
            cells.extend([
                Td(format_value(li.get("quantity"))),
                Td(format_value(li.get("unit") or None)),
                Td(format_value(li.get("unit_price"), "money"), cls="cell--number"),
                Td(f"{discount_pct:.1f}%" if discount_pct else "-"),
                Td(format_value(li.get("tax_rate"))),
                Td(format_value(line_total, "money"), cls="cell--number col-total"),
            ])
            return Tr(*cells)

        _thead_base = []
        if _fin_show_bulk:
            _thead_base.append(Th(Input(type="checkbox", id="li-select-all"), cls="col-checkbox li-checkbox-cell"))
        _thead_base += [Th(t("th.description")), Th(t("th.skuitem"))]
        if _is_vendor_doc:
            _thead_base += [Th(t("th.category")), Th(t("th.type"))]
        _thead_base += [Th(t("th.qty")), Th(t("th.unit")), Th(t("th.unit_price")), Th(t("th.disc")), Th(t("th.tax")), Th(t("th.total"), cls="cell--number col-total")]
        _colspan = len(_thead_base)
        _fin_bulk_id = "fin-lines-body"
        lines_section = Div(
            _li_bulk_toolbar(entity_id, is_list, labels_only=True) if _fin_show_bulk else None,
            Table(
                Thead(Tr(*_thead_base)),
                Tbody(*([_li_row(li) for li in line_items] if line_items else [
                    Tr(Td(t("doc.no_line_items"), colspan=str(_colspan), cls="empty-state-msg"))
                ]), id=_fin_bulk_id),
                cls="data-table doc-lines",
            ),
            Script(f"""
(function(){{
  var table=document.getElementById('{_fin_bulk_id}');
  var toolbar=document.getElementById('li-bulk-toolbar');
  var countEl=document.getElementById('li-bulk-count');
  var sel=document.getElementById('li-bulk-select');
  function _n(){{return table?table.querySelectorAll('.li-select:checked').length:0;}}
  function _update(){{
    var n=_n();
    if(countEl) countEl.textContent=n+' row'+(n===1?'':'s')+' selected';
    if(toolbar) toolbar.style.display=n>0?'flex':'none';
    if(sel&&n===0) sel.value='';
  }}
  if(table) table.addEventListener('change',function(e){{
    if(e.target&&e.target.classList.contains('li-select')) _update();
  }});
  var sa=document.getElementById('li-select-all');
  if(sa) sa.addEventListener('change',function(){{
    if(table) table.querySelectorAll('.li-select').forEach(function(cb){{cb.checked=sa.checked;}});
    _update();
  }});
  var labelsBtn=document.getElementById('li-bulk-labels-btn');
  window.liBulkActionSelected=function(action){{
    if(labelsBtn) labelsBtn.style.display=action&&action.startsWith('mod:')?'':'none';
  }};
  window.liBulkLabelsConfirmed=function(){{
    var ids=[];
    if(table) table.querySelectorAll('.li-select:checked').forEach(function(cb){{
      var row=cb.closest('tr');
      var eidInput=row?row.querySelector('[data-name="entity_id"]'):null;
      var id=(eidInput&&eidInput.value)||cb.value||'';
      if(!id){{var sku=cb.dataset.sku||'';if(sku)id='sku:'+sku;}}
      if(id) ids.push(id);
    }});
    if(!ids.length){{alert('The selected rows have no linked inventory items. Only items picked from the product catalog can be label-printed.');return;}}
    var form=document.createElement('form');
    form.method='POST';form.action='/labels/print-bulk';form.target='_blank';
    ids.forEach(function(id){{
      var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
      form.appendChild(inp);
    }});
    document.body.appendChild(form);form.submit();setTimeout(function(){{form.remove();}},100);
  }};
  window.liActionChanged=function(action){{ window.liBulkActionSelected(action); }};
}})();
""") if _fin_show_bulk else None,
        )

    # --- Totals ---
    # Compute gross (pre-discount) and net (post-discount) subtotals
    def _li_gross(li: dict) -> float:
        return float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)

    def _li_discounted(li: dict) -> float:
        gross = _li_gross(li)
        dpct = float(li.get("discount_pct") or 0)
        return gross * (1 - dpct / 100) if dpct else gross

    gross_subtotal = sum(_li_gross(li) for li in line_items) if line_items else 0.0
    subtotal = float(subtotal_value or 0) or sum(_li_discounted(li) for li in line_items)
    line_discount = gross_subtotal - subtotal  # total discount from line-level discount_pct
    tax_amount = float(tax_value or 0)
    total_amount = float(total_value or 0) or (subtotal + tax_amount)
    discount = float(discount_value or 0)

    # Build per-code tax rows: prefer `taxes` list on line items, fall back to tax_rate
    doc_taxes = doc.get("doc_taxes") or []
    code_totals: dict[str, dict] = {}  # key → {label, amount}

    if doc_taxes:
        # doc_taxes already have computed amounts (server-side)
        for dtax in doc_taxes:
            code = dtax.get("code", "Tax")
            amt = float(dtax.get("amount", 0) or 0)
            if code not in code_totals:
                code_totals[code] = {"label": code, "amount": 0.0}
            code_totals[code]["amount"] += amt
    elif line_items:
        for li in line_items:
            li_total = _li_discounted(li)
            li_taxes = li.get("taxes") or []
            if li_taxes:
                for item in li_taxes:
                    code = item.get("code") or ""
                    rate = float(item.get("rate", 0) or 0)
                    custom_label = item.get("label") or ""
                    amt = float(item.get("amount", 0) or 0) or round(li_total * rate / 100, 2)
                    key = code or f"custom_{rate}"
                    label = f"{code} ({rate}%)" if code else f"{custom_label or 'Tax'} ({rate}%)"
                    if key not in code_totals:
                        code_totals[key] = {"label": label, "amount": 0.0}
                    code_totals[key]["amount"] += amt
            else:
                rate = float(li.get("tax_rate", 0) or 0)
                if rate != 0:
                    amt = round(li_total * rate / 100, 2)
                    key = f"rate_{rate}"
                    label = f"Tax ({rate}%)"
                    if key not in code_totals:
                        code_totals[key] = {"label": label, "amount": 0.0}
                    code_totals[key]["amount"] += amt

    tax_rows = [
        Div(Span(f"{v['label']}:", cls="total-label"),
            Span(fmt_money(v["amount"], currency), cls="total-value"),
            cls="total-row")
        for v in code_totals.values()
    ]

    if not tax_amount and code_totals:
        tax_amount = sum(v["amount"] for v in code_totals.values())
        total_amount = subtotal - discount + tax_amount

    total_panel = Div(
        Div(Span(t("doc.subtotal"), cls="total-label"),
            Span(fmt_money(gross_subtotal, currency), id="doc-gross-subtotal", cls="total-value"), cls="total-row") if line_discount > 0.005 else "",
        Div(Span(t("doc.discount"), cls="total-label"),
            Span(f"-{fmt_money(line_discount, currency)}", id="doc-line-discount", cls="total-value"), cls="total-row") if line_discount > 0.005 else "",
        Div(Span("Net Subtotal:" if line_discount > 0.005 else "Subtotal:", cls="total-label"),
            Span(fmt_money(subtotal, currency), id="doc-subtotal", cls="total-value"), cls="total-row"),
        Div(Span(t("doc.discount"), cls="total-label"),
            Span(fmt_money(discount, currency), cls="total-value"), cls="total-row") if discount else "",
        Div(*tax_rows, id="doc-tax-rows"),
        Div(Span(t("doc.total"), cls="total-label total-label--final"),
            Span(fmt_money(total_amount, currency), id="doc-total", cls="total-value total-value--final"),
            cls="total-row total-row--final"),
        # Conversion note: shown only when doc currency differs from company base currency
        *([Div(
            Span(f"≈ {fmt_money(total_amount * float(doc.get('conversion_rate') or 1), company_currency)} at {doc.get('conversion_rate')} {company_currency}/{currency}", cls="total-label total-label--conversion"),
            cls="total-row total-row--conversion",
        )] if currency != company_currency and doc.get("conversion_rate") else []),
        cls="total-panel",
    )

    contact_label = {
        "invoice": "Bill to", "purchase_order": "Supplier", "quotation": "Quote to",
        "memo": "Receiver", "credit_note": "Issued to", "receipt": "Customer",
        "list": "Customer",
    }.get(doc_type, "Contact")

    # Build contact detail rows - hide payment terms/outstanding for lists
    _contact_rows: list = [
        Div(Span("👤", cls="section-icon"), H3(contact_label, cls="section-title"), cls="section-header"),
        Div(Div(t("doc.contact"), cls="form-label"), _cell("contact_id", contact_value), cls="form-group"),
        Div(Div(t("doc.company"), cls="form-label"), _cell("contact_company_name", doc.get("contact_company_name") or "--"), cls="form-group"),
        Div(Div(t("doc.address"), cls="form-label"), _cell("contact_billing_address", doc.get("contact_billing_address") or doc.get("contact_address")), cls="form-group"),
        Div(Div(t("doc.phone"), cls="form-label"), _cell("contact_phone", doc.get("contact_phone")), cls="form-group"),
        Div(Div(t("doc.email"), cls="form-label"), P(doc.get("contact_email") or "--", cls="meta-value"), cls="form-group"),
        Div(Div(t("doc.tax_id"), cls="form-label"), _cell("contact_tax_id", doc.get("contact_tax_id")), cls="form-group"),
        Hr(cls="section-divider"),
    ]
    if not is_list:
        _contact_rows.append(Div(Div(t("doc.payment_terms"), cls="form-label"), _cell("payment_terms", doc.get("payment_terms")), cls="form-group"))
    _contact_rows.append(Div(Div(t("doc.status"), cls="form-label"), _cell("status", status), *_slot_badges, cls="form-group"))
    # Currency row: always show; rate row shown only when doc currency differs from base
    _contact_rows.append(Div(Div(t("doc.currency"), cls="form-label"), _cell("currency", currency), cls="form-group"))
    if currency != company_currency:
        _rate_val = doc.get("conversion_rate")
        _rate_display = str(_rate_val) if _rate_val else "--"
        _contact_rows.append(Div(Div(t("doc.conversion_rate"), cls="form-label"), _cell("conversion_rate", _rate_display), cls="form-group"))
    if not is_list and outstanding_value is not None:
        _contact_rows.append(Div(Div(t("doc.outstanding"), cls="form-label"), Span(fmt_money(float(outstanding_value or 0), currency), cls="meta-value"), cls="form-group"))

    _is_sub_template = doc_type in ("subscription_invoice", "subscription_po")

    return Div(
        list_type_selector,
        Div(
            Div(*(extra_left_actions or []), *action_btns_left, cls="doc-actions-left"),
            Div(
                Div(*action_btns_right, *(extra_right_actions or []), cls="doc-actions-right") if (action_btns_right or extra_right_actions) else "",
                Span("|", cls="doc-actions-sep") if (action_btns_right or extra_right_actions) else "",
                Div(*action_btns_print, cls="doc-actions-print"),
                cls="doc-actions-right-group",
            ),
            cls="doc-actions",
        ) if (action_btns_left or action_btns_right or action_btns_print) else "",
        po_receive_section,
        # Metadata bar: Doc ID | Reference | Issue date | Due date
        # For subscription templates: show Frequency + Next Issue Date instead of Issue/Due date
        Div(
            Div(Div(t("doc.doc"), cls="meta-label"), _cell("ref_id", ref), cls="meta-cell"),
            Div(Div(t("doc.reference"), cls="meta-label"), _cell("reference", doc.get("reference")), cls="meta-cell"),
            Div(Div("Frequency" if _is_sub_template else t("doc.issue_date"), cls="meta-label"),
                _cell("frequency", doc.get("frequency", "").capitalize()) if _is_sub_template else _cell("issue_date", issue_date_value),
                cls="meta-cell"),
            (Div(Div(t("doc.next_issue_date"), cls="meta-label"),
                 _cell("next_run_date", doc.get("next_run_date") or "--"),
                 cls="meta-cell") if _is_sub_template else
             (Div(Div(t("doc.due_date"), cls="meta-label"), _cell("due_date", due_date_value), cls="meta-cell") if not is_list else "")),
            cls="doc-meta-bar",
        ),
        # Company (left) + Contact/Ship To (right, stacked)
        Div(
            Div(
                Div(Span("🏢", cls="section-icon"), H3(t("page.from"), cls="section-title"), cls="section-header"),
                Div(Div(t("doc.company"), cls="form-label"), _cell("company_name", doc.get("company_name") or "--"), cls="form-group"),
                Div(
                    Div(t("doc.address"), cls="form-label"),
                    _company_address_picker(entity_id, doc.get("company_address") or "", company_locations or []),
                    cls="form-group",
                ),
                Div(Div(t("doc.phone"), cls="form-label"), _cell("company_phone", doc.get("company_phone") or "--"), cls="form-group"),
                Div(Div(t("doc.email"), cls="form-label"), _cell("company_email", doc.get("company_email") or "--"), cls="form-group"),
                Div(Div(t("doc.tax_id"), cls="form-label"), _cell("company_tax_id", doc.get("company_tax_id") or "--"), cls="form-group"),
                cls="doc-section doc-section--half",
            ),
            Div(
                Div(*_contact_rows, cls="doc-section"),
                Div(
                    Div(Span("🚚", cls="section-icon"), H3(t("page.ship_to"), cls="section-title"), cls="section-header"),
                    Div(Div(t("doc.address"), cls="form-label"), _cell("contact_shipping_address", doc.get("contact_shipping_address")), cls="form-group"),
                    Div(Div(t("doc.attn"), cls="form-label"), _cell("shipping_attn", doc.get("shipping_attn")), cls="form-group"),
                    cls="doc-section", style="margin-top:0.75rem",
                ),
                cls="doc-section doc-section--half",
            ),
            cls="doc-row",
        ),
        # Inventory action button - appears just above the line items section, aligned right
        Div(
            _fulfill_el or _receive_return_el or _receive_goods_el or "",
            cls="doc-inventory-action",
        ) if (_fulfill_el or _receive_return_el or _receive_goods_el) else "",
        # Line items + price list bar
        Div(
            lines_section,
            cls="doc-section",
        ),
        # Totals + optional quotation valid-until
        Div(
            Div(Div(t("doc.valid_until"), cls="form-label"), _cell("valid_until", doc.get("valid_until")), cls="form-group") if doc_type == "quotation" else "",
            total_panel,
            cls="doc-section doc-section--totals",
        ),
        # Payment section (invoices, bills, credit notes - not drafts/voids)
        _payment_section(doc, bank_accounts=bank_accounts, is_manager=_is_manager),
        # Term & Conditions + Note to customer (2 columns)
        Div(
            Div(
                Div(Span("📄", cls="section-icon"), H3(t("page.terms_conditions"), cls="section-title"), cls="section-header"),
                *(_tc_dropdown(entity_id, doc, tc_templates or [], doc_type, is_draft) if is_draft and not is_list else [
                    Div(Div(t("doc.template"), cls="form-label"), P(doc.get("terms_template") or "--", cls="meta-value"), cls="form-group"),
                ]),
                Div(
                    Div(t("doc.terms_text"), cls="form-label"),
                    Textarea(doc.get("terms_text") or "", name="terms_text", rows="4",
                             placeholder="Terms & conditions text", cls="form-input",
                             hx_post=f"{_base}/field/terms_text",
                             hx_trigger="blur", hx_swap="none") if is_draft
                    else Div(doc.get("terms_text") or "--", cls="meta-value"),
                    cls="form-group",
                ),
                cls="doc-section doc-section--half",
            ),
            Div(
                Div(Span("💬", cls="section-icon"), H3(t("page.note_to_customer"), cls="section-title"), cls="section-header"),
                Div(
                    Textarea(doc.get("customer_note") or "", name="customer_note", rows="4",
                             placeholder="Add a note to your customer", cls="form-input",
                             hx_post=f"{_base}/field/customer_note",
                             hx_trigger="blur", hx_swap="none") if is_draft
                    else Div(doc.get("customer_note") or "-", cls="meta-value"),
                    cls="form-group",
                ),
                cls="doc-section doc-section--half",
            ),
            cls="doc-row",
        ),
        # Internal information
        Details(
            Summary(
                H2(t("page.additional_internal_information"), cls="internal-section-title"),
                P(t("doc.this_will_not_be_seen_by_your_clients"), cls="internal-section-sub"),
            ),
            Div(
                Div(
                    Div(Span("📝", cls="section-icon"), H3(t("page.internal_notes"), cls="section-title"), cls="section-header"),
                    _shared_notes_tab(
                        entity_id=entity_id,
                        notes=notes or [],
                        add_url=f"{_base}/notes",
                        edit_url=f"{_base}/notes/{{note_id}}/edit",
                        delete_url=f"{_base}/notes/{{note_id}}",
                        refresh_target=f"#notes-section-{_safe_id(entity_id)}",
                        note_field="note",
                        author_field="author_name",
                        tz=tz,
                    ),
                    cls="doc-section doc-section--half",
                    id=f"notes-section-{_safe_id(entity_id)}",
                ),
                Div(
                    Div(Span("🤝", cls="section-icon"), H3(t("page.sales_commissions"), cls="section-title"), cls="section-header"),
                    P(t("doc.commission_agent_and_fee_for_this_document_agent_m"), cls="section-hint"),
                    Div(Div(t("doc.commission_agent"), cls="form-label"), _cell("commission_contact_id", _resolve_contact_display(doc, "commission_contact_id")), cls="form-group"),
                    Div(Div(t("doc.commission"), cls="form-label"), _cell("commission_rate_pct", doc.get("commission_rate_pct")), cls="form-group"),
                    cls="doc-section doc-section--half",
                ),
                cls="doc-row",
            ),
            cls="doc-internal",
        ),
        # --- Attachments section ---
        Details(
            Summary(
                H2(t("label.files"), cls="internal-section-title"),
            ),
            _doc_files_section("doc", entity_id, _enrich_doc_files(doc)),
            cls="doc-internal",
        ),
        # --- History / Activity section ---
        _doc_history_section(ledger or []),
        cls="doc-detail doc-detail--gc",
    )



def _doc_history_section(ledger: list[dict]) -> FT:
    """Render a timeline of ledger events for a document."""
    return activity_table(
        ledger,
        title="History",
        section_cls="doc-section",
        icon="\U0001f4dc",
        empty_msg="No activity recorded yet.",
        max_display=50,
    )


def _doc_status_cards(docs: list[dict], active_status: str, summary: dict | None = None, currency: str | None = None, doc_type: str = "", lang: str = "en", status_in: str = "", overdue_only: bool = False, unfulfilled_only: bool = False, not_restocked: bool = False, not_stocked: bool = False, all_issued: bool = False, converted_to_type: str = "") -> FT:
    """Render status cards for the doc list page. Doc-type-aware."""
    _sm = summary or {}
    base_url = f"/docs?type={doc_type}" if doc_type else "/docs"
    _cbs = _sm.get("count_by_status") or {}

    # Determine active card key
    if all_issued:
        _active_key = "all_issued"
    elif unfulfilled_only:
        _active_key = "unfulfilled"
    elif not_restocked:
        _active_key = "not_restocked"
    elif not_stocked:
        _active_key = "not_stocked"
    elif overdue_only:
        _active_key = "overdue"
    elif active_status:
        _active_key = active_status
    elif status_in:
        _active_key = f"status_in:{status_in}"
    else:
        _active_key = ""

    if doc_type == "invoice":
        _AWAITING_STATUSES = "final,sent,awaiting_payment,partial"
        _PAID_STATUSES = "paid"
        _ALL_ISSUED_STATUSES = "final,sent,awaiting_payment,paid,partial"

        draft_cnt      = _cbs.get("draft", 0)
        all_issued_cnt = _sm.get("all_issued_count", 0)
        awaiting       = _sm.get("awaiting_payment_count", 0)
        overdue        = _sm.get("overdue_count", 0)
        unfulfilled    = _sm.get("unfulfilled_count", 0)
        paid_cnt       = _cbs.get("paid", 0)
        void_cnt       = _cbs.get("void", 0)

        draft_total      = _sm.get("draft_total", 0.0)
        all_issued_total = _sm.get("all_issued_total", _sm.get("ar_total", 0.0))
        awaiting_total   = _sm.get("awaiting_payment_total", 0.0)
        overdue_total    = _sm.get("overdue_total", 0.0)
        unfulfilled_total = _sm.get("unfulfilled_total", 0.0)
        paid_total       = _sm.get("paid_total", 0.0)
        void_total       = _sm.get("void_total", 0.0)

        # Resolve active key for invoice virtual cards
        if active_status == "draft":
            _active_key = "draft"
        elif all_issued or status_in == _ALL_ISSUED_STATUSES:
            _active_key = "all_issued"
        elif overdue_only:
            _active_key = "overdue"
        elif unfulfilled_only:
            _active_key = "unfulfilled"
        elif status_in == _AWAITING_STATUSES:
            _active_key = "awaiting_payment"
        elif status_in == _PAID_STATUSES or active_status == "paid":
            _active_key = "paid"
        elif active_status == "void":
            _active_key = "void"
        else:
            _active_key = active_status or ""

        cards = [
            {"label": t("status.pro_forma", lang),        "count": draft_cnt,      "total": draft_total,      "status": "draft",            "color": "gray"},
            {"label": t("status.all_issued", lang),       "count": all_issued_cnt, "total": all_issued_total, "status": "all_issued",       "color": "blue",   "_url": f"{base_url}&all_issued=1",                                      "_active_key": "all_issued"},
            {"label": t("status.awaiting_payment", lang), "count": awaiting,       "total": awaiting_total,   "status": "awaiting_payment", "color": "yellow", "_url": f"{base_url}&status_in={_AWAITING_STATUSES}",                    "_active_key": "awaiting_payment"},
            {"label": t("status.overdue", lang),          "count": overdue,        "total": overdue_total,    "status": "overdue",          "color": "red",    "_url": f"{base_url}&overdue_only=1",                                    "_active_key": "overdue"},
            {"label": t("status.unfulfilled", lang),      "count": unfulfilled,    "total": unfulfilled_total,"status": "unfulfilled",      "color": "orange", "_url": f"{base_url}&unfulfilled_only=1",                                "_active_key": "unfulfilled"},
            {"label": t("label.paid", lang),              "count": paid_cnt,       "total": paid_total,       "status": "paid",             "color": "green",  "_url": f"{base_url}&status_in={_PAID_STATUSES}",                        "_active_key": "paid"},
            {"label": t("btn.void", lang),                "count": void_cnt,       "total": void_total,       "status": "void",             "color": "gray"},
        ]
        return status_cards(cards, base_url, _active_key or None, total_override=all_issued_cnt, currency=currency, show_all_card=False)

    if doc_type == "memo":
        draft_cnt      = _cbs.get("draft", 0)
        all_issued_cnt = _sm.get("all_issued_count", 0)
        overdue        = _sm.get("overdue_count", 0)
        converted_cnt  = _cbs.get("converted", 0)
        void_cnt       = _cbs.get("void", 0)

        if all_issued or active_status == "" and not overdue_only:
            _active_key = _active_key or ""
        if active_status == "draft":
            _active_key = "draft"
        elif all_issued:
            _active_key = "all_issued"
        elif overdue_only:
            _active_key = "overdue"
        elif active_status == "converted":
            _active_key = "converted"
        elif active_status == "void":
            _active_key = "void"

        cards = [
            {"label": t("status.draft", lang),       "count": draft_cnt,      "total": None, "status": "draft",      "color": "gray"},
            {"label": t("status.all_issued", lang),  "count": all_issued_cnt, "total": None, "status": "all_issued", "color": "blue",  "_url": f"{base_url}&all_issued=1",   "_active_key": "all_issued"},
            {"label": t("status.overdue", lang),     "count": overdue,        "total": None, "status": "overdue",    "color": "red",   "_url": f"{base_url}&overdue_only=1", "_active_key": "overdue"},
            {"label": t("status.converted", lang),   "count": converted_cnt,  "total": None, "status": "converted",  "color": "green"},
            {"label": t("btn.void", lang),           "count": void_cnt,       "total": None, "status": "void",       "color": "gray"},
        ]
        return status_cards(cards, base_url, _active_key or None, currency=currency, show_all_card=False)

    if doc_type == "credit_note":
        draft_cnt       = _cbs.get("draft", 0)
        all_issued_cnt  = _sm.get("all_issued_count", 0)
        not_restocked_cnt = _sm.get("not_restocked_count", 0)
        void_cnt        = _cbs.get("void", 0)

        if active_status == "draft":
            _active_key = "draft"
        elif all_issued:
            _active_key = "all_issued"
        elif not_restocked:
            _active_key = "not_restocked"
        elif active_status == "void":
            _active_key = "void"

        cards = [
            {"label": t("status.draft", lang),       "count": draft_cnt,         "total": None, "status": "draft",        "color": "gray"},
            {"label": t("status.all_issued", lang),  "count": all_issued_cnt,    "total": None, "status": "all_issued",   "color": "blue",   "_url": f"{base_url}&all_issued=1",   "_active_key": "all_issued"},
            {"label": "Not Restocked",               "count": not_restocked_cnt, "total": None, "status": "not_restocked","color": "orange", "_url": f"{base_url}&not_restocked=1","_active_key": "not_restocked"},
            {"label": t("btn.void", lang),           "count": void_cnt,          "total": None, "status": "void",         "color": "gray"},
        ]
        return status_cards(cards, base_url, _active_key or None, currency=currency, show_all_card=False)

    if doc_type == "bill":
        all_issued_cnt  = _sm.get("all_issued_count", 0)
        not_stocked_cnt = _sm.get("not_stocked_count", 0)
        awaiting        = _sm.get("awaiting_payment_count", 0)
        overdue         = _sm.get("overdue_count", 0)
        paid_cnt        = _cbs.get("paid", 0)
        void_cnt        = _cbs.get("void", 0)

        if all_issued:
            _active_key = "all_issued"
        elif not_stocked:
            _active_key = "not_stocked"
        elif overdue_only:
            _active_key = "overdue"
        elif active_status:
            _active_key = active_status

        _AWAITING_STATUSES_BILL = "final,sent,awaiting_payment,partial"
        cards = [
            {"label": t("status.all_issued", lang),   "count": all_issued_cnt,  "total": None, "status": "all_issued",  "color": "blue",   "_url": f"{base_url}&all_issued=1",                        "_active_key": "all_issued"},
            {"label": "Not Stocked Goods",            "count": not_stocked_cnt, "total": None, "status": "not_stocked", "color": "orange", "_url": f"{base_url}&not_stocked=1",                       "_active_key": "not_stocked"},
            {"label": t("status.awaiting_payment", lang), "count": awaiting,    "total": None, "status": "awaiting_payment", "color": "yellow", "_url": f"{base_url}&status_in={_AWAITING_STATUSES_BILL}", "_active_key": "awaiting_payment"},
            {"label": t("status.overdue", lang),      "count": overdue,         "total": None, "status": "overdue",     "color": "red",    "_url": f"{base_url}&overdue_only=1",                      "_active_key": "overdue"},
            {"label": t("label.paid", lang),          "count": paid_cnt,        "total": None, "status": "paid",        "color": "green"},
            {"label": t("btn.void", lang),            "count": void_cnt,        "total": None, "status": "void",        "color": "gray"},
        ]
        return status_cards(cards, base_url, _active_key or None, currency=currency, show_all_card=False)

    if doc_type == "consignment_in":
        draft_cnt      = _cbs.get("draft", 0)
        all_issued_cnt = _sm.get("all_issued_count", 0)
        overdue        = _sm.get("overdue_count", 0)
        converted_cnt  = _cbs.get("converted", 0)
        void_cnt       = _cbs.get("void", 0)

        if active_status == "draft":
            _active_key = "draft"
        elif all_issued:
            _active_key = "all_issued"
        elif overdue_only:
            _active_key = "overdue"
        elif active_status == "converted":
            _active_key = "converted"
        elif active_status == "void":
            _active_key = "void"

        cards = [
            {"label": t("status.draft", lang),       "count": draft_cnt,      "total": None, "status": "draft",      "color": "gray"},
            {"label": t("status.all_issued", lang),  "count": all_issued_cnt, "total": None, "status": "all_issued", "color": "blue",  "_url": f"{base_url}&all_issued=1",   "_active_key": "all_issued"},
            {"label": t("status.overdue", lang),     "count": overdue,        "total": None, "status": "overdue",    "color": "red",   "_url": f"{base_url}&overdue_only=1", "_active_key": "overdue"},
            {"label": t("status.converted", lang),   "count": converted_cnt,  "total": None, "status": "converted",  "color": "green"},
            {"label": t("btn.void", lang),           "count": void_cnt,       "total": None, "status": "void",       "color": "gray"},
        ]
        return status_cards(cards, base_url, _active_key or None, currency=currency, show_all_card=False)

    # Purchase order: keep as-is
    if doc_type == "purchase_order":
        cards = [
            {"label": t("status.purchase_order", lang), "count": _cbs.get("draft", 0), "total": None, "status": "draft", "color": "gray"},
            {"label": t("doc.sent", lang),              "count": _cbs.get("sent", 0),  "total": None, "status": "sent",  "color": "blue"},
            {"label": t("btn.void", lang),              "count": _cbs.get("void", 0),  "total": None, "status": "void",  "color": "gray"},
        ]
        return status_cards(cards, base_url, _active_key or None, currency=currency, show_all_card=False)

    # Generic fallback for remaining doc types (receipt, etc.)
    _DEFAULT_CARDS = [
        ("draft", t("status.draft", lang), "gray"),
        ("awaiting_payment", t("status.awaiting_payment", lang), "yellow"),
        ("paid", t("label.paid", lang), "green"),
        ("overdue", t("status.overdue", lang), "red"),
        ("void", t("btn.void", lang), "gray"),
    ]
    card_defs = _DEFAULT_CARDS
    api_counts = _cbs
    counts: dict[str, int] = {s: api_counts.get(s, 0) for s, _, _ in card_defs}
    totals: dict[str, float] = {s: 0.0 for s, _, _ in card_defs}
    for d in docs:
        s = str(d.get("status") or "").lower()
        if not api_counts and s in counts:
            counts[s] += 1
        if s in totals:
            amt = d.get("total_amount") if d.get("total_amount") is not None else d.get("total")
            try:
                totals[s] += float(amt or 0)
            except (ValueError, TypeError):
                pass
    cards = [
        {"label": label, "count": counts[s], "total": totals[s], "status": s, "color": color}
        for s, label, color in card_defs
    ]
    return status_cards(cards, base_url, active_status or None, currency=currency)


def _summary_bar(summary: dict, doc_type: str = "", currency: str | None = None, lang: str = "en") -> FT:
    # Only show invoice-specific metrics when viewing invoices or all types
    if doc_type and doc_type != "invoice":
        count = summary.get(f"{doc_type}_count", summary.get("total_count", 0))
        return Div(
            Span(f"{doc_type.replace('_', ' ').title()}s: {count}", cls="val-chip"),
            cls="valuation-bar",
        )
    return Div(
        Span(f"{t('chip.ar', lang)}: {fmt_money(float(summary.get('ar_outstanding', 0) or 0), currency)}", cls="val-chip val-chip--alert"),
        Span(f"{t('chip.billed', lang)}: {fmt_money(float(summary.get('ar_total', 0) or 0), currency)}", cls="val-chip"),
        Span(f"{t('chip.invoices', lang)}: {summary.get('invoice_count', 0)}", cls="val-chip"),
        cls="valuation-bar",
    )


def _drafts_tab(draft_count: int, is_active: bool, doc_type: str = "", status: str = "", lang: str = "en") -> FT:
    """Drafts pill - visible when drafts exist, active when in drafts view."""
    if status == "draft":
        return Span()
    type_param = f"&type={doc_type}" if doc_type else ""
    href = f"/docs?view=drafts{type_param}"
    # Invoice drafts are called "Pro Forma" since they use proforma numbering
    label = t("status.pro_forma", lang) if doc_type == "invoice" else t("status.drafts", lang)
    if is_active:
        return A(
            f"{label} ({draft_count})",
            href="/docs" + (f"?type={doc_type}" if doc_type else ""),
            cls="drafts-tab drafts-tab--active",
            title=f"Viewing {label.lower()} - click to return to live documents",
        )
    if draft_count == 0:
        return Span()
    return A(
        f"{label} ({draft_count})",
        href=href,
        cls="drafts-tab",
        title="Click to view draft documents",
    )



# ---------------------------------------------------------------------------
# List listing-page helpers (kept for /lists table view)
# ---------------------------------------------------------------------------

def _list_table(lists: list[dict], lang: str = "en") -> FT:
    if not lists:
        return Div(
            empty_state_cta(t("label.no_lists_yet", lang), t("btn.new_list", lang), "/lists/create-blank", hx_post=True),
            id="list-table",
        )

    def _row(d: dict) -> FT:
        eid = d.get("entity_id") or d.get("id", "")
        ref = d.get("ref_id") or eid
        items = d.get("line_items", [])
        weight = sum(float(li.get("weight_ct") or li.get("weight") or 0) for li in items)
        return Tr(
            Td(A(ref, href=f"/lists/{eid}", cls="table-link")),
            Td(format_value(d.get("list_type"), "badge")),
            Td(format_value(d.get("customer_name") or d.get("receiver") or d.get("customer_id"))),
            Td(format_value(d.get("created_at") or d.get("date"), "date")),
            Td(str(len(items)), cls="cell--number"),
            Td(f"{weight:.2f}" if weight else EMPTY, cls="cell--number"),
            Td(format_value(d.get("total"), "money"), cls="cell--number"),
            Td(format_value(d.get("status"), "badge")),
            cls="data-row",
        )

    return Table(
        Thead(Tr(
            Th(t("th.ref")), Th(t("th.doc_type")), Th(t("th.customer")),
            Th(t("label.issue_date")), Th(t("th.items")), Th(t("th.weight")), Th(t("label.amount")),
            Th(t("th.status")),
        )),
        Tbody(*[_row(d) for d in lists]),
        cls="data-table",
        id="list-table",
    )


def _list_status_cards(summary: dict, active_status: str = "", converted_to_type: str = "") -> FT:
    count_by_status = summary.get("count_by_status", {})
    draft_cnt          = count_by_status.get("draft", 0)
    all_issued_cnt     = summary.get("all_issued_count", 0)
    memo_cnt           = summary.get("converted_to_memo_count", 0)
    invoice_cnt        = summary.get("converted_to_invoice_count", 0)
    void_cnt           = count_by_status.get("void", 0)

    if converted_to_type == "memo":
        _active_key = "converted_to_memo"
    elif converted_to_type == "invoice":
        _active_key = "converted_to_invoice"
    elif active_status == "all_issued":
        _active_key = "all_issued"
    else:
        _active_key = active_status or ""

    cards = [
        {"label": "Draft",                "count": draft_cnt,      "total": None, "status": "draft",            "color": "gray"},
        {"label": "All Issued",           "count": all_issued_cnt, "total": None, "status": "all_issued",       "color": "blue",  "_url": "/lists?all_issued=1",              "_active_key": "all_issued"},
        {"label": "Converted to Memo",    "count": memo_cnt,       "total": None, "status": "converted_to_memo","color": "green", "_url": "/lists?converted_to_type=memo",    "_active_key": "converted_to_memo"},
        {"label": "Converted to Invoice", "count": invoice_cnt,    "total": None, "status": "converted_to_invoice","color": "green","_url": "/lists?converted_to_type=invoice","_active_key": "converted_to_invoice"},
        {"label": "Void",                 "count": void_cnt,       "total": None, "status": "void",             "color": "gray"},
    ]
    return status_cards(cards, "/lists", _active_key or None, show_all_card=False)


def _list_type_tabs(active: str) -> FT:
    all_cls = "category-tab" + (" category-tab--active" if not active else "")
    tabs = [A(t("doc.all"), href="/lists", hx_get="/lists/search", hx_target="#list-table",
               hx_swap="outerHTML", hx_push_url="/lists", cls=all_cls)]
    for lt in _LIST_TYPES:
        label = lt.replace("_", " ").title()
        cls = "category-tab" + (" category-tab--active" if lt == active else "")
        tabs.append(A(
            label,
            href=f"/lists?type={lt}",
            hx_get=f"/lists/search?type={lt}",
            hx_target="#list-table",
            hx_swap="outerHTML",
            hx_push_url=f"/lists?type={lt}",
            cls=cls,
        ))
    return Div(*tabs, cls="category-tabs", id="type-tabs")


def _list_drafts_tab(draft_count: int, is_active: bool, list_type: str = "") -> FT:
    type_param = f"&type={list_type}" if list_type else ""
    if is_active:
        return A(f"Drafts ({draft_count})", href="/lists" + (f"?type={list_type}" if list_type else ""),
                 cls="drafts-tab drafts-tab--active", title="Viewing drafts - click to return")
    if draft_count == 0:
        return Span()
    return A(f"Drafts ({draft_count})", href=f"/lists?view=drafts{type_param}", cls="drafts-tab")
