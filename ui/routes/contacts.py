# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import json as _json
import logging
from datetime import date, datetime, timezone as _tz
from urllib.parse import quote_plus as _quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.attrs import hx_vals
from ui.components.shell import base_shell, page_header
from ui.components.table import search_bar, pagination, EMPTY, breadcrumbs, status_cards, empty_state_cta, fmt_money, format_value, add_new_option, data_table, column_manager
from ui.components.notes import notes_tab as _shared_notes_tab, note_edit_form as _shared_note_edit_form
from ui.components.files import _files_section as _shared_files_section, _DOCUMENT_TAGS
from ui.components.currency import currency_combobox_td, CURRENCY_CODES as _CURRENCY_CODES
from ui.components.phone import phone_input_td, phone_head_items as _phone_head_items
from ui.config import get_token as _token, get_role as _get_role
from celerp.services.permissions import role_has_permission
from ui.i18n import t, get_lang
from ui.routes.reports import _date_filter_bar, _parse_dates

logger = logging.getLogger(__name__)

_PER_PAGE = 50
_EDITABLE = {"name", "company_name", "website", "currency", "phone", "email", "billing_address", "shipping_address", "tax_id", "credit_limit", "payment_terms", "contact_type", "price_list"}

# Contact table schemas - built from a shared base to stay DRY.
# Only show_in_table differs: customers surface credit_limit; vendors surface payment_terms.
_CONTACT_SCHEMA_BASE = [
    {"key": "name",             "label": "Name",             "type": "text",  "editable": True,  "show_in_table": True},
    {"key": "company_name",     "label": "Company",          "type": "text",  "editable": True,  "show_in_table": True},
    {"key": "email",            "label": "Email",            "type": "text",  "editable": True,  "show_in_table": True},
    {"key": "phone",            "label": "Phone",            "type": "text",  "editable": True,  "show_in_table": True},
    {"key": "website",          "label": "Website",          "type": "text",  "editable": True,  "show_in_table": False},
    {"key": "billing_address",  "label": "Billing Address",  "type": "text",  "editable": True,  "show_in_table": True},
    {"key": "shipping_address", "label": "Shipping Address", "type": "text",  "editable": True,  "show_in_table": False},
    {"key": "tax_id",           "label": "Tax ID",           "type": "text",  "editable": True,  "show_in_table": False},
    {"key": "currency",         "label": "Currency",         "type": "text",  "editable": True,  "show_in_table": False},
    {"key": "credit_limit",     "label": "Credit Limit",     "type": "money", "editable": True,  "show_in_table": False},
    {"key": "payment_terms",    "label": "Payment Terms",    "type": "text",  "editable": True,  "show_in_table": False},
    {"key": "price_list",       "label": "Price List",       "type": "text",  "editable": True,  "show_in_table": True},
    {"key": "tags",             "label": "Tags",             "type": "tags",  "editable": False, "show_in_table": True},
]

def _contact_schema(contact_type: str) -> list[dict]:
    """Return contact schema with show_in_table defaults appropriate for the contact_type."""
    extra_visible = {"credit_limit"} if contact_type == "customer" else {"payment_terms"}
    return [
        {**f, "show_in_table": f["show_in_table"] or f["key"] in extra_visible}
        for f in _CONTACT_SCHEMA_BASE
    ]



# UX rule: all editable text fields use dblclick-to-edit, ESC to cancel, blur/Enter to save.
# This is a system-wide rule — see USER.md for details.
def _contact_display_cell(contact_id: str, field: str, value, trigger: str = "dblclick") -> FT:
    if field == "credit_limit":
        display = format_value(value, "money")
    elif field == "contact_type":
        display = format_value(value, "badge")
    elif field == "price_list":
        display = str(value) if value and str(value).strip() else "Company Default"
    else:
        display = str(value) if value and str(value).strip() else EMPTY
    title = "Double-click to edit"
    return Td(
        display,
        title=title,
        hx_get=f"/contacts/{contact_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger=trigger,
        cls="cell cell--clickable",
    )


def _contact_tags_section(contact: dict, vocabulary: list[dict] | None = None) -> FT:
    """Tags section for contact detail page.

    After adding a tag the whole section is re-rendered via HTMX.
    keep typing tags without clicking.
    """
    contact_id = contact.get("entity_id") or contact.get("id") or ""
    tags = list(contact.get("tags") or [])
    vocab_map = {t["name"]: t for t in (vocabulary or []) if t.get("name")}
    tag_pills = []
    for tag in tags:
        v = vocab_map.get(tag)
        pill_style = f"background:{v['color']};color:#fff" if v and v.get("color") else ""
        tag_pills.append(
            Span(
                tag,
                Button(
                    "×",
                    hx_post=f"/contacts/{contact_id}/tags/remove",
                    hx_vals=hx_vals({"tag": tag}),
                    hx_target="#contact-tags",
                    hx_swap="outerHTML",
                    cls="tag-remove",
                    title=f"Remove tag {tag}",
                ),
                cls="tag-pill",
                style=pill_style,
            )
        )
    existing_set = set(tags)
    options = [t["name"] for t in (vocabulary or []) if t.get("name") and t["name"] not in existing_set]
    datalist = Datalist(*[Option(value=o) for o in options], id=f"tag-options-{contact_id}") if options else ""
    add_input = Form(
        Input(
            type="text", name="tag", placeholder="+ Add tag",
            cls="tag-add-input", autocomplete="off",
            list=f"tag-options-{contact_id}" if options else None,
        ),
        datalist,
        hx_post=f"/contacts/{contact_id}/tags/add",
        hx_target="#contact-tags",
        hx_swap="outerHTML",
        hx_trigger="submit, keydown[key=='Enter'] from:find input, blur from:find input delay:200ms",
    )
    return Div(
        H3(t("page.tags"), cls="section-title"),
        Div(*tag_pills, add_input, cls="tags-row"),
        cls="tags-section",
        id="contact-tags",
    )



def _collect_contact_files(contact: dict, docs: list[dict]) -> list[dict]:
    """Merge contact-own files with files from related docs.

    Contact files have no linked_ref (shown as '--').
    Doc files are tagged with linked_ref=doc_number and linked_url=/docs/{id}.
    File ids are unique across the merged list (dedup by id).
    """
    seen: set[str] = set()
    result: list[dict] = []

    for f in contact.get("files", []):
        fid = f.get("id", "")
        if fid not in seen:
            seen.add(fid)
            result.append({**f, "linked_ref": "", "linked_url": ""})

    for doc in docs:
        doc_id = doc.get("entity_id") or doc.get("id") or ""
        ref = doc.get("ref_id") or doc.get("doc_number") or ""
        url = f"/docs/{doc_id}" if doc_id else ""
        for f in doc.get("files", []):
            fid = f.get("id", "")
            if fid not in seen:
                seen.add(fid)
                # Override download + mutation URLs — file lives under the doc entity.
                download_url = f"/docs/{doc_id}/files/{fid}/download" if doc_id else ""
                action_base = f"/docs/{doc_id}/files" if doc_id else ""
                result.append({**f, "linked_ref": ref, "linked_url": url,
                                "_download_url": download_url,
                                "_action_base_url": action_base,
                                "_no_delete": True})

    return result


def _collect_company_files(self_contact: dict, items: list[dict], docs: list[dict]) -> list[dict]:
    """Aggregate a read-only company-wide files view: the company's own documents (self-contact files),
    every inventory item's images, and every document's attachments. Each row keeps the right linked
    ref/url + download/action overrides so it still points back at its owning entity (reuses the same
    override shape as _collect_contact_files - DRY)."""
    seen: set[str] = set()
    out: list[dict] = []

    def _add(f: dict, ref: str, url: str, base: str) -> None:
        fid = f.get("id", "")
        if not fid or fid in seen:
            return
        seen.add(fid)
        row = {**f, "linked_ref": ref, "linked_url": url, "_no_delete": True}
        if base:
            row["_download_url"] = f"{base}/{fid}/download"
            row["_action_base_url"] = base
        out.append(row)

    for f in (self_contact.get("files") or []):
        _add(f, "Company", "/finance/company-details", "")
    for it in items:
        iid = it.get("id") or it.get("entity_id") or ""
        ref = it.get("sku") or it.get("name") or "Item"
        for f in (it.get("files") or []):
            _add(f, f"Item: {ref}", f"/inventory/{iid}" if iid else "", f"/items/{iid}/files" if iid else "")
    for d in docs:
        did = d.get("id") or d.get("entity_id") or ""
        ref = d.get("ref_id") or d.get("doc_number") or "Doc"
        for f in (d.get("files") or []):
            _add(f, f"Doc: {ref}", f"/docs/{did}" if did else "", f"/docs/{did}/files" if did else "")
    return out


def _files_section(contact: dict, contact_id: str, docs: list[dict] | None = None, **kwargs) -> FT:
    """Wrapper - merges contact-own files with related doc files, then delegates to shared component."""
    merged = _collect_contact_files(contact, docs or [])
    return _shared_files_section("contact", contact_id, merged, **kwargs)


def _contact_info_card(c: dict, *, oob: bool = False, hide_fields: tuple = ()) -> FT:
    """Left-column contact info: click-to-edit fields. hide_fields drops rows that would be redundant in
    a given context (e.g. the Company Details page hides Currency - it's in the Settings card - and the
    Billing/Shipping text fields, which the dedicated two-column address book already covers)."""
    cid = c.get("entity_id") or c.get("id") or ""
    fields = [
        ("name", "Name"), ("company_name", "Company"), ("email", "Email"), ("phone", "Phone"),
        ("website", "Website"), ("billing_address", "Billing Address"), ("shipping_address", "Shipping Address"),
        ("tax_id", "Tax ID"), ("currency", "Currency"),
    ]
    fields = [(k, lbl) for k, lbl in fields if k not in hide_fields]
    attrs = {"hx_swap_oob": "outerHTML:#contact-info-card"} if oob else {}
    return Div(
        H3(t("page.contact_info"), cls="section-title"),
        Table(*[Tr(Td(label, cls="detail-label"), _contact_display_cell(cid, key, c.get(key))) for key, label in fields], cls="detail-table"),
        cls="detail-card section-card",
        id="contact-info-card",
        **attrs,
    )


def _settings_card(c: dict) -> FT:
    """Right-column settings: price list, payment terms, credit limit."""
    cid = c.get("entity_id") or c.get("id") or ""
    fields = [
        ("price_list", "Price List"), ("payment_terms", "Payment Terms"), ("credit_limit", "Credit Limit"),
    ]
    return Div(
        H3(t("page.settings"), cls="section-title"),
        Table(*[Tr(Td(label, cls="detail-label"), _contact_display_cell(cid, key, c.get(key))) for key, label in fields], cls="detail-table"),
        cls="detail-card section-card",
    )


def _financial_summary(docs: list[dict], contact_id: str = "", fiscal_year_start: str = "01-01") -> FT:
    """Compute and render financial summary cards from contact docs."""
    from ui.routes.reports import _fy_start
    today = date.today()
    # Reuse the canonical parser: it falls back to Jan 1 on a malformed
    # fiscal_year_start (e.g. "01" with no day) instead of crashing the page.
    fy_start = _fy_start(fiscal_year_start, today)
    fy_start_str = fy_start.isoformat()

    invoices = [d for d in docs if d.get("doc_type") == "invoice"]
    ytd_invoices = [d for d in invoices if (d.get("issue_date") or "") >= fy_start_str]
    total_invoiced = sum(float(d.get("total_amount") or 0) for d in ytd_invoices)
    total_paid = sum(float(d.get("amount_paid") or 0) for d in ytd_invoices)
    outstanding = total_invoiced - total_paid

    # Avg days to pay (all-time, not just YTD)
    days = []
    for d in invoices:
        if d.get("status") == "paid" and d.get("paid_date") and d.get("issue_date"):
            try:
                paid_dt = datetime.fromisoformat(str(d["paid_date"])[:10])
                issue_dt = datetime.fromisoformat(str(d["issue_date"])[:10])
                days.append((paid_dt - issue_dt).days)
            except (ValueError, TypeError):
                pass
    avg_dtp = f"{sum(days) // len(days)}d" if days else EMPTY

    # Consignment total (active consignments for this contact)
    consignment_docs = [d for d in docs if d.get("doc_type") == "consignment_in"]
    total_consigned = sum(
        float(d.get("total_amount") or 0) for d in consignment_docs
        if d.get("status") not in ("void", "converted")
    )

    def _card(label: str, value: str, sub_label: str = "", href: str = "") -> FT:
        label_el = Div(
            Span(label),
            Span(sub_label, cls="financial-card-sublabel") if sub_label else "",
            cls="financial-card-label",
        )
        content = [label_el, Div(value, cls="financial-card-value")]
        if href:
            return A(*content, href=href, cls="financial-card financial-card--link")
        return Div(*content, cls="financial-card")

    ytd_filter = f"&from_date={fy_start_str}&to_date={today.isoformat()}"
    contact_filter = f"&contact_id={contact_id}" if contact_id else ""

    return Div(
        _card("Total Invoiced", fmt_money(total_invoiced, None), sub_label="Year To Date",
              href=f"/docs?type=invoice{contact_filter}{ytd_filter}" if contact_id else ""),
        _card("Total Paid", fmt_money(total_paid, None), sub_label="Year To Date",
              href=f"/docs?type=invoice&status=paid{contact_filter}{ytd_filter}" if contact_id else ""),
        _card("Outstanding", fmt_money(outstanding, None),
              href=f"/docs?type=invoice&status=awaiting_payment{contact_filter}" if contact_id else ""),
        _card("Avg Days to Pay", avg_dtp),
        _card("Consignment", fmt_money(total_consigned, None),
              href=f"/docs?type=consignment_in{contact_filter}" if contact_id else ""),
        cls="financial-cards",
    )


def _compose_address_str(addr: dict) -> str:
    """Build a single-line address string for display/sync."""
    parts = [
        addr.get("line1", ""),
        addr.get("line2", ""),
        addr.get("city", ""),
        addr.get("state", ""),
        addr.get("postal_code", ""),
        addr.get("country", ""),
    ]
    return ", ".join(p for p in parts if p)


def _address_card(cid: str, addr: dict) -> FT:
    """Render a single address card with edit/delete/make-primary actions."""
    addr_id = addr.get("address_id", "")
    _dom = f"addr-{addr_id.replace(':', '-')}"  # address ids contain ':' which is invalid in a CSS id selector
    addr_type = addr.get("address_type", "billing")
    is_default = bool(addr.get("is_default"))
    lines = [P(addr.get("line1", ""))] if addr.get("line1") else []
    if addr.get("line2"):
        lines.append(P(addr["line2"]))
    city_parts = [addr.get("city", ""), addr.get("state", ""), addr.get("postal_code", "")]
    city_line = ", ".join(p for p in city_parts if p)
    if city_line:
        lines.append(P(city_line))
    if addr.get("country"):
        lines.append(P(addr["country"]))
    if addr.get("attn"):
        lines.append(P(f"Attn: {addr['attn']}", cls="addr-attn"))
    if not lines:
        lines = [P(EMPTY)]
    primary_btn = (
        Span(t("label._primary"), cls="badge badge--primary", title="This is the primary address")
        if is_default else
        Button(t("btn._make_primary"),
               hx_post=f"/contacts/{cid}/addresses/{addr_id}/make-primary",
               hx_target="#addresses-section",
               hx_swap="outerHTML",
               cls="btn btn--xs btn--ghost",
               title="Set as primary address")
    )
    return Div(
        *lines,
        Div(
            primary_btn,
            Button("✏", hx_get=f"/contacts/{cid}/addresses/{addr_id}/edit", hx_target=f"#{_dom}", hx_swap="outerHTML", cls="btn btn--xs btn--secondary", title="Edit"),
            # The primary address can't be deleted - it keeps the contact (incl. the company) with a
            # billing address at all times; make another address primary first to remove this one.
            (Button("×", hx_delete=f"/contacts/{cid}/addresses/{addr_id}", hx_target="#addresses-section", hx_swap="outerHTML", hx_confirm="Remove this address?", cls="btn btn--xs btn--danger", title="Remove")
             if not is_default else None),
            cls="addr-actions",
        ),
        cls="address-card", id=_dom,
    )


def _addresses_section(contact: dict) -> FT:
    """Render addresses in two columns: Billing | Shipping, each with Make Primary."""
    cid = contact.get("entity_id") or contact.get("id") or ""
    addresses = list(contact.get("addresses") or [])
    billing = [a for a in addresses if a.get("address_type") == "billing"]
    shipping = [a for a in addresses if a.get("address_type") == "shipping"]

    billing_col = Div(
        Div(H4(t("page.billing_addresses"), cls="addr-col-title"),
            Button(t("btn._add_billing"), hx_get=f"/contacts/{cid}/addresses/new?type=billing", hx_target=f"#addr-new-billing", hx_swap="innerHTML", cls="btn btn--xs btn--secondary"),
            cls="addr-col-header"),
        *[_address_card(cid, a) for a in billing] if billing else [P(t("label.none_yet"), cls="empty-state-msg")],
        Div(id="addr-new-billing"),
        cls="addr-col",
    )
    shipping_col = Div(
        Div(H4(t("page.shipping_addresses"), cls="addr-col-title"),
            Button(t("btn._add_shipping"), hx_get=f"/contacts/{cid}/addresses/new?type=shipping", hx_target=f"#addr-new-shipping", hx_swap="innerHTML", cls="btn btn--xs btn--secondary"),
            cls="addr-col-header"),
        *[_address_card(cid, a) for a in shipping] if shipping else [P(t("label.none_yet"), cls="empty-state-msg")],
        Div(id="addr-new-shipping"),
        cls="addr-col",
    )
    return Div(
        H3(t("page.addresses"), cls="section-title"),
        Div(billing_col, shipping_col, cls="addr-cols"),
        cls="section-card", id="addresses-section",
    )


def _people_section(contact: dict) -> FT:
    """Render contact people list with add button."""
    cid = contact.get("entity_id") or contact.get("id") or ""
    people = list(contact.get("people") or [])
    cards = []
    for i, person in enumerate(people):
        pid = person.get("id", str(i))
        name_parts = [Span(person.get("name", ""), cls="person-name")]
        if person.get("role"):
            name_parts.append(Span(f" ({person['role']})", cls="person-role"))
        if person.get("is_primary"):
            name_parts.append(" ⭐")
        extras = []
        if person.get("email"):
            extras.append(Div(person["email"], cls="person-email"))
        if person.get("phone"):
            extras.append(Div(person["phone"], cls="person-phone"))
        cards.append(Div(
            Div(*name_parts),
            *extras,
            Div(
                Button("✏", hx_get=f"/contacts/{cid}/people/{pid}/edit", hx_target=f"#person-{pid}", hx_swap="outerHTML", cls="btn btn--xs btn--secondary", title="Edit"),
                Button("×", hx_delete=f"/contacts/{cid}/people/{pid}", hx_target="#people-section", hx_swap="outerHTML", hx_confirm="Remove this person?", cls="btn btn--xs btn--danger", title="Remove"),
                cls="person-actions",
            ),
            cls="person-card", id=f"person-{pid}",
        ))
    if not cards:
        cards = [P(t("label.no_contact_people_yet"), cls="empty-state-msg")]
    return Div(
        H3(t("page.contact_people"), cls="section-title"),
        *cards,
        Button(t("btn._add_person"), hx_get=f"/contacts/{cid}/people/new", hx_target="#person-new-form", hx_swap="innerHTML", cls="btn btn--secondary btn--sm"),
        Div(id="person-new-form"),
        cls="section-card", id="people-section",
    )


def _tab_bar(cid: str, active: str = "documents", lead_tabs: list | None = None) -> FT:
    """HTMX-driven tab bar for Documents / Notes / Activity. lead_tabs prepends extra (key, label, href)
    tabs (e.g. the company's Files tab, which loads a different route than /contacts/{cid}/tab/{key})."""
    tabs = list(lead_tabs or []) + [("documents", "Documents", None), ("notes", "Notes", None), ("activity", "Activity", None)]
    return Div(
        *[A(
            label,
            hx_get=(href or f"/contacts/{cid}/tab/{key}"),
            hx_target="#tab-content",
            hx_swap="innerHTML",
            cls="tab active" if key == active else "tab",
            id=f"tab-{key}",
            # JS: set active class on click
            onclick="document.querySelectorAll('.tab-bar .tab').forEach(t=>t.classList.remove('active'));this.classList.add('active');",
        ) for key, label, href in tabs],
        cls="tab-bar",
    )


def _documents_tab(docs: list[dict], contact: dict | None = None, contact_id: str = "", show_files: bool = True) -> FT:
    """Documents tab content: table of related docs (+ the file upload zone unless show_files=False).
    The Company Details page passes show_files=False because company files have their own page."""
    # Related documents table
    if not docs:
        docs_section = P(
            "Invoices, quotes, receipts, bills and credit notes involving this contact appear here "
            "automatically as you create them. None yet.",
            cls="empty-state-msg",
        )
    else:
        sorted_docs = sorted(docs, key=lambda d: d.get("issue_date") or d.get("created_at") or "", reverse=True)
        rows = []
        for d in sorted_docs:
            doc_id = d.get("entity_id") or d.get("id", "")
            doc_num = d.get("doc_number") or doc_id
            rows.append(Tr(
                Td(A(format_value(doc_num), href=f"/docs/{doc_id}", cls="table-link")),
                Td(format_value((d.get("doc_type") or "").replace("_", " ").title(), "badge")),
                Td(format_value(str(d.get("issue_date") or "")[:10] or None)),
                Td(format_value(d.get("total_amount"), "money"), cls="cell--number"),
                Td(format_value(d.get("status"), "badge")),
                cls="data-row",
            ))
        docs_section = Table(
            Thead(Tr(Th(t("th.doc")), Th(t("th.doc_type")), Th(t("th.date")), Th(t("th.total")), Th(t("th.status")))),
            Tbody(*rows),
            cls="data-table",
        )

    # Files / upload section (suppressed on the Company Details page - files live on Company Files)
    if show_files and contact is not None and contact_id:
        files_content = _files_section(contact, contact_id, docs)
    else:
        files_content = ""

    return Div(docs_section, files_content)


async def _company_timezone(token: str) -> str:
    """Return the company's configured timezone string, defaulting to UTC."""
    try:
        company = await api.get_company(token)
        return company.get("timezone") or "UTC"
    except Exception:
        return "UTC"


def _notes_tab(contact_id: str, notes: list[dict], tz: str = "UTC") -> FT:
    """Notes tab content: add form + timeline. Delegates to shared notes_tab component."""
    return _shared_notes_tab(
        entity_id=contact_id,
        notes=notes,
        add_url=f"/contacts/{contact_id}/notes",
        edit_url=f"/contacts/{contact_id}/notes/{{note_id}}/edit",
        delete_url=f"/contacts/{contact_id}/notes/{{note_id}}",
        refresh_target="#tab-content",
        note_field="note",
        author_field="author_name",
        tz=tz,
    )


def _contact_ledger_table(ledger: list[dict]) -> FT:
    """Activity history section for contact detail page."""
    from ui.components.activity import activity_table
    return activity_table(ledger, max_display=10)


def _contacts_bulk_toolbar(contact_type: str) -> FT:
    """Sticky bulk action toolbar for the contacts list. Hidden until rows are selected."""
    return Div(
        Span(t("doc.0_selected"), id="contact-bulk-count", cls="bulk-count"),
        Button(t("btn.clear"), id="contact-bulk-clear-btn", cls="btn btn--ghost btn--sm",
               style="display:none",
               onclick=(
                   "CelerpSelection.clear();"
                   "CelerpSelection.syncCheckboxes();"
                   "document.getElementById('contact-bulk-count').textContent='0 selected';"
                   "document.getElementById('contact-bulk-toolbar').classList.remove('is-active');"
                   "document.getElementById('contact-bulk-clear-btn').style.display='none';"
                   "_resetBulkActions();"
               )),
        Select(
            Option(t("inv.action"), value="", disabled=True, selected=True),
            Option(t("btn.export_csv") + " (selected)", value="contact-export"),
            Option(t("inv.merge"), value="contact-merge"),
            Option(t("btn.delete"), value="delete"),
            id="contact-bulk-select", cls="form-input form-input--sm",
            onchange="contactBulkChanged(this.value)",
        ),
        Div(id="contact-bulk-context"),
        Div(id="bulk-action-result"),
        _contact_bulk_templates(contact_type),
        id="contact-bulk-toolbar",
        cls="bulk-toolbar",
    )


def _contact_bulk_templates(contact_type: str) -> FT:
    """Hidden <template> elements for contact bulk action context UI."""
    from fasthtml.common import Template as _Tpl
    merge_tpl = _Tpl(
        Div(
            P(f"Select the primary {contact_type} to keep. All others will be merged into it.",
              cls="meta-value", style="margin-bottom:0.5rem;font-size:0.85rem;"),
            Div(id="contact-merge-radio-list"),
            Button(t("btn.confirm_merge"), type="button", id="contact-merge-confirm-btn",
                   cls="btn btn--primary btn--sm", style="margin-top:0.75rem;",
                   disabled=True,
                   onclick="contactMergeSubmit()"),
            Script("""
(function(){
  var list = document.getElementById('contact-merge-radio-list');
  if (!list) return;
  var all = CelerpSelection.all();
  Object.keys(all).forEach(function(id) {
    var meta = all[id];
    var label = document.createElement('label');
    label.style.cssText = 'display:flex;align-items:center;gap:0.5rem;margin:0.25rem 0;font-size:0.85rem;cursor:pointer;';
    var radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'contact-merge-winner'; radio.value = id;
    radio.addEventListener('change', function() {
      document.getElementById('contact-merge-confirm-btn').disabled = false;
    });
    label.appendChild(radio);
    label.appendChild(document.createTextNode((meta.name || id) + (meta.contact_type ? ' (' + meta.contact_type + ')' : '')));
    list.appendChild(label);
  });
  window.contactMergeSubmit = function() {
    var winner = document.querySelector('input[name="contact-merge-winner"]:checked');
    if (!winner) return;
    var sources = CelerpSelection.ids().filter(function(id) { return id !== winner.value; });
    fetch('/crm/contacts/merge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target_contact_id: winner.value, source_contact_ids: sources})
    }).then(function(r) { return r.json(); }).then(function(d) {
      if (d.merged_into) {
        window.location.href = '/contacts/' + d.merged_into;
      } else {
        var res = document.getElementById('bulk-action-result');
        if (res) res.innerHTML = '<p class="flash flash--error">' + (d.detail || 'Merge failed') + '</p>';
      }
    }).catch(function(err) {
      var res = document.getElementById('bulk-action-result');
      if (res) res.innerHTML = '<p class="flash flash--error">Merge failed: ' + err.message + '</p>';
    });
  };
  if (window.htmx) htmx.process(list.parentElement);
})();
"""),
            cls="bulk-context",
        ),
        id="tpl-contact-merge",
    )
    return merge_tpl


def _contacts_content(
    contact_type: str,
    contacts: list[dict],
    q: str,
    page: int,
    total: int,
    per_page: int,
    sort: str,
    sort_dir: str,
    currency: str | None = None,
) -> FT:
    """Inner content fragment for contacts list - used by full page and HTMX partial."""
    schema = _contact_schema(contact_type)
    # All columns are rendered in the DOM so the column manager can toggle them.
    # show_in_table controls only the *initial* visible set (seeded into localStorage).
    base_url = f"/contacts/{contact_type}s"
    create_url = f"/contacts/create?type={contact_type}"
    extra_params = {"type": contact_type, "q": q, "dir": sort_dir} if q else {"type": contact_type, "dir": sort_dir}
    link_fn = {"name": "/contacts/{id}", "company_name": "/contacts/{id}"}

    if not contacts:
        table_content = Div(
            empty_state_cta(
                f"No {contact_type}s yet.",
                f"Add {contact_type.title()}",
                create_url,
                hx_post=True,
            ),
        )
    else:
        table_content = data_table(
            schema,
            contacts,
            entity_type=f"{contact_type}s",
            sort_key=sort,
            sort_dir=sort_dir,
            sort_url="/contacts/content",
            extra_params=extra_params,
            currency=currency,
            sort_target="#contacts-content",
            show_row_menu=False,
            show_checkboxes=True,
            selection_key="celerp_contact_selection",
            link_fn=link_fn,
            auto_hide_empty=False,
            edit_url_tpl="/contacts/{id}/field/{field}/edit",
        )

    return Div(
        table_content,
        pagination(page, total, per_page, base_url,
                   f"type={contact_type}&q={_quote_plus(q)}&sort={sort}&dir={sort_dir}".strip("&")),
        id="contacts-content",
    )


async def _contacts_page_shell(contact_type: str, contacts: list[dict], request: Request, q: str, page: int, total: int, per_page: int, sort: str, sort_dir: str, currency: str | None = None, settings: dict | None = None) -> FT:
    """Renders the full contact list page for customers or vendors."""
    label = "Customers" if contact_type == "customer" else "Vendors"
    nav_key = "customers" if contact_type == "customer" else "vendors"
    base_url = f"/contacts/{contact_type}s"
    create_url = f"/contacts/create?type={contact_type}"
    search_url = "/contacts/content"
    schema = _contact_schema(contact_type)
    et = f"{contact_type}s"

    _settings = settings or {}
    _role = _get_role(request)
    _can_edit = role_has_permission(_settings, _role, "edit_contacts")
    _can_import_export = role_has_permission(_settings, _role, "import_export_data")
    return await base_shell(
        page_header(
            label,
            search_bar(placeholder=f"Search {label.lower()}...", target="#contacts-content", url=search_url),
            Button(f"New {label[:-1]}", hx_post=create_url, hx_swap="none", cls="btn btn--primary") if _can_edit else "",
            A(t("btn.export_csv"), href=f"{base_url}/export/csv", cls="btn btn--secondary") if _can_import_export else "",
            A(t("btn.import"), href="/crm/import/contacts", cls="btn btn--secondary") if _can_import_export else "",
        ),
        Div(column_manager(schema, et), cls="column-manager-row"),
        _contacts_bulk_toolbar(contact_type),
        _contacts_content(contact_type, contacts, q, page, total, per_page, sort, sort_dir, currency),
        Script(f"""
(function(){{
  function updateContactBulkToolbar(){{
    var n = typeof CelerpSelection !== 'undefined' ? CelerpSelection.count() : 0;
    var toolbar = document.getElementById('contact-bulk-toolbar');
    var countEl = document.getElementById('contact-bulk-count');
    var clearBtn = document.getElementById('contact-bulk-clear-btn');
    if (countEl) countEl.textContent = n + ' selected';
    if (toolbar) toolbar.classList.toggle('is-active', n > 0);
    if (clearBtn) clearBtn.style.display = n > 0 ? '' : 'none';
  }}
  window.contactBulkChanged = function(action){{
    var ctx = document.getElementById('contact-bulk-context');
    if (ctx) ctx.innerHTML = '';
    document.getElementById('bulk-action-result').innerHTML = '';
    if (action === 'contact-export'){{
      var ids = typeof CelerpSelection !== 'undefined' ? CelerpSelection.ids() : [];
      if (!ids.length) return;
      var qs = ids.map(function(id){{return 'selected=' + encodeURIComponent(id);}}).join('&');
      window.location.href = '/contacts/{contact_type}s/export/csv?' + qs;
      return;
    }}
    if (action === 'delete'){{
      if (!confirm('Delete selected contacts? Contacts linked to open documents cannot be deleted.')) return;
      var ids = typeof CelerpSelection !== 'undefined' ? CelerpSelection.ids() : [];
      fetch('/crm/contacts/bulk/delete', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{contact_ids: ids}})
      }}).then(function(r){{ return r.json(); }}).then(function(d){{
        var res = document.getElementById('bulk-action-result');
        if (d.deleted !== undefined){{
          res.innerHTML = '<p class="flash flash--success">Deleted ' + d.deleted + ' contact(s).</p>';
          setTimeout(function(){{ sessionStorage.removeItem('celerp_contact_selection'); window.location.reload(); }}, 800);
        }} else {{
          res.innerHTML = '<p class="flash flash--error">' + (d.detail || 'Delete failed') + '</p>';
        }}
      }}).catch(function(err){{
        document.getElementById('bulk-action-result').innerHTML = '<p class="flash flash--error">Delete failed: ' + err.message + '</p>';
      }});
      return;
    }}
    if (action === 'contact-merge'){{
      var tpl = document.getElementById('tpl-contact-merge');
      if (!tpl || !ctx) return;
      var clone = tpl.content.cloneNode(true);
      ctx.appendChild(clone);
      if (window.htmx) htmx.process(ctx);
      return;
    }}
  }};
  // Hook into CelerpSelection updates
  document.body.addEventListener('htmx:afterSwap', function(e){{
    if (e.detail && e.detail.target && e.detail.target.id === 'contacts-content'){{
      if (typeof CelerpSelection !== 'undefined') CelerpSelection.syncCheckboxes();
      updateContactBulkToolbar();
    }}
  }});
  document.addEventListener('change', function(e){{
    if (e.target && e.target.classList.contains('row-select')) updateContactBulkToolbar();
  }});
  document.body.addEventListener('celerpSelectionClear', updateContactBulkToolbar);
  updateContactBulkToolbar();
}})();
"""),
        title=f"{label} - Celerp",
        nav_active=nav_key,
        request=request,
    )



def _clean_external_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    import re
    m = re.match(r"gc:(customer|supplier|contact):(\d+)", ref)
    if m:
        return f"{m.group(1).title()} #{m.group(2)}"
    return ref


async def build_contact_detail(contact: dict, docs: list, vocab: list, company: dict, request: Request, *,
                         contact_id: str = "", show_financials: bool = True, show_delete: bool = True,
                         show_contact_addresses: bool = True, extra_sections: list | None = None,
                         back: tuple | None = None, nav_active: str | None = None,
                         title: str | None = None) -> FT:
    """Shared assembly for the contact detail page. The Company Details page reuses this verbatim with
    show_financials=False / show_delete=False / show_contact_addresses=False (the company is its own
    customer+vendor self-contact, and it has no financial-summary-against-itself) - one page layout, no
    fork. The only differences are the conditional sections gated by the flags."""
    contact_name = contact.get("name", "Contact")
    contact_type = contact.get("contact_type", "")
    if back is not None:
        back_label, back_href = back
        nav_active_key = nav_active or back_label.lower()
    elif contact_type == "vendor":
        back_label, back_href, nav_active_key = "Vendors", "/contacts/vendors", "vendors"
    else:
        back_label, back_href, nav_active_key = "Customers", "/contacts/customers", "customers"
    if nav_active:
        nav_active_key = nav_active
    cid = contact.get("entity_id") or contact.get("id") or contact_id
    is_deleted = bool(contact.get("deleted"))
    fiscal_year_start = company.get("fiscal_year_start") or "01-01"
    page_title = title or contact_name

    autofocus_script = (
        Script("document.querySelector('.cell--clickable')?.click();")
        if contact_name in ("New Customer", "New Vendor", "New Both", "New Contact") else ""
    )

    delete_btn = "" if (is_deleted or not show_delete) else Script(f"""
(function(){{
  var btn = document.getElementById('contact-delete-btn');
  if (!btn) return;
  btn.addEventListener('click', function(){{
    if (!confirm('Delete this contact? This cannot be undone if the contact has no associated documents.')) return;
    fetch('/crm/contacts/bulk/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{contact_ids: [{_json.dumps(cid)}]}})
    }}).then(function(r){{ return r.json(); }}).then(function(d){{
      if (d.deleted !== undefined) {{
        sessionStorage.removeItem('celerp_contact_selection');
        window.location.href = {_json.dumps(back_href)};
      }} else {{
        alert(d.detail || 'Delete failed.');
      }}
    }}).catch(function(err){{ alert('Delete failed: ' + err.message); }});
  }});
}})();
""")

    action_bar = Div(
        Button(t("btn.delete_contact"), id="contact-delete-btn", cls="btn btn--danger",
               type="button", style="margin-left:auto;") if (show_delete and not is_deleted) else
        (Span(t("label.deleted"), cls="status-badge status-badge--void") if is_deleted else ""),
        cls="action-bar",
        style="display:flex; align-items:center; padding: 0.5rem 0;",
    )

    return await base_shell(
        breadcrumbs([("Dashboard", "/dashboard"), (back_label, back_href), (page_title, None)]),
        page_header(page_title),
        autofocus_script,
        action_bar,
        delete_btn,
        *([_financial_summary(docs, contact_id=cid, fiscal_year_start=fiscal_year_start)] if show_financials else []),
        Div(
            Div(
                _contact_info_card(contact),
                _people_section(contact),
                cls="detail-col-left",
            ),
            Div(
                _settings_card(contact),
                _contact_tags_section(contact, vocab),
                cls="detail-col-right",
            ),
            cls="detail-layout",
        ),
        *([_addresses_section(contact)] if show_contact_addresses else []),
        *(extra_sections or []),
        _tab_bar(cid),
        Div(
            _documents_tab(docs, contact, cid),
            id="tab-content",
        ),
        title=f"{page_title} - Celerp",
        nav_active=nav_active_key,
        extra_head=_phone_head_items(),
        request=request,
    )


def setup_routes(app):

    # ── /contacts/customers ───────────────────────────────────────────────

    @app.get("/contacts/customers")
    async def customers_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        from ui.routes.settings import _check_permission
        denied = await _check_permission(request, "view_contacts")
        if denied:
            return denied
        q = request.query_params.get("q", "")
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "created_at")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
        except (ValueError, TypeError):
            per_page = _PER_PAGE

        params = {"limit": per_page, "offset": (page - 1) * per_page, "contact_type": "customer"}
        if q:
            params["q"] = q
        try:
            resp = await api.list_contacts(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            resp = {"items": [], "total": 0}
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}

        contacts = resp.get("items", [])
        total = resp.get("total", len(contacts))
        currency = company.get("currency")
        # Placeholder entries bubble to the top so users notice them
        _placeholder = f"New Customer"
        contacts = sorted(contacts, key=lambda c: (0 if (c.get("name") or "").strip() == _placeholder else 1))
        return await _contacts_page_shell("customer", contacts, request, q, page, total, per_page, sort, sort_dir, currency, settings=company.get("settings") or {})

    # ── /contacts/vendors ─────────────────────────────────────────────────

    @app.get("/contacts/vendors")
    async def vendors_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        from ui.routes.settings import _check_permission
        denied = await _check_permission(request, "view_contacts")
        if denied:
            return denied
        q = request.query_params.get("q", "")
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "created_at")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
        except (ValueError, TypeError):
            per_page = _PER_PAGE

        params = {"limit": per_page, "offset": (page - 1) * per_page, "contact_type": "vendor"}
        if q:
            params["q"] = q
        try:
            resp = await api.list_contacts(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            resp = {"items": [], "total": 0}
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}

        contacts = resp.get("items", [])
        total = resp.get("total", len(contacts))
        currency = company.get("currency")
        # Placeholder entries bubble to the top so users notice them
        _placeholder = "New Vendor"
        contacts = sorted(contacts, key=lambda c: (0 if (c.get("name") or "").strip() == _placeholder else 1))
        return await _contacts_page_shell("vendor", contacts, request, q, page, total, per_page, sort, sort_dir, currency, settings=company.get("settings") or {})

    # ── /contacts/search-options ──────────────────────────────────────────

    @app.get("/contacts/search-options")
    async def contacts_search_options(request: Request):
        """HTMX: combobox option fragments for the contact picker server-side search.

        Used by documents.py searchable_select (search_url= param).
        Returns empty body when q is blank so JS can restore the static list.
        When q is set, searches all contacts (no 500-cap) and returns option HTML.

        Query params:
          q            - search string
          contact_type - 'customer' or 'vendor' (default: 'customer')
          field        - 'contact_company_name' or anything else (default: contact_id behaviour)
        """
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R(status_code=200, content="")
        q = request.query_params.get("q", "").strip()
        if not q:
            return _R(status_code=200, content="")
        contact_type = request.query_params.get("contact_type", "customer")
        if contact_type not in ("customer", "vendor"):
            contact_type = "customer"
        field = request.query_params.get("field", "contact_id")
        try:
            resp = await api.list_contacts(token, {"q": q, "contact_type": contact_type})
            contacts = resp.get("items", [])
        except APIError:
            contacts = []

        if field == "contact_company_name":
            opts = [
                (c.get("entity_id") or c.get("id") or "", c.get("company_name") or c.get("name") or "")
                for c in contacts if c.get("company_name")
            ]
        else:
            opts = [
                (c.get("entity_id") or c.get("id") or "", c.get("name") or c.get("entity_id") or c.get("id") or "")
                for c in contacts
            ]
            opts.append(("__new__", "+ Add new contact"))

        opt_els = [
            Div(label, cls="combobox-option" + (" combobox-option--new" if val.startswith("__new__") else ""), data_value=val)
            for val, label in opts
        ]
        has_real_contacts = any(not val.startswith("__new__") for val, _ in opts)
        opt_els.append(Div(t("msg.no_results"), cls="combobox-option combobox-option--empty",
                           style="display:none" if has_real_contacts else ""))
        from fasthtml.common import to_xml
        return _R(content="".join(to_xml(el) for el in opt_els), media_type="text/html")

    # ── /contacts/search ─────────────────────────────────────────────────

    @app.get("/contacts/search")
    async def contacts_search(request: Request):
        """Legacy search endpoint — delegates to /contacts/content."""
        return RedirectResponse(f"/contacts/content?{request.query_params}", status_code=302)

    # ── /contacts/content ────────────────────────────────────────────────

    @app.get("/contacts/content")
    async def contacts_content(request: Request):
        """HTMX partial: returns #contacts-content fragment for sort/search/filter."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        contact_type = request.query_params.get("type", "customer")
        if contact_type not in ("customer", "vendor"):
            contact_type = "customer"
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "created_at")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
        except (ValueError, TypeError):
            per_page = _PER_PAGE

        params = {"limit": per_page, "offset": (page - 1) * per_page, "contact_type": contact_type}
        if q:
            params["q"] = q
        try:
            resp = await api.list_contacts(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            resp = {"items": [], "total": 0}
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}

        contacts = resp.get("items", [])
        total = resp.get("total", len(contacts))
        currency = company.get("currency")
        # Placeholder entries bubble to the top so users notice them
        _placeholder = f"New {contact_type.title()}"
        contacts = sorted(contacts, key=lambda c: (0 if (c.get("name") or "").strip() == _placeholder else 1))
        return _contacts_content(contact_type, contacts, q, page, total, per_page, sort, sort_dir, currency)

    # ── /contacts/create ─────────────────────────────────────────────────

    @app.post("/contacts/create")
    async def create_contact_route(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        contact_type = request.query_params.get("type", "customer")
        if contact_type not in ("customer", "vendor", "both"):
            contact_type = "customer"
        default_name = f"New {contact_type.title()}"
        # Prevent duplicate placeholder entries
        try:
            existing_resp = await api.list_contacts(token, {"contact_type": contact_type, "limit": 500})
            existing = existing_resp.get("items", [])
            dup = next((c for c in existing if (c.get("name") or "").strip() == default_name), None)
            if dup:
                dup_id = dup.get("entity_id") or dup.get("id", "")
                return _R("", status_code=204, headers={"HX-Redirect": f"/contacts/{dup_id}"})
        except APIError:
            pass
        try:
            result = await api.create_contact(token, {"name": default_name, "contact_type": contact_type})
            contact_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _R("", status_code=500)
        return _R("", status_code=204, headers={"HX-Redirect": f"/contacts/{contact_id}"})

    # ── /contacts/sales (Sales Funnel page — registered before {contact_id} catch-all) ──
    # Route lives here to ensure correct ordering before {contact_id} catch-all.
    # Kanban builder is provided by celerp-sales-funnel if installed, otherwise a stub.

    @app.get("/contacts/sales")
    async def sales_funnel_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        show_closed = request.query_params.get("show_closed", "") == "1"
        try:
            from celerp_sales_funnel.ui_routes import _build_kanban
            kanban = await _build_kanban(token, show_closed)
            sfn_installed = True
        except ImportError:
            kanban = P(t("label.sales_funnel_module_not_installed"), cls="empty-state-msg")
            sfn_installed = False
        toggle = A(
            "Hide closed" if show_closed else "Show closed",
            href=f"/contacts/sales{'?show_closed=1' if not show_closed else ''}",
            cls="btn btn--secondary btn--sm",
        ) if sfn_installed else ""
        return await base_shell(
            page_header(
                "Sales Funnel",
                A(t("page.new_deal"), href="/crm/deals/new", cls="btn btn--primary") if sfn_installed else "",
                toggle,
            ),
            kanban,
            Span("", id="deal-error"),
            title="Sales Funnel - Celerp",
            nav_active="sales-funnel",
            request=request,
        )

    # ── /contacts/{contact_id} ────────────────────────────────────────────

    @app.get("/contacts/{contact_id}")
    async def contact_detail(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            contact = await api.get_contact(token, contact_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            contact = {}
        try:
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
            docs = docs_resp.get("items", []) if isinstance(docs_resp, dict) else docs_resp
        except Exception:
            docs = []
        try:
            vocab = await api.get_contact_tags_vocabulary(token)
        except Exception:
            vocab = []
        try:
            company = await api.get_company(token)
        except Exception:
            company = {}

        return await build_contact_detail(contact, docs, vocab, company, request, contact_id=contact_id)

    # ── Company Details (Finance) ─────────────────────────────────────────
    # The company is its own customer+vendor self-contact, so this page is the same contact-detail
    # layout reused verbatim, minus the financial-summary cards (no financials against yourself) and
    # the delete affordance. Identity edits here flow to every customer/vendor context at once.

    async def _resolve_self_contact_id(token: str) -> str | None:
        """The company's own contact id: the cached settings value, else the contact flagged is_self."""
        try:
            company = await api.get_company(token)
            sid = (company.get("settings") or {}).get("self_contact_id")
            if sid:
                return sid
        except Exception:
            pass
        try:
            items = (await api.list_contacts(token, {"limit": 999})).get("items", [])
            for c in items:
                if c.get("is_self"):
                    return c.get("id") or c.get("entity_id")
        except Exception:
            pass
        return None

    @app.get("/finance/company-details")
    async def company_details(request: Request):
        from ui.routes.settings import _check_permission
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_company_settings")):
            return r
        sid = await _resolve_self_contact_id(token)
        if not sid:
            return await base_shell(
                page_header("🏢 Company Details"),
                empty_state_cta("Your company's own contact record has not been initialised yet."),
                title="Company Details - Celerp", nav_active="company-details", request=request,
            )
        try:
            contact = await api.get_contact(token, sid)
        except Exception:
            contact = {}
        try:
            company = await api.get_company(token)
        except Exception:
            company = {}
        # Organized like a customer/vendor page: Contact Info card (the company's identity - name, email,
        # phone, tax id) on the left, a Settings card (currency/timezone/fiscal) on the right, then the
        # billing/shipping address book, then tabs. Files is the FIRST tab (the company's whole file
        # library, with a quick-upload dropzone); Documents/Notes/Activity follow. No tags / financial cards.
        from ui.routes.settings import _company_settings_card
        from ui.i18n import get_lang
        return await base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Company Details", None)]),
            page_header("🏢 Company Details"),
            Div(
                Div(_contact_info_card(contact, hide_fields=("currency", "billing_address", "shipping_address")),
                    cls="detail-col-left"),
                Div(_company_settings_card(company, get_lang(request)), cls="detail-col-right"),
                cls="detail-layout",
            ),
            _addresses_section(contact),
            _tab_bar(sid, active="company-files",
                     lead_tabs=[("company-files", "Files", "/finance/company-files/_section")]),
            Div(await _company_files_section(token), id="tab-content"),
            title="Company Details - Celerp", nav_active="company-details",
            extra_head=_phone_head_items(), request=request,
        )

    async def _company_files_section(token: str, **section_kwargs) -> FT:
        """Aggregate + render the read-only company-wide files section (company docs + item images +
        doc attachments). Shared by the page and its sort/pagination _section route."""
        sid = await _resolve_self_contact_id(token)
        self_contact: dict = {}
        if sid:
            try:
                self_contact = await api.get_contact(token, sid) or {}
            except Exception:
                self_contact = {}
        try:
            items = (await api.list_items(token, {"limit": 9999})).get("items", [])
        except Exception:
            items = []
        try:
            docs = (await api.list_docs(token, {"limit": 9999})).get("items", [])
        except Exception:
            docs = []
        files = _collect_company_files(self_contact, items, docs)
        # can_upload=True: the dropzone POSTs to base_url (/finance/company-files), which routes the file
        # onto the self-contact so it shows here AND in the company's customer/vendor entries. Item and
        # doc rows stay read-only via their per-row _no_delete override.
        return _shared_files_section(
            "company", "all", files, can_tag=False, can_describe=False, can_set_hero=False,
            can_upload=True, hide_product_images=True, show_linked=True, base_url="/finance/company-files",
            title="Company files", **section_kwargs,
        )

    # Company files live as the first tab on the Company Details page (loaded via the _section route
    # below + the POST upload route), so there is no standalone page.

    @app.post("/finance/company-files")
    async def company_files_upload(request: Request):
        """Dropzone upload from the Company Files page: store the file on the self-contact so it appears
        both here and in the company's customer/vendor entries. Returns the refreshed aggregated section."""
        from ui.routes.settings import _check_permission
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        if (r := await _check_permission(request, "manage_company_settings")):
            return r
        sid = await _resolve_self_contact_id(token)
        form = await request.form()
        file = form.get("file")
        if file is not None and hasattr(file, "read") and sid:
            content = await file.read()
            filename = getattr(file, "filename", "upload")
            content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
            try:
                await api.upload_contact_file(token, sid, content, filename, content_type,
                                              str(form.get("description", "")).strip(),
                                              str(form.get("document_tag", "")).strip())
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
        return await _company_files_section(token)

    @app.get("/finance/company-files/_section")
    async def company_files_section(request: Request, page: int = 1, sort_dir: str = "desc",
                                    tag_filter: str = "", date_from: str = "", date_to: str = "", search: str = ""):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await _company_files_section(token, page=page, sort_dir=sort_dir, tag_filter=tag_filter,
                                            date_from=date_from, date_to=date_to, search=search)

    # ── Tab routes ────────────────────────────────────────────────────────

    @app.get("/contacts/{contact_id}/tab/documents")
    async def contact_tab_documents(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
            docs = docs_resp.get("items", []) if isinstance(docs_resp, dict) else docs_resp
        except Exception:
            docs = []
        try:
            contact = await api.get_contact(token, contact_id)
        except Exception:
            contact = {}
        cid = contact.get("entity_id") or contact.get("id") or contact_id
        # The company self-contact has its own Files tab, so its Documents tab is docs-only (no dup files).
        return _documents_tab(docs, contact, cid, show_files=not contact.get("is_self"))

    @app.get("/contacts/{contact_id}/tab/notes")
    async def contact_tab_notes(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            notes = await api.list_contact_notes(token, contact_id)
        except APIError:
            notes = []
        tz = await _company_timezone(token)
        return _notes_tab(contact_id, notes, tz)

    @app.get("/contacts/{contact_id}/tab/activity")
    async def contact_tab_activity(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            ledger_resp = await api.list_ledger(token, {"entity_id": contact_id, "limit": 10})
            ledger = ledger_resp.get("items", []) if isinstance(ledger_resp, dict) else []
        except Exception:
            ledger = []
        return _contact_ledger_table(ledger)

    # ── Notes routes ─────────────────────────────────────────────────────

    @app.post("/contacts/{contact_id}/notes")
    async def contact_add_note(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        text = str(form.get("note", "")).strip()
        if text:
            try:
                await api.add_contact_note(token, contact_id, {"note": text})
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
        try:
            notes = await api.list_contact_notes(token, contact_id)
        except APIError:
            notes = []
        tz = await _company_timezone(token)
        return _notes_tab(contact_id, notes, tz)

    @app.get("/contacts/{contact_id}/notes/{note_id}/edit")
    async def contact_note_edit_form(request: Request, contact_id: str, note_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            notes = await api.list_contact_notes(token, contact_id)
        except APIError:
            notes = []
        note = next((n for n in notes if (n.get("note_id") or n.get("id")) == note_id), {})
        return _shared_note_edit_form(
            note_id=note_id,
            current_text=note.get("note", ""),
            save_url=f"/contacts/{contact_id}/notes/{note_id}",
            cancel_url=f"/contacts/{contact_id}/tab/notes",
            refresh_target="#tab-content",
        )

    @app.patch("/contacts/{contact_id}/notes/{note_id}")
    async def contact_edit_note(request: Request, contact_id: str, note_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        text = str(form.get("note", "")).strip()
        if text:
            try:
                await api.update_contact_note(token, contact_id, note_id, {"note": text})
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
        try:
            notes = await api.list_contact_notes(token, contact_id)
        except APIError:
            notes = []
        tz = await _company_timezone(token)
        return _notes_tab(contact_id, notes, tz)

    @app.delete("/contacts/{contact_id}/notes/{note_id}")
    async def contact_delete_note(request: Request, contact_id: str, note_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.delete_contact_note(token, contact_id, note_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        try:
            notes = await api.list_contact_notes(token, contact_id)
        except APIError:
            notes = []
        tz = await _company_timezone(token)
        return _notes_tab(contact_id, notes, tz)

    # ── Address routes ────────────────────────────────────────────────────

    @app.get("/contacts/{contact_id}/addresses/new")
    async def contact_address_new_form(request: Request, contact_id: str):
        addr_type = request.query_params.get("type", "billing")
        if addr_type not in ("billing", "shipping"):
            addr_type = "billing"
        target_div = f"addr-new-{addr_type}"
        return Div(
            Form(
                Div(
                    Div(Label(t("label.line_1"), cls="form-label"), Input(type="text", name="line1", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.line_2"), cls="form-label"), Input(type="text", name="line2", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(Label(t("label.city"), cls="form-label"), Input(type="text", name="city", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.state"), cls="form-label"), Input(type="text", name="state", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.postal_code"), cls="form-label"), Input(type="text", name="postal_code", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.country"), cls="form-label"), Input(type="text", name="country", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.attn"), cls="form-label"), Input(type="text", name="attn", placeholder="Attention / recipient name", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Input(type="hidden", name="address_type", value=addr_type),
                Div(Label(t("label.default_address"), cls="form-label"),
                    Input(type="checkbox", name="is_default", value="true", cls="form-checkbox"),
                    cls="form-group form-group--inline"),
                Div(
                    Button(t("btn.save"), type="submit", cls="btn btn--primary btn--sm"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--sm", onclick=f"document.getElementById('{target_div}').innerHTML=''"),
                    cls="form-row",
                ),
                hx_post=f"/contacts/{contact_id}/addresses",
                hx_target="#addresses-section",
                hx_swap="outerHTML",
                hx_trigger="submit",
            ),
            cls="inline-form",
        )

    @app.post("/contacts/{contact_id}/addresses")
    async def contact_address_create(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        data = {k: str(form.get(k, "")).strip() for k in ("address_type", "line1", "line2", "city", "state", "postal_code", "country", "attn")}
        data = {k: v for k, v in data.items() if v}
        is_default = str(form.get("is_default", "")).strip().lower() in ("true", "on", "1")
        data["is_default"] = is_default
        try:
            await api.add_contact_address(token, contact_id, data)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _addresses_section(contact), _contact_info_card(contact, oob=True)

    @app.get("/contacts/{contact_id}/addresses/{address_id}/edit")
    async def contact_address_edit_form(request: Request, contact_id: str, address_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            contact = await api.get_contact(token, contact_id)
        except APIError:
            contact = {}
        addresses = list(contact.get("addresses") or [])
        addr = next((a for a in addresses if str(a.get("address_id", "")) == address_id), {})
        return Div(
            Form(
                Div(
                    Div(Label(t("label.line_1"), cls="form-label"), Input(type="text", name="line1", value=addr.get("line1", ""), cls="form-input"), cls="form-group"),
                    Div(Label(t("label.line_2"), cls="form-label"), Input(type="text", name="line2", value=addr.get("line2", ""), cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(Label(t("label.city"), cls="form-label"), Input(type="text", name="city", value=addr.get("city", ""), cls="form-input"), cls="form-group"),
                    Div(Label(t("label.state"), cls="form-label"), Input(type="text", name="state", value=addr.get("state", ""), cls="form-input"), cls="form-group"),
                    Div(Label(t("label.postal_code"), cls="form-label"), Input(type="text", name="postal_code", value=addr.get("postal_code", ""), cls="form-input"), cls="form-group"),
                    Div(Label(t("label.country"), cls="form-label"), Input(type="text", name="country", value=addr.get("country", ""), cls="form-input"), cls="form-group"),
                    Div(Label(t("label.attn"), cls="form-label"), Input(type="text", name="attn", value=addr.get("attn", ""), placeholder="Attention / recipient name", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(Label(t("label.default_address"), cls="form-label"),
                    Input(type="checkbox", name="is_default", value="true",
                          checked=bool(addr.get("is_default")), cls="form-checkbox"),
                    cls="form-group form-group--inline"),
                Div(
                    Button(t("btn.save"), type="submit", cls="btn btn--primary btn--sm"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--sm",
                           onclick=f"htmx.ajax('GET',{_json.dumps(f'/contacts/{contact_id}/addresses/section')},'#addresses-section',{{swap:'outerHTML'}})"),
                    cls="form-row",
                ),
                hx_patch=f"/contacts/{contact_id}/addresses/{address_id}",
                hx_target="#addresses-section",
                hx_swap="outerHTML",
                hx_trigger="submit",
            ),
            cls="inline-form", id=f"addr-{address_id.replace(':', '-')}",
        )

    @app.patch("/contacts/{contact_id}/addresses/{address_id}")
    async def contact_address_update(request: Request, contact_id: str, address_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        data = {k: str(form.get(k, "")).strip() for k in ("address_type", "line1", "line2", "city", "state", "postal_code", "country", "attn")}
        data = {k: v for k, v in data.items() if v}
        is_default = str(form.get("is_default", "")).strip().lower() in ("true", "on", "1")
        data["is_default"] = is_default
        try:
            await api.update_contact_address(token, contact_id, address_id, data)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _addresses_section(contact), _contact_info_card(contact, oob=True)

    @app.delete("/contacts/{contact_id}/addresses/{address_id}")
    async def contact_address_delete(request: Request, contact_id: str, address_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            contact = await api.get_contact(token, contact_id)
        except APIError:
            contact = {}
        # Never delete the primary address - the contact (incl. the company self-contact) must always keep
        # a primary billing address. The UI hides the delete button on it; this guards the route too.
        target = next((a for a in (contact.get("addresses") or []) if str(a.get("address_id", "")) == address_id), None)
        if target is not None and target.get("is_default"):
            return P("Make another address primary before removing this one.", cls="cell-error")
        try:
            await api.remove_contact_address(token, contact_id, address_id)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _addresses_section(contact), _contact_info_card(contact, oob=True)

    # ── People routes ─────────────────────────────────────────────────────

    @app.get("/contacts/{contact_id}/addresses/section")
    async def contact_addresses_section(request: Request, contact_id: str):
        """Re-render just the addresses section (used by cancel buttons in edit forms)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            contact = await api.get_contact(token, contact_id)
        except APIError:
            contact = {}
        return _addresses_section(contact)

    @app.post("/contacts/{contact_id}/addresses/{address_id}/make-primary")
    async def contact_address_make_primary(request: Request, contact_id: str, address_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.update_contact_address(token, contact_id, address_id, {"is_default": True})
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _addresses_section(contact), _contact_info_card(contact, oob=True)

    @app.get("/contacts/{contact_id}/people/new")
    async def contact_person_new_form(request: Request, contact_id: str):
        return Div(
            Form(
                Div(
                    Div(Label(t("th.name"), cls="form-label"), Input(type="text", name="name", cls="form-input", required=True), cls="form-group"),
                    Div(Label(t("th.role"), cls="form-label"), Input(type="text", name="role", placeholder="e.g. Sales, AP", cls="form-input"), cls="form-group"),
                    Div(Label(t("th.email"), cls="form-label"), Input(type="email", name="email", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(Label(t("th.phone"), cls="form-label"), Input(type="text", name="phone", cls="form-input"), cls="form-group"),
                    Div(Label(Input(type="checkbox", name="is_primary", value="true"), " Primary contact"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Button(t("btn.save"), type="submit", cls="btn btn--primary btn--sm"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--sm", onclick="this.closest('.inline-form').remove()"),
                    cls="form-row",
                ),
                hx_post=f"/contacts/{contact_id}/people",
                hx_target="#people-section",
                hx_swap="outerHTML",
            ),
            cls="inline-form",
        )

    @app.post("/contacts/{contact_id}/people")
    async def contact_person_create(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        data = {k: str(form.get(k, "")).strip() for k in ("name", "role", "email", "phone")}
        data = {k: v for k, v in data.items() if v}
        if form.get("is_primary"):
            data["is_primary"] = True
        try:
            await api.add_contact_person(token, contact_id, data)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        people = _people_section(contact)
        if form.get("is_primary"):
            info_card = _contact_info_card(contact)
            info_card.attrs["hx-swap-oob"] = "outerHTML:#contact-info-card"
            return Div(people, info_card)
        return people

    @app.get("/contacts/{contact_id}/people/{person_id}/edit")
    async def contact_person_edit_form(request: Request, contact_id: str, person_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            contact = await api.get_contact(token, contact_id)
        except APIError:
            contact = {}
        people = list(contact.get("people") or [])
        person = next((p for p in people if str(p.get("person_id", "")) == person_id), {})
        return Div(
            Form(
                Div(
                    Div(Label(t("th.name"), cls="form-label"), Input(type="text", name="name", value=person.get("name", ""), cls="form-input", required=True), cls="form-group"),
                    Div(Label(t("th.role"), cls="form-label"), Input(type="text", name="role", value=person.get("role", ""), cls="form-input"), cls="form-group"),
                    Div(Label(t("th.email"), cls="form-label"), Input(type="email", name="email", value=person.get("email", ""), cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(Label(t("th.phone"), cls="form-label"), Input(type="text", name="phone", value=person.get("phone", ""), cls="form-input"), cls="form-group"),
                    Div(Label(Input(type="checkbox", name="is_primary", value="true", checked=bool(person.get("is_primary"))), " Primary contact"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Button(t("btn.save"), type="submit", cls="btn btn--primary btn--sm"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--sm", onclick=f"htmx.ajax('GET','/contacts/{contact_id}/tab/documents','#tab-content');location.reload()"),
                    cls="form-row",
                ),
                hx_patch=f"/contacts/{contact_id}/people/{person_id}",
                hx_target="#people-section",
                hx_swap="outerHTML",
            ),
            cls="inline-form", id=f"person-{person_id}",
        )

    @app.patch("/contacts/{contact_id}/people/{person_id}")
    async def contact_person_update(request: Request, contact_id: str, person_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        data = {k: str(form.get(k, "")).strip() for k in ("name", "role", "email", "phone")}
        data = {k: v for k, v in data.items() if v}
        data["is_primary"] = bool(form.get("is_primary"))
        try:
            await api.update_contact_person(token, contact_id, person_id, data)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        people = _people_section(contact)
        if data["is_primary"]:
            info_card = _contact_info_card(contact)
            info_card.attrs["hx-swap-oob"] = "outerHTML:#contact-info-card"
            return Div(people, info_card)
        return people

    @app.delete("/contacts/{contact_id}/people/{person_id}")
    async def contact_person_delete(request: Request, contact_id: str, person_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.remove_contact_person(token, contact_id, person_id)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _people_section(contact)

    @app.get("/contacts/{contact_id}/field/{field}/edit")
    async def contact_field_edit(request: Request, contact_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            _settings = (await api.get_company(token)).get("settings") or {}
        except APIError:
            _settings = {}
        if not role_has_permission(_settings, _get_role(request), "edit_contacts"):
            # Viewers are read-only: return the display state instead of an input.
            return await contact_field_display(request, contact_id, field)
        try:
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        if field not in _EDITABLE:
            return P(t("label.not_editable"), cls="cell-error")

        val = str(contact.get(field, "") or "")
        _esc_js = (
            "if(event.key==='Escape'){"
            f"htmx.ajax('GET','/contacts/{contact_id}/field/{field}/display',"
            "{target:this.closest('td'),swap:'outerHTML'});event.preventDefault()}"
        )

        if field == "contact_type":
            input_el = Select(
                Option("customer", value="customer", selected=val == "customer"),
                Option("vendor", value="vendor", selected=val == "vendor"),
                Option("both", value="both", selected=val == "both"),
                name="value",
                hx_patch=f"/contacts/{contact_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=_esc_js,
            )
        elif field == "price_list":
            try:
                price_lists = await api.get_price_lists(token)
            except APIError:
                price_lists = []
            pl_names = [pl.get("name", "") for pl in price_lists]
            input_el = Select(
                Option(t("label._company_default"), value=""),
                *[Option(name, value=name, selected=(name == val)) for name in pl_names],
                name="value",
                hx_patch=f"/contacts/{contact_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=_esc_js,
            )
        elif field == "payment_terms":
            try:
                terms = await api.get_payment_terms(token)
            except APIError:
                terms = []
            term_names = [term.get("name", "") for term in terms]
            input_el = Select(
                Option("-- Select --", value=""),
                *[Option(name, value=name, selected=(name == val)) for name in term_names],
                Option(t("label._add_new"), value="__add_new__"),
                name="value",
                hx_patch=f"/contacts/{contact_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=_esc_js,
                onchange="if(this.value==='__add_new__'){window.location.href='/settings/contacts?tab=payment-terms';return false;}",
            )
        elif field == "phone":
            return phone_input_td(
                value=val,
                patch_url=f"/contacts/{contact_id}/field/phone",
                cancel_url=f"/contacts/{contact_id}/field/phone/display",
                field_id="contact-phone",
            )
        elif field == "currency":
            return currency_combobox_td(
                value=val,
                hidden_id="contact-currency-input",
                patch_url=f"/contacts/{contact_id}/field/currency",
                cancel_url=f"/contacts/{contact_id}/field/currency/display",
            )
        else:
            # Guard: ignore blur within 350ms of mount to prevent dblclick's
            # second mouseup from immediately closing the editor.
            input_el = Input(
                type="number" if field == "credit_limit" else "text",
                name="value", value=val,
                id=f"edit-{field}",
                cls="cell-input", autofocus=True,
                onkeydown=(
                    "if(event.key==='Escape'){this._escaping=true;"
                    f"htmx.ajax('GET','/contacts/{contact_id}/field/{field}/display',"
                    "{target:this.closest('td'),swap:'outerHTML'});event.preventDefault()}"
                    "else if(event.key==='Enter'){event.preventDefault();this.blur()}"
                ),
            )
            blur_guard_js = Script(
                "requestAnimationFrame(()=>{"
                f"var el=document.getElementById('edit-{field}');"
                "if(!el)return;el._ready=false;"
                "setTimeout(()=>{el._ready=true;el.focus()},350);"
                "el.addEventListener('blur',function(e){"
                "if(!el._ready||el._escaping)return;"
                f"htmx.ajax('PATCH','/contacts/{contact_id}/field/{field}',"
                "{target:el.closest('td'),swap:'outerHTML',values:{value:el.value}})"
                "},{once:true})"
                "})"
            )
            return Td(input_el, blur_guard_js, cls="cell cell--editing")
        return Td(input_el, cls="cell cell--editing")

    @app.get("/contacts/{contact_id}/field/{field}/display")
    async def contact_field_display(request: Request, contact_id: str, field: str):
        """Return the read-only display cell (used by ESC cancel)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _contact_display_cell(contact_id, field, contact.get(field))

    @app.patch("/contacts/{contact_id}/field/{field}")
    async def contact_field_patch(request: Request, contact_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        if field not in _EDITABLE:
            return P(t("label.not_editable"), cls="cell-error")
        if field == "currency" and value and value not in _CURRENCY_CODES:
            return P(f"Invalid currency code: {value!r}", cls="cell-error")
        data = {field: float(value) if field == "credit_limit" and value else value}
        try:
            await api.patch_contact(token, contact_id, data)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _contact_display_cell(contact_id, field, contact.get(field))

    @app.post("/contacts/{contact_id}/tags/add")
    async def contact_add_tag(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        tag = str(form.get("tag", "")).strip()
        if not tag:
            try:
                contact = await api.get_contact(token, contact_id)
            except APIError as e:
                logger.warning("API error fetching contact %s for tags: %s", contact_id, e.detail)
                contact = {}
            try:
                vocab = await api.get_contact_tags_vocabulary(token)
            except Exception:
                vocab = []
            return _contact_tags_section(contact, vocab)
        try:
            # Auto-add to vocabulary if not already present
            try:
                vocab = await api.get_contact_tags_vocabulary(token)
            except Exception:
                vocab = []
            if not any(item.get("name") == tag for item in vocab):
                vocab.append({"name": tag, "color": None, "category": None})
                try:
                    await api.patch_contact_tags_vocabulary(token, vocab)
                except Exception:
                    pass  # Non-critical: tag still gets added to contact
            await api.add_contact_tags(token, contact_id, [tag])
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _contact_tags_section(contact, vocab)

    @app.post("/contacts/{contact_id}/tags/remove")
    async def contact_remove_tag(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        tag = str(form.get("tag", "")).strip()
        try:
            contact = await api.get_contact(token, contact_id)
            current_tags = list(contact.get("tags") or [])
            remaining = [t for item in current_tags if t != tag]
            await api.patch_contact(token, contact_id, {"tags": remaining})
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        try:
            vocab = await api.get_contact_tags_vocabulary(token)
        except Exception:
            vocab = []
        return _contact_tags_section(contact, vocab)

    # ── File routes ───────────────────────────────────────────────────────

    @app.post("/contacts/{contact_id}/files")
    async def contact_upload_file(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            try:
                contact = await api.get_contact(token, contact_id)
                docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
            except APIError:
                contact = {"entity_id": contact_id}
                docs_resp = {"items": []}
            return _files_section(contact, contact_id, docs_resp.get("items", []))
        description = str(form.get("description", "")).strip()
        document_tag = str(form.get("document_tag", "")).strip()
        content = await file.read()
        filename = getattr(file, "filename", "upload")
        content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
        try:
            await api.upload_contact_file(token, contact_id, content, filename, content_type, description, document_tag)
            contact = await api.get_contact(token, contact_id)
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id, docs_resp.get("items", []))

    @app.delete("/contacts/{contact_id}/files/{file_id}")
    async def contact_delete_file(request: Request, contact_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.delete_contact_file(token, contact_id, file_id)
            contact = await api.get_contact(token, contact_id)
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id, docs_resp.get("items", []))

    @app.post("/contacts/{contact_id}/files/{file_id}/tag")
    async def contact_tag_file(request: Request, contact_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        document_tag = str(form.get("document_tag", "")).strip()
        try:
            await api.tag_contact_file(token, contact_id, file_id, document_tag)
            contact = await api.get_contact(token, contact_id)
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id, docs_resp.get("items", []))

    @app.post("/contacts/{contact_id}/files/{file_id}/description")
    async def contact_patch_file_description(request: Request, contact_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        description = str(form.get("description", "")).strip()
        try:
            await api.patch_contact_file_description(token, contact_id, file_id, description)
            contact = await api.get_contact(token, contact_id)
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id, docs_resp.get("items", []))

    @app.get("/contacts/{contact_id}/files/_section")
    async def contact_files_section(request: Request, contact_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        qp = request.query_params
        try:
            contact = await api.get_contact(token, contact_id)
            docs_resp = await api.list_contact_docs(token, contact_id, {"limit": 999})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id, docs_resp.get("items", []),
            page=int(qp.get("page", "1") or "1"),
            sort_dir=qp.get("sort_dir", "desc"),
            tag_filter=qp.get("tag_filter", ""),
            date_from=qp.get("date_from", ""),
            date_to=qp.get("date_to", ""),
            search=qp.get("search", ""),
        )

    @app.get("/contacts/{contact_id}/files/{file_id}/download")
    async def contact_download_file(request: Request, contact_id: str, file_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            resp = await api.download_contact_file(token, contact_id, file_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            from starlette.responses import Response as _R
            return _R(str(e.detail), status_code=e.status)
        content_type = resp.headers.get("content-type", "application/octet-stream")
        # Extract filename from content-disposition header if present
        cd = resp.headers.get("content-disposition", "")
        filename = "download"
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"').strip("'")
        from starlette.responses import Response as _R
        return _R(
            content=resp.content,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ── CSV export for contacts ───────────────────────────────────────────

    @app.get("/contacts/customers/export/csv")
    async def customers_export_csv(request: Request):
        qs = request.query_params
        selected_qs = "&".join(f"selected={v}" for v in qs.getlist("selected"))
        base = "/crm/export/csv?contact_type=customer"
        return RedirectResponse(base + ("&" + selected_qs if selected_qs else ""), status_code=302)

    @app.get("/contacts/vendors/export/csv")
    async def vendors_export_csv(request: Request):
        qs = request.query_params
        selected_qs = "&".join(f"selected={v}" for v in qs.getlist("selected"))
        base = "/crm/export/csv?contact_type=vendor"
        return RedirectResponse(base + ("&" + selected_qs if selected_qs else ""), status_code=302)

    # ── Backward compat: /crm redirects ──────────────────────────────────

    @app.get("/crm")
    async def crm_redirect(request: Request):
        return RedirectResponse("/contacts/customers", status_code=302)

    @app.get("/crm/new")
    async def crm_new_redirect(request: Request):
        return RedirectResponse("/contacts/customers", status_code=302)

    @app.get("/crm/search")
    async def crm_search_redirect(request: Request):
        q = request.query_params.get("q", "")
        return RedirectResponse(f"/contacts/search?q={q}", status_code=302)

    @app.get("/crm/export/csv")
    async def crm_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        selected = request.query_params.getlist("selected")
        params: dict = {}
        if q:
            params["q"] = q
        if selected:
            params["selected"] = selected
        try:
            data = await api.export_contacts_csv(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = b"error\n"
        from starlette.responses import Response
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=contacts.csv"},
        )

    # ── CSV import (kept at original URL for backward compat) ─────────────

    from ui.routes.csv_import import (
        CsvImportSpec, _resolve_csv_text, _rows_to_csv, _stash_csv,
        upload_form as _csv_upload_form, validate_cell as _csv_validate_cell,
        read_csv_upload as _csv_read_upload, validation_result as _csv_validation_result,
        error_report_response as _csv_error_report, apply_fixes_to_rows as _csv_apply_fixes,
        column_mapping_form as _csv_column_mapping_form,
        validate_column_mapping as _csv_validate_column_mapping,
        apply_column_mapping as _csv_apply_column_mapping,
        import_result_panel as _csv_import_result_panel,
    )

    _CONTACT_IMPORT_SPEC = CsvImportSpec(
        cols=["name", "company_name", "website", "currency", "phone", "email", "billing_address", "tax_id", "credit_limit", "contact_type", "payment_terms"],
        required={"name"},
        type_map={"credit_limit": float},
    )

    @app.get("/crm/import/contacts")
    async def crm_import_contacts_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await base_shell(
            page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
            _csv_upload_form(
                cols=_CONTACT_IMPORT_SPEC.cols,
                template_href="/crm/import/contacts/template",
                preview_action="/crm/import/contacts/preview",
                has_mapping=True,
            ),
            title="Import Contacts - Celerp",
            nav_active="customers",
            request=request,
        )

    @app.get("/crm/import/contacts/template")
    async def crm_import_contacts_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        import csv, io
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=_CONTACT_IMPORT_SPEC.cols)
        writer.writeheader()
        writer.writerow({"name": "", "phone": "", "email": "", "billing_address": "", "tax_id": "", "credit_limit": "", "contact_type": "customer", "payment_terms": ""})
        from starlette.responses import Response
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=contacts_template.csv"},
        )

    @app.post("/crm/import/contacts/preview")
    async def crm_import_contacts_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await _csv_read_upload(form)
        if err:
            return await base_shell(
                page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
                _csv_upload_form(
                    cols=_CONTACT_IMPORT_SPEC.cols,
                    template_href="/crm/import/contacts/template",
                    preview_action="/crm/import/contacts/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Contacts - Celerp",
                nav_active="customers",
                request=request,
            )
        import csv as _csv_mod, io as _io
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return await base_shell(
            page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
            _csv_column_mapping_form(
                csv_cols=cols,
                target_cols=_CONTACT_IMPORT_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/crm/import/contacts/mapped",
                back_href="/crm/import/contacts",
                required_targets=_CONTACT_IMPORT_SPEC.required,
            ),
            title="Import Contacts - Celerp",
            nav_active="customers",
            request=request,
        )

    @app.post("/crm/import/contacts/mapped")
    async def crm_import_contacts_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        import csv as _csv_mod, io as _io
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return await base_shell(
                page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
                _csv_upload_form(
                    cols=_CONTACT_IMPORT_SPEC.cols,
                    template_href="/crm/import/contacts/template",
                    preview_action="/crm/import/contacts/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Contacts - Celerp",
                nav_active="customers",
                request=request,
            )

        original_cols = list(_csv_mod.DictReader(_io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = _csv_validate_column_mapping(form, original_cols, core_fields=set(_CONTACT_IMPORT_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(_csv_mod.DictReader(_io.StringIO(csv_text)))
            return await base_shell(
                page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
                _csv_column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_CONTACT_IMPORT_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/crm/import/contacts/mapped",
                    back_href="/crm/import/contacts",
                    required_targets=_CONTACT_IMPORT_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Contacts - Celerp",
                nav_active="customers",
                request=request,
            )

        remapped_csv, remapped_cols = _csv_apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(_csv_mod.DictReader(_io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _CONTACT_IMPORT_SPEC.cols)

        return await base_shell(
            page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
            _csv_validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v, r: _csv_validate_cell(_CONTACT_IMPORT_SPEC, c, v),
                confirm_action="/crm/import/contacts/confirm",
                error_report_action="/crm/import/contacts/errors",
                back_href="/crm/import/contacts",
                revalidate_action="/crm/import/contacts/revalidate",
                has_mapping=True,
            ),
            title="Import Contacts - Celerp",
            nav_active="customers",
            request=request,
        )

    @app.post("/crm/import/contacts/revalidate")
    async def crm_import_contacts_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        import csv as _csv_mod, io as _io
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return _csv_upload_form(
                cols=_CONTACT_IMPORT_SPEC.cols, template_href="/crm/import/contacts/template",
                preview_action="/crm/import/contacts/preview",
                has_mapping=True,
                error="CSV data expired. Please re-upload.",
            )
        rows = list(_csv_mod.DictReader(_io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _CONTACT_IMPORT_SPEC.cols
        rows = _csv_apply_fixes(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return _csv_validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v, r: _csv_validate_cell(_CONTACT_IMPORT_SPEC, c, v),
            confirm_action="/crm/import/contacts/confirm",
            error_report_action="/crm/import/contacts/errors",
            back_href="/crm/import/contacts",
            revalidate_action="/crm/import/contacts/revalidate",
            has_mapping=True,
        )

    @app.post("/crm/import/contacts/errors")
    async def crm_import_contacts_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        import csv as _csv_mod, io as _io
        form = await request.form()
        rows = list(_csv_mod.DictReader(_io.StringIO(_resolve_csv_text(form))))
        cols = list(rows[0].keys()) if rows else _CONTACT_IMPORT_SPEC.cols
        return _csv_error_report(rows, cols, lambda c, v: _csv_validate_cell(_CONTACT_IMPORT_SPEC, c, v), "contacts_errors.csv")

    @app.post("/crm/import/contacts/confirm")
    async def crm_import_contacts_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        import csv, io, uuid

        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        records: list[dict] = []
        for r in rows:
            name = str(r.get("name", "")).strip()
            if not name:
                continue
            email = str(r.get("email", "")).strip()
            phone = str(r.get("phone", "")).strip()
            contact_type_val = str(r.get("contact_type", "")).strip() or "customer"

            data = {
                "name": name,
                "email": email or None,
                "phone": phone or None,
                "company_name": str(r.get("company_name", "")).strip() or None,
                "website": str(r.get("website", "")).strip() or None,
                "currency": str(r.get("currency", "")).strip() or None,
                "billing_address": str(r.get("billing_address", "")).strip() or None,
                "tax_id": str(r.get("tax_id", "")).strip() or None,
                "contact_type": contact_type_val,
                "payment_terms": str(r.get("payment_terms", "")).strip() or None,
            }
            credit_limit_raw = str(r.get("credit_limit", "")).strip()
            if credit_limit_raw:
                try:
                    data["credit_limit"] = float(credit_limit_raw)
                except ValueError:
                    data["credit_limit"] = credit_limit_raw

            idem = f"csv:contact:{email or phone or name}".lower()
            records.append({
                "entity_id": f"contact:{uuid.uuid4()}",
                "event_type": "crm.contact.created",
                "data": data,
                "source": "csv_import",
                "idempotency_key": idem,
            })

        try:
            result = await api.batch_import(token, "/crm/contacts/import/batch", records)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            result = {"created": 0, "skipped": 0, "errors": [e.detail]}

        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        errors = list(result.get("errors", []) or [])

        return _csv_import_result_panel(
            created=created,
            skipped=skipped,
            errors=errors,
            entity_label="contacts",
            back_href="/contacts/customers",
            import_more_href="/crm/import/contacts",
            has_mapping=True,
        )

    # ── Backward compat: /crm/{contact_id:path} → /contacts/{contact_id} ──

    @app.post("/crm/contacts/bulk/delete")
    async def crm_bulk_delete_contacts(request: Request):
        """Proxy: browser JS -> UI server -> backend API."""
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
            result = await api.bulk_delete_contacts(token, body.get("contact_ids", []))
            return JSONResponse(result)
        except APIError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)

    @app.post("/crm/contacts/merge")
    async def crm_merge_contacts(request: Request):
        """Proxy: browser JS -> UI server -> backend API."""
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
            result = await api.merge_contacts(
                token,
                target_contact_id=body.get("target_contact_id", ""),
                source_contact_ids=body.get("source_contact_ids", []),
            )
            return JSONResponse(result)
        except APIError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)

    @app.get("/crm/{contact_id}")
    async def crm_detail_redirect(contact_id: str):
        """Redirect old /crm/<id> URLs to /contacts/<id>. Only matches single path segments."""
        return RedirectResponse(f"/contacts/{contact_id}", status_code=302)
