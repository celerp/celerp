# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging
from datetime import date, datetime, timezone as _tz
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import search_bar, pagination, EMPTY, breadcrumbs, status_cards, empty_state_cta, fmt_money, format_value, add_new_option, data_table, column_manager
from ui.config import get_token as _token
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



def _memo_display_number(m: dict) -> str:
    """Extract a human-readable memo number from a memo dict."""
    if n := m.get("memo_number"):
        return n
    if gc := m.get("gc_id"):
        return f"GC-{gc}"
    raw_id = m.get("id", "")
    parts = raw_id.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return f"GC-{parts[1]}"
    return raw_id


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
                    hx_vals=f'{{"tag": "{tag}"}}',
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


def _files_section(contact: dict, contact_id: str) -> FT:
    """Files list with drag-and-drop upload, rendered inside the Documents tab."""
    files = contact.get("files", [])
    file_items = []
    for f in files:
        fid = f.get("file_id", "")
        fname = f.get("filename", "file")
        size = f.get("size", 0)
        size_str = f"{size / 1024:.0f}KB" if size < 1048576 else f"{size / 1048576:.1f}MB"
        file_items.append(Div(
            A(fname, href=f"/contacts/{contact_id}/files/{fid}/download", cls="file-link"),
            Span(size_str, cls="file-size"),
            Button("×", hx_delete=f"/contacts/{contact_id}/files/{fid}",
                   hx_target="#files-section", hx_swap="outerHTML",
                   hx_confirm=f"Delete {fname}?",
                   cls="btn btn--ghost btn--xs"),
            cls="file-item",
        ))

    # Drag-and-drop upload zone with click-to-browse fallback
    drop_js = f"""
(function(){{
  var zone = document.getElementById('file-drop-zone');
  var input = document.getElementById('file-drop-input');
  if (!zone || !input) return;
  function uploadFile(file) {{
    var fd = new FormData();
    fd.append('file', file);
    var statusEl = document.querySelector('.file-drop-text');
    if (statusEl) statusEl.textContent = 'Uploading...';
    fetch('/contacts/{contact_id}/files', {{
      method: 'POST',
      body: fd,
    }}).then(function(resp) {{
      if (!resp.ok) throw new Error('Upload failed');
      // Reload documents tab to refresh files section with proper htmx init
      htmx.ajax('GET', '/contacts/{contact_id}/tab/documents', {{target: '#tab-content', swap: 'innerHTML'}});
    }}).catch(function(err) {{
      alert('Upload failed: ' + err.message);
      if (statusEl) statusEl.textContent = 'Drop files here or click to browse';
    }});
  }}
  zone.addEventListener('click', function() {{ input.click(); }});
  input.addEventListener('change', function() {{
    if (input.files.length) uploadFile(input.files[0]);
  }});
  zone.addEventListener('dragover', function(e) {{ e.preventDefault(); zone.classList.add('file-drop-zone--active'); }});
  zone.addEventListener('dragleave', function() {{ zone.classList.remove('file-drop-zone--active'); }});
  zone.addEventListener('drop', function(e) {{
    e.preventDefault();
    zone.classList.remove('file-drop-zone--active');
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  }});
}})();
"""

    upload_form = Div(
        Div(
            Div("📁", cls="file-drop-icon"),
            Div(t("label.drop_files_here_or_click_to_browse"), cls="file-drop-text"),
            Div(t("label.max_file_size_10mb"), cls="file-drop-hint"),
            Input(type="file", name="file", id="file-drop-input", style="display:none"),
            cls="file-drop-zone", id="file-drop-zone",
        ),
    )

    return Div(
        H3(t("page.documents"), cls="section-title"),
        *file_items if file_items else [P(t("label.no_files_yet"), cls="text--muted")],
        upload_form,
        Script(drop_js),
        cls="card", id="files-section",
    )


def _contact_info_card(c: dict, *, oob: bool = False) -> FT:
    """Left-column contact info: click-to-edit fields."""
    cid = c.get("entity_id") or c.get("id") or ""
    fields = [
        ("name", "Name"), ("company_name", "Company"), ("email", "Email"), ("phone", "Phone"),
        ("website", "Website"), ("billing_address", "Billing Address"), ("shipping_address", "Shipping Address"),
        ("tax_id", "Tax ID"), ("currency", "Currency"),
    ]
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
    today = date.today()
    fy_month, fy_day = (int(x) for x in fiscal_year_start.split("-"))
    fy_start = date(today.year, fy_month, fy_day)
    if fy_start > today:
        fy_start = date(today.year - 1, fy_month, fy_day)
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
            Button("✏", hx_get=f"/contacts/{cid}/addresses/{addr_id}/edit", hx_target=f"#addr-{addr_id}", hx_swap="outerHTML", cls="btn btn--xs btn--secondary", title="Edit"),
            Button("×", hx_delete=f"/contacts/{cid}/addresses/{addr_id}", hx_target="#addresses-section", hx_swap="outerHTML", hx_confirm="Remove this address?", cls="btn btn--xs btn--danger", title="Remove"),
            cls="addr-actions",
        ),
        cls="address-card", id=f"addr-{addr_id}",
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


def _tab_bar(cid: str, active: str = "documents") -> FT:
    """HTMX-driven tab bar for Documents / Notes / Activity."""
    tabs = [("documents", "Documents"), ("notes", "Notes"), ("activity", "Activity")]
    return Div(
        *[A(
            label,
            hx_get=f"/contacts/{cid}/tab/{key}",
            hx_target="#tab-content",
            hx_swap="innerHTML",
            cls="tab active" if key == active else "tab",
            id=f"tab-{key}",
            # JS: set active class on click
            onclick="document.querySelectorAll('.tab-bar .tab').forEach(t=>t.classList.remove('active'));this.classList.add('active');",
        ) for key, label in tabs],
        cls="tab-bar",
    )


def _documents_tab(docs: list[dict], contact: dict | None = None, contact_id: str = "") -> FT:
    """Documents tab content: table of related docs + file upload zone."""
    # Related documents table
    if not docs:
        docs_section = P(t("label.no_documents_yet"), cls="empty-state-msg")
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

    # Files / upload section
    if contact is not None and contact_id:
        files_content = _files_section(contact, contact_id)
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
    """Notes tab content: add form + timeline."""
    try:
        _zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        _zone = _tz.utc

    def _fmt_ts(iso: str) -> str:
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt.astimezone(_zone).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return iso[:16].replace("T", " ")
    note_form = Form(
        Div(
            Textarea(name="note", placeholder="Add a note...", rows="3", cls="form-input", style="width:100%"),
            cls="form-group",
        ),
        Button(t("btn.add_note"), type="submit", cls="btn btn--primary btn--sm"),
        hx_post=f"/contacts/{contact_id}/notes",
        hx_target="#tab-content",
        hx_swap="innerHTML",
        cls="section-card",
    )
    if not notes:
        return Div(note_form, P(t("label.no_notes_yet"), cls="empty-state-msg"))
    timeline = []
    for n in notes:
        note_id = n.get("note_id") or n.get("id") or ""
        text = n.get("note") or ""
        author = n.get("author_name") or ""
        created = n.get("created_at") or ""
        updated = n.get("updated_at")
        ts_display = _fmt_ts(created)
        if updated:
            ts_display += f" (edited {_fmt_ts(updated)})"
        initials = "".join(w[0].upper() for w in author.split()[:2]) if author else "?"
        timeline.append(Div(
            Div(
                Span(initials, cls="note-author-badge", title=author),
                Div(
                    Span(author, cls="note-author-name") if author else "",
                    Small(ts_display, cls="note-timestamp"),
                    cls="note-meta",
                ),
                cls="note-header",
            ),
            P(text, cls="note-text"),
            Div(
                Button(t("btn.edit"),
                       hx_get=f"/contacts/{contact_id}/notes/{note_id}/edit",
                       hx_target=f"#note-{note_id}",
                       hx_swap="outerHTML",
                       cls="btn btn--ghost btn--xs"),
                Button(t("btn.delete"),
                       hx_delete=f"/contacts/{contact_id}/notes/{note_id}",
                       hx_target="#tab-content",
                       hx_swap="innerHTML",
                       hx_confirm="Delete this note?",
                       cls="btn btn--ghost btn--xs btn--danger"),
                cls="note-actions",
            ),
            cls="note-item", id=f"note-{note_id}",
        ))
    return Div(note_form, *timeline)


def _contact_ledger_table(ledger: list[dict]) -> FT:
    """Activity history section for contact detail page."""
    from ui.components.activity import activity_table
    return activity_table(ledger, max_display=10)


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
            show_checkboxes=False,
            link_fn=link_fn,
            auto_hide_empty=False,
            edit_url_tpl="/contacts/{id}/field/{field}/edit",
        )

    return Div(
        table_content,
        pagination(page, total, per_page, base_url, f"type={contact_type}&q={q}&sort={sort}&dir={sort_dir}".strip("&")),
        id="contacts-content",
    )


def _contacts_page_shell(contact_type: str, contacts: list[dict], request: Request, q: str, page: int, total: int, per_page: int, sort: str, sort_dir: str, currency: str | None = None) -> FT:
    """Renders the full contact list page for customers or vendors."""
    label = "Customers" if contact_type == "customer" else "Vendors"
    nav_key = "customers" if contact_type == "customer" else "vendors"
    base_url = f"/contacts/{contact_type}s"
    create_url = f"/contacts/create?type={contact_type}"
    search_url = "/contacts/content"
    schema = _contact_schema(contact_type)
    et = f"{contact_type}s"

    return base_shell(
        page_header(
            label,
            search_bar(placeholder=f"Search {label.lower()}...", target="#contacts-content", url=search_url),
            Button(f"New {label[:-1]}", hx_post=create_url, hx_swap="none", cls="btn btn--primary"),
            A(t("btn.export_csv"), href=f"{base_url}/export/csv", cls="btn btn--secondary"),
            A(t("btn.import"), href="/crm/import/contacts", cls="btn btn--secondary"),
        ),
        Div(column_manager(schema, et), cls="column-manager-row"),
        _contacts_content(contact_type, contacts, q, page, total, per_page, sort, sort_dir, currency),
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


async def _memos_tab(request: Request, token: str, tab_bar) -> FT:
    """CRM Memos tab: list all memos with New Memo button, date filter, and pagination."""
    from ui.components.table import pagination as _pagination
    date_from, date_to, preset = _parse_dates(request)
    page = int(request.query_params.get("page", 1))
    try:
        per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
    except (ValueError, TypeError):
        per_page = _PER_PAGE

    params: dict = {"limit": per_page, "offset": (page - 1) * per_page}
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    try:
        resp = await api.list_memos(token, params)
        memos = resp.get("items", []) if isinstance(resp, dict) else resp
        total = resp.get("total", len(memos)) if isinstance(resp, dict) else len(memos)
    except APIError as e:
        logger.warning("API error loading memos tab: %s", e.detail)
        memos, total = [], 0

    def _row(m: dict) -> FT:
        memo_id = m.get("entity_id") or m.get("id") or ""
        memo_number = m.get("memo_number") or m.get("ref_id") or (str(m.get("external_id") or "").split(":")[-1] if m.get("external_id") else None)
        contact_raw = m.get("contact_name") or m.get("contact_id") or m.get("contact_external_id")
        contact_val = _clean_external_ref(contact_raw) if contact_raw and "gc:" in str(contact_raw) else contact_raw
        issue_date = m.get("issue_date") or m.get("created_at")
        total_val = m.get("total_amount") or m.get("total")

        return Tr(
            Td(A(format_value(memo_number or (memo_id.split(":")[-1][:8] if memo_id else EMPTY)), href=f"/crm/memos/{memo_id}", cls="table-link") if memo_id else format_value(memo_number)),
            Td(format_value(contact_val)),
            Td(format_value(str(issue_date)[:10] if issue_date else None)),
            Td(format_value(total_val, "money"), cls="cell--number"),
            Td(format_value(m.get("status"), "badge")),
            cls="data-row",
        )

    table = Table(
        Thead(Tr(Th(t("th.memo")), Th(t("page.contact_detail")), Th(t("th.date")), Th(t("th.total")), Th(t("th.status")))),
        Tbody(*[_row(m) for m in memos]) if memos else Tbody(Tr(Td(t("label.no_memos_yet"), colspan="5", cls="empty-state-msg"))),
        cls="data-table",
    ) if memos else Div(P(t("label.no_memos_yet"), cls="empty-state-msg"), cls="empty-state")

    return base_shell(
        page_header(
            "CRM",
            A(t("page.new_memo"), href="/docs?type=memo", cls="btn btn--primary"),
        ),
        tab_bar,
        _date_filter_bar("/crm", date_from, date_to, preset, extra_params="&tab=memos"),
        table,
        _pagination(page, total, per_page, "/crm", f"tab=memos&preset={preset}"),
        title="CRM - Memos - Celerp",
        nav_active="crm",
        request=request,
    )


def _memo_detail(memo: dict, items: list[dict]) -> FT:
    memo_id = memo.get("entity_id") or memo.get("id") or ""
    status = memo.get("status", "draft")
    line_items = memo.get("items", memo.get("line_items", []))

    memo_ref = memo.get("memo_number") or memo.get("ref_id") or memo.get("external_id") or memo_id
    header_bar = Div(
        Span(f"Memo #{memo_ref}", cls="doc-ref"),
        format_value(status, "badge"),
        cls="doc-header-bar",
    )

    li_rows = []
    for li in line_items:
        iid = li.get("item_id", "")
        li_rows.append(Tr(
            Td(str(iid)),
            Td(str(li.get("quantity", 0))),
            Td(format_value(li.get("price"), "money"), cls="cell--number"),
            Td(
                Button(t("btn.remove"), hx_post=f"/crm/memos/{memo_id}/remove-item/{iid}",
                       hx_swap="none", cls="btn btn--danger btn--xs")
                if status == "draft" else "",
            ),
            cls="data-row",
        ))

    li_table = Table(
        Thead(Tr(Th(t("th.item")), Th(t("th.qty")), Th(t("th.price")), Th(""))),
        Tbody(*li_rows) if li_rows else Tbody(Tr(Td(t("label.no_items_yet"), colspan="4", cls="empty-state-msg"))),
        cls="data-table data-table--compact",
    )

    add_item_form = ""
    if status == "draft":
        add_item_form = Details(
            Summary(t("label._add_item"), cls="btn btn--secondary btn--sm"),
            Form(
                Div(Label(t("label.item_id"), cls="form-label"), Input(type="text", name="item_id", placeholder="item:...", cls="form-input"), cls="form-group"),
                Div(Label(t("th.quantity"), cls="form-label"), Input(type="number", name="quantity", value="1", step="any", min="0", cls="form-input"), cls="form-group"),
                Button(t("btn.add"), type="submit", cls="btn btn--primary btn--sm"),
                hx_post=f"/crm/memos/{memo_id}/add-item",
                hx_swap="none",
                cls="form-card",
            ),
        )

    actions = []
    if status == "draft":
        actions.append(Button(t("btn.approve"), hx_post=f"/crm/memos/{memo_id}/approve", hx_swap="none", cls="btn btn--primary"))
    if status == "approved":
        actions.append(Button(t("btn.convert"), hx_post=f"/crm/memos/{memo_id}/convert-to-invoice", hx_swap="none", cls="btn btn--primary"))
        actions.append(
            Details(
                Summary(t("btn.cancel"), cls="btn btn--danger btn--sm"),
                Form(
                    Div(Label(t("label.reason"), cls="form-label"), Input(type="text", name="reason", placeholder="Cancellation reason...", cls="form-input"), cls="form-group"),
                    Button(t("btn.confirm_cancel"), type="submit", cls="btn btn--danger btn--sm"),
                    hx_post=f"/crm/memos/{memo_id}/cancel",
                    hx_swap="none",
                    cls="form-card",
                ),
            )
        )
        if line_items:
            return_rows = []
            for idx, li in enumerate(line_items):
                iid = li.get("item_id", "")
                return_rows.append(Tr(
                    Td(str(iid)),
                    Td(Input(type="hidden", name=f"ret_item_{idx}", value=iid),
                       Input(type="number", name=f"ret_qty_{idx}", value="0", step="any", min="0", cls="form-input form-input--sm")),
                ))
            actions.append(
                Details(
                    Summary(t("label.return_items"), cls="btn btn--secondary btn--sm"),
                    Form(
                        Table(Thead(Tr(Th(t("th.item")), Th(t("th.qty_to_return")))), Tbody(*return_rows), cls="data-table data-table--compact"),
                        Button(t("btn.confirm_return"), type="submit", cls="btn btn--primary btn--sm"),
                        hx_post=f"/crm/memos/{memo_id}/return",
                        hx_swap="none",
                        cls="form-card",
                    ),
                )
            )
    actions.append(Span("", id="memo-error"))

    return Div(
        header_bar,
        Div(*actions, cls="doc-actions"),
        Div(
            Table(
                Tbody(
                    Tr(Td(t("page.contact_detail"), cls="detail-label"), Td(str(memo.get("contact_id", "") or EMPTY))),
                    Tr(Td(t("th.status"), cls="detail-label"), Td(format_value(status, "badge"))),
                    Tr(Td(t("th.notes"), cls="detail-label"), Td(str(memo.get("notes", "") or EMPTY))),
                ),
                cls="detail-table",
            ),
            cls="detail-card",
        ),
        H3(t("page.line_items"), cls="section-title"),
        li_table,
        add_item_form,
        cls="memo-detail",
    )


def setup_routes(app):

    # ── /contacts/customers ───────────────────────────────────────────────

    @app.get("/contacts/customers")
    async def customers_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
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
        return _contacts_page_shell("customer", contacts, request, q, page, total, per_page, sort, sort_dir, currency)

    # ── /contacts/vendors ─────────────────────────────────────────────────

    @app.get("/contacts/vendors")
    async def vendors_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
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
        return _contacts_page_shell("vendor", contacts, request, q, page, total, per_page, sort, sort_dir, currency)

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
        return base_shell(
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
            ledger_resp = await api.list_ledger(token, {"entity_id": contact_id, "limit": 10})
            ledger = ledger_resp.get("items", []) if isinstance(ledger_resp, dict) else []
        except Exception:
            ledger = []
        try:
            vocab = await api.get_contact_tags_vocabulary(token)
        except Exception:
            vocab = []

        try:
            company = await api.get_company(token)
        except Exception:
            company = {}
        fiscal_year_start = company.get("fiscal_year_start") or "01-01"

        contact_name = contact.get("name", "Contact")
        contact_type = contact.get("contact_type", "")
        if contact_type in ("vendor",):
            back_href = "/contacts/vendors"
            back_label = "Vendors"
            nav_active_key = "vendors"
        else:
            back_href = "/contacts/customers"
            back_label = "Customers"
            nav_active_key = "customers"

        autofocus_script = (
            Script("document.querySelector('.cell--clickable')?.click();")
            if contact_name in ("New Customer", "New Vendor", "New Both", "New Contact") else ""
        )

        cid = contact.get("entity_id") or contact.get("id") or contact_id

        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), (back_label, back_href), (contact_name, None)]),
            page_header(contact_name),
            autofocus_script,
            _financial_summary(docs, contact_id=cid, fiscal_year_start=fiscal_year_start),
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
            _addresses_section(contact),
            _tab_bar(cid),
            Div(
                _documents_tab(docs, contact, cid),
                id="tab-content",
            ),
            title=f"{contact_name} - Celerp",
            nav_active=nav_active_key,
            request=request,
        )

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
        return _documents_tab(docs, contact, cid)

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
        return Div(
            Form(
                Textarea(note.get("note", ""), name="note", rows="3", cls="form-input", style="width:100%"),
                Div(
                    Button(t("btn.save"), type="submit", cls="btn btn--primary btn--xs"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--xs",
                           hx_get=f"/contacts/{contact_id}/tab/notes",
                           hx_target="#tab-content", hx_swap="innerHTML"),
                    cls="form-row",
                ),
                hx_patch=f"/contacts/{contact_id}/notes/{note_id}",
                hx_target="#tab-content",
                hx_swap="innerHTML",
            ),
            cls="note-item note-item--editing", id=f"note-{note_id}",
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
                           onclick=f"htmx.ajax('GET','/contacts/{contact_id}/addresses/section','#addresses-section',{{swap:'outerHTML'}})"),
                    cls="form-row",
                ),
                hx_patch=f"/contacts/{contact_id}/addresses/{address_id}",
                hx_target="#addresses-section",
                hx_swap="outerHTML",
                hx_trigger="submit",
            ),
            cls="inline-form", id=f"addr-{address_id}",
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
            except APIError:
                contact = {"entity_id": contact_id}
            return _files_section(contact, contact_id)
        description = str(form.get("description", "")).strip()
        content = await file.read()
        filename = getattr(file, "filename", "upload")
        content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
        try:
            await api.upload_contact_file(token, contact_id, content, filename, content_type, description)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id)

    @app.delete("/contacts/{contact_id}/files/{file_id}")
    async def contact_delete_file(request: Request, contact_id: str, file_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.delete_contact_file(token, contact_id, file_id)
            contact = await api.get_contact(token, contact_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _files_section(contact, contact_id)

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
        return RedirectResponse("/crm/export/csv?contact_type=customer", status_code=302)

    @app.get("/contacts/vendors/export/csv")
    async def vendors_export_csv(request: Request):
        return RedirectResponse("/crm/export/csv?contact_type=vendor", status_code=302)

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
        params = {"q": q} if q else {}
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
        return base_shell(
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
            return base_shell(
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
        return base_shell(
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
            return base_shell(
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
            return base_shell(
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

        return base_shell(
            page_header("Import Contacts", A(t("btn.back_to_settings"), href="/contacts/customers", cls="btn btn--secondary")),
            _csv_validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v: _csv_validate_cell(_CONTACT_IMPORT_SPEC, c, v),
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
            validate=lambda c, v: _csv_validate_cell(_CONTACT_IMPORT_SPEC, c, v),
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

    # ── Memo routes ────────────────────────────────────────────────────────

    @app.post("/crm/memos/from-items")
    async def memo_from_items_modal(request: Request):
        """Modal: choose to create new memo or add items to existing."""
        from ui.routes.documents import _send_to_modal, _send_to_option_list
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            drafts_resp = await api.list_memos(token, {"status": "out", "limit": 20})
            drafts = drafts_resp.get("items", [])
        except APIError:
            drafts = []
        hidden_items = [Input(type="hidden", name="selected", value=eid) for eid in entity_ids]
        return _send_to_modal("Memo", "/crm/memos/from-items/new", "/crm/memos/from-items/add",
                              "/crm/memos/from-items/search", drafts, hidden_items, "memo")

    @app.post("/crm/memos/new")
    async def create_blank_memo(request: Request):
        """Create a blank memo and redirect to it."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.create_memo(token)
            memo_id = result.get("id", "")
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _R("", status_code=500)
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    @app.post("/crm/memos/from-items/new")
    async def create_memo_from_items(request: Request):
        """Create a memo and add selected inventory items to it."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="modal-container")
        try:
            result = await api.create_memo(token)
            memo_id = result.get("id", "")
            for eid in entity_ids:
                await api.add_memo_item(token, memo_id, {"item_id": eid})
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    @app.post("/crm/memos/from-items/add")
    async def add_items_to_memo(request: Request):
        """Add selected inventory items to an existing memo."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_id = str(form.get("target_id", "")).strip()
        if not entity_ids or not target_id:
            return Div(P(t("label.no_items_or_target_selected"), cls="flash flash--warning"), id="modal-container")
        try:
            for eid in entity_ids:
                await api.add_memo_item(token, target_id, {"item_id": eid})
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{target_id}"})

    @app.get("/crm/memos/from-items/search")
    async def memo_from_items_search(request: Request):
        """HTMX search endpoint for the memo picker dropdown."""
        from ui.routes.documents import _send_to_option_list
        token = _token(request)
        if not token:
            return Div()
        q = request.query_params.get("q", "").strip()
        try:
            params: dict = {"limit": 20}
            if q:
                # Memo API doesn't have q param - filter client-side
                resp = await api.list_memos(token, {"limit": 100})
                items = resp.get("items", [])
                ql = q.lower()
                items = [m for m in items if ql in str(m.get("memo_number", "")).lower()
                         or ql in str(m.get("contact_name", "")).lower()
                         or ql in str(m.get("id", "")).lower()][:20]
            else:
                resp = await api.list_memos(token, params)
                items = resp.get("items", [])
        except APIError:
            items = []
        return _send_to_option_list(items, "memo")

    @app.get("/crm/memos/{memo_id}")
    async def memo_detail_page(request: Request, memo_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            memo = await api.get_memo(token, memo_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            memo = {}
        try:
            items = (await api.list_items(token, {"limit": 500})).get("items", [])
        except APIError as e:
            logger.warning("API error loading items for memo detail: %s", e.detail)
            items = []
        memo_label = _memo_display_number(memo)
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Customers", "/contacts/customers"), (f"Memo {memo_label}", None)]),
            page_header(f"Memo — {memo_label}", A(t("label.back"), href="/contacts/customers", cls="btn btn--secondary")),
            _memo_detail(memo, items),
            title=f"Memo {memo_label} - CRM",
            nav_active="customers",
            request=request,
        )

    @app.post("/crm/memos/{memo_id}/approve")
    async def approve_memo_route(request: Request, memo_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.approve_memo(token, memo_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="memo-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    @app.post("/crm/memos/{memo_id}/cancel")
    async def cancel_memo_route(request: Request, memo_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.cancel_memo(token, memo_id, reason)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="memo-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    @app.post("/crm/memos/{memo_id}/convert-to-invoice")
    async def convert_memo_route(request: Request, memo_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.convert_memo_to_invoice(token, memo_id)
            target_id = result.get("doc_id", memo_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="memo-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})

    @app.post("/crm/memos/{memo_id}/return")
    async def return_memo_route(request: Request, memo_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        items = []
        idx = 0
        while f"ret_item_{idx}" in form:
            item_id = str(form.get(f"ret_item_{idx}", "")).strip()
            try:
                qty = float(str(form.get(f"ret_qty_{idx}", "0")))
            except ValueError:
                qty = 0.0
            if item_id and qty > 0:
                items.append({"item_id": item_id, "quantity": qty})
            idx += 1
        try:
            await api.return_memo(token, memo_id, {"items": items})
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="memo-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    @app.post("/crm/memos/{memo_id}/add-item")
    async def add_memo_item_route(request: Request, memo_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        item_id = str(form.get("item_id", "")).strip()
        try:
            quantity = float(str(form.get("quantity", "1")))
        except ValueError:
            quantity = 1.0
        try:
            await api.add_memo_item(token, memo_id, {"item_id": item_id, "quantity": quantity})
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="memo-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    @app.post("/crm/memos/{memo_id}/remove-item/{item_id}")
    async def remove_memo_item_route(request: Request, memo_id: str, item_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.remove_memo_item(token, memo_id, item_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="memo-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})

    # ── Backward compat: /crm/{contact_id:path} → /contacts/{contact_id} ──

    @app.get("/crm/{contact_id}")
    async def crm_detail_redirect(contact_id: str):
        """Redirect old /crm/<id> URLs to /contacts/<id>. Only matches single path segments."""
        return RedirectResponse(f"/contacts/{contact_id}", status_code=302)
