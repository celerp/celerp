# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Subscription template UI routes.

Subscription templates are docs with doc_type "subscription_invoice" or "subscription_po".
Creation uses POST /docs; lifecycle actions use the /subscriptions API.
Inline editing on the detail page uses the existing /docs/{entity_id}/field/{field} PATCH
endpoint - subscription-specific GET routes only override the edit/display widgets.
"""
from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import searchable_select, empty_state_cta, fmt_money, format_value
from ui.config import get_token as _token, get_role as _get_role
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)

_FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "annually", "custom"]
_CURRENCIES = ["USD", "EUR", "GBP", "THB", "SGD", "AUD", "CAD", "JPY", "CNY"]

_STATUS_COLORS = {
    "active": "badge--active",
    "paused": "badge--paused",
    "cancelled": "badge--void",
    "draft": "badge--draft",
}

# Line items JS: add/remove rows in the template table
_LINE_ITEMS_JS = """
function subAddRow() {
    var tbody = document.getElementById('sub-line-items');
    var idx = tbody.rows.length;
    var tr = document.createElement('tr');
    tr.innerHTML = '<td><input type="text" name="li_desc_' + idx + '" placeholder="Description" class="cell-input" style="width:100%"></td>'
        + '<td><input type="number" name="li_qty_' + idx + '" value="1" min="0" step="any" class="cell-input" style="width:70px"></td>'
        + '<td><input type="number" name="li_price_' + idx + '" value="0" min="0" step="any" class="cell-input" style="width:100px"></td>'
        + '<td><button type="button" onclick="this.closest(\'tr\').remove()" class="btn btn--ghost btn--sm">&#x2715;</button></td>';
    tbody.appendChild(tr);
}
"""

# --- Inline-edit helpers ---

def _sub_display_cell(entity_id: str, field: str, value) -> FT:
    """Read-only div with click-to-edit trigger. Routes to /subscriptions/ edit endpoint."""
    if field == "status":
        return Div(format_value(value, "badge"), cls="editable-cell")
    return Div(
        format_value(value, "date" if field in {"start_date", "next_run_date"} else "text"),
        hx_get=f"/subscriptions/{entity_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        title="Click to edit",
        cls="editable-cell",
    )


def _sub_edit_cell(entity_id: str, field: str, value) -> FT:
    """Edit widget for a subscription field. PATCHes /docs/{entity_id}/field/{field}."""
    display_val = str(value) if value is not None else ""
    patch_url = f"/docs/{entity_id}/field/{field}"
    restore_url = f"/subscriptions/{entity_id}/field/{field}/display"
    swap = dict(hx_patch=patch_url, hx_target="this", hx_swap="outerHTML")
    escape_js = (
        f"if(event.key==='Escape'){{"
        f"htmx.ajax('GET','{restore_url}',{{target:this,swap:'outerHTML'}});"
        f"event.preventDefault();}}"
        f"else if(event.key==='Enter'){{event.preventDefault();htmx.trigger(this,'blur');}}"
    )

    if field == "frequency":
        input_el = Select(
            *[Option(f.capitalize(), value=f, selected=(f == display_val)) for f in _FREQUENCIES],
            name="value",
            **swap, hx_trigger="change",
            cls="cell-input cell-input--select",
            autofocus=True,
            onkeydown=escape_js,
        )
    elif field == "currency":
        input_el = Select(
            *[Option(c, value=c, selected=(c == display_val)) for c in _CURRENCIES],
            name="value",
            **swap, hx_trigger="change",
            cls="cell-input cell-input--select",
            autofocus=True,
            onkeydown=escape_js,
        )
    elif field == "start_date":
        input_el = Input(
            type="date", name="value", value=display_val,
            **swap, hx_trigger="blur delay:200ms",
            cls="cell-input",
            autofocus=True,
            onkeydown=escape_js,
        )
    elif field == "custom_interval_days":
        input_el = Input(
            type="number", name="value", value=display_val, min="1",
            **swap, hx_trigger="blur delay:200ms",
            cls="cell-input cell-input--number",
            autofocus=True,
            onkeydown=escape_js,
        )
    else:
        input_el = Input(
            type="text", name="value", value=display_val,
            **swap, hx_trigger="blur delay:200ms",
            cls="cell-input",
            autofocus=True,
            onkeydown=escape_js,
        )
    return Div(input_el, cls="editable-cell editable-cell--editing")


# Subscription-editable fields; others are read-only on the detail page
_EDITABLE_FIELDS = frozenset({
    "name", "frequency", "start_date", "custom_interval_days",
    "currency", "payment_terms", "notes",
})


# --- Status badge ---

def _sub_status_badge(status: str) -> FT:
    css = _STATUS_COLORS.get(status, "badge--draft")
    return Span(status.capitalize(), cls=f"badge {css}")


# --- Table helpers ---

def _line_items_section(sub: dict) -> FT:
    line_items = sub.get("line_items") or []
    if not line_items:
        return P("No line items.", cls="text-muted")
    rows = [
        Tr(
            Td(li.get("description") or li.get("name") or "-"),
            Td(str(li.get("quantity") or ""), style="text-align:right"),
            Td(fmt_money(li.get("unit_price") or 0), style="text-align:right"),
            Td(fmt_money((li.get("quantity") or 0) * (li.get("unit_price") or 0)), style="text-align:right"),
        )
        for li in line_items
    ]
    return Table(
        Thead(Tr(
            Th("Description"),
            Th("Qty", style="text-align:right"),
            Th("Unit Price", style="text-align:right"),
            Th("Subtotal", style="text-align:right"),
        )),
        Tbody(*rows),
        cls="data-table",
    )


def _generated_docs_table(doc_ids: list) -> FT:
    if not doc_ids:
        return P("No documents generated yet.", cls="text-muted")
    rows = [Tr(Td(A(did, href=f"/docs/{did}"))) for did in doc_ids]
    return Table(Thead(Tr(Th("Generated Document"))), Tbody(*rows), cls="data-table")


def _template_row(sub: dict) -> FT:
    sid = sub.get("id") or sub.get("entity_id", "")
    name = sub.get("name") or sid
    freq = sub.get("frequency", "-")
    next_run = sub.get("next_run_date", "-")
    status = sub.get("status", "active")
    return Tr(
        Td(A(name, href=f"/subscriptions/{sid}")),
        Td(freq.capitalize()),
        Td(next_run),
        Td(_sub_status_badge(status)),
    )


def _template_table(subs: list) -> FT:
    if not subs:
        return empty_state_cta("No subscription templates found.", "New Subscription", "/subscriptions/new")
    rows = [_template_row(s) for s in subs]
    return Table(
        Thead(Tr(Th("Name"), Th("Frequency"), Th("Next Run"), Th("Status"))),
        Tbody(*rows),
        cls="data-table",
    )


def _direction_tabs(direction: str) -> FT:
    return Nav(
        A("Sales", href="/subscriptions?direction=sales",
          cls=f"tab-link {'tab-link--active' if direction == 'sales' else ''}"),
        A("Purchasing", href="/subscriptions?direction=purchasing",
          cls=f"tab-link {'tab-link--active' if direction == 'purchasing' else ''}"),
        cls="tab-nav",
    )


def setup_routes(app) -> None:

    # --- Inline-edit endpoints (subscription-specific field rendering) ---

    @app.get("/subscriptions/{entity_id}/field/{field}/display")
    async def sub_field_display(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            sub = await api.get_subscription(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        return _sub_display_cell(entity_id, field, sub.get(field))

    @app.get("/subscriptions/{entity_id}/field/{field}/edit")
    async def sub_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        if field not in _EDITABLE_FIELDS:
            return P("-")
        try:
            sub = await api.get_subscription(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        if sub.get("status") != "draft":
            return _sub_display_cell(entity_id, field, sub.get(field))
        return _sub_edit_cell(entity_id, field, sub.get(field))

    # --- List ---

    @app.get("/subscriptions")
    async def subscriptions_list(request: Request, direction: str = "sales", status: str | None = None):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            params = {"direction": direction}
            if status:
                params["status"] = status
            resp = await api.list_subscriptions(token, params)
            subs = resp if isinstance(resp, list) else resp.get("items", [])
        except APIError:
            subs = []

        title = "Sales Subscriptions" if direction == "sales" else "Purchasing Subscriptions"
        content = Div(
            page_header(title,
                A("+ New Subscription", href=f"/subscriptions/new?direction={direction}", cls="btn btn--primary"),
            ),
            _direction_tabs(direction),
            _template_table(subs),
            cls="page-content",
        )
        return base_shell(request, content, title=title)

    # --- New form ---

    @app.get("/subscriptions/new")
    async def new_subscription_form(request: Request, direction: str = "sales"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        doc_type = "subscription_invoice" if direction == "sales" else "subscription_po"
        contact_filter = "customer" if direction == "sales" else "vendor"
        title = "New Subscription"
        error = request.query_params.get("error")

        try:
            contacts_resp = await api.list_contacts(token, {"limit": 500, "contact_type": contact_filter})
            contacts = contacts_resp.get("items", [])
        except APIError:
            contacts = []

        contact_opts = [(c.get("entity_id", ""), c.get("name") or c.get("entity_id", "")) for c in contacts]
        contact_opts.append(("__new__", "+ Add new contact"))

        def _li_row(idx: int) -> FT:
            return Tr(
                Td(Input(type="text", name=f"li_desc_{idx}", placeholder="Description", cls="cell-input", style="width:100%")),
                Td(Input(type="number", name=f"li_qty_{idx}", value="1", min="0", step="any", cls="cell-input", style="width:70px")),
                Td(Input(type="number", name=f"li_price_{idx}", value="0", min="0", step="any", cls="cell-input", style="width:100px")),
                Td(Button("\u2715", type="button", onclick="this.closest('tr').remove()", cls="btn btn--ghost btn--sm")),
            )

        form = Form(
            Script(_LINE_ITEMS_JS),
            Input(type="hidden", name="doc_type", value=doc_type),
            error and Div(P(f"Error: {error}"), cls="flash flash--error"),
            Section(
                H3("Basic Info", cls="section-title"),
                Div(
                    Div(Label("Name *", cls="form-label"), Input(name="name", placeholder="e.g. Monthly Retainer", required=True, cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(Label("Contact *", cls="form-label"), searchable_select("contact_id", contact_opts, placeholder="Search contact..."), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(
                        Label("Frequency", cls="form-label"),
                        Select(
                            *[Option(f.capitalize(), value=f) for f in _FREQUENCIES],
                            name="frequency",
                            id="frequency-select",
                            onchange="document.getElementById('custom-interval-row').style.display = this.value === 'custom' ? '' : 'none'",
                            cls="form-input",
                        ),
                        cls="form-group",
                    ),
                    Div(Label("Start Date *", cls="form-label"), Input(name="start_date", type="date", required=True, cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Div(
                    Div(Label("Custom Interval (days)", cls="form-label"), Input(name="custom_interval_days", type="number", min="1", placeholder="30", cls="form-input"), cls="form-group"),
                    id="custom-interval-row",
                    style="display:none",
                    cls="form-row",
                ),
                Div(
                    Div(
                        Label("Currency", cls="form-label"),
                        Select(*[Option(c, value=c) for c in _CURRENCIES], name="currency", cls="form-input"),
                        cls="form-group",
                    ),
                    Div(Label("Payment Terms", cls="form-label"), Input(name="payment_terms", placeholder="e.g. Net 30", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                cls="section-card",
            ),
            Section(
                H3("Line Items", cls="section-title"),
                Table(
                    Thead(Tr(Th("Description"), Th("Qty", style="text-align:right"), Th("Unit Price", style="text-align:right"), Th(""))),
                    Tbody(_li_row(0), id="sub-line-items"),
                    cls="data-table",
                ),
                Button("+ Add Row", type="button", onclick="subAddRow()", cls="btn btn--ghost btn--sm", style="margin-top:0.5rem"),
                cls="section-card",
            ),
            Section(
                H3("Additional", cls="section-title"),
                Div(
                    Div(Label("Discount %", cls="form-label"), Input(name="discount_pct", type="number", min="0", max="100", step="0.01", value="0", cls="form-input"), cls="form-group"),
                    Div(Label("Notes", cls="form-label"), Textarea(name="notes", rows="3", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                cls="section-card",
            ),
            Div(
                Button("Create Subscription", type="submit", cls="btn btn--primary"),
                A("Cancel", href=f"/subscriptions?direction={direction}", cls="btn btn--ghost"),
                cls="form-actions",
            ),
            method="post",
            action="/subscriptions/new",
            cls="form form--wide",
        )

        content = Div(page_header(title), form, cls="page-content")
        return base_shell(request, content, title=title)

    @app.post("/subscriptions/new")
    async def create_subscription_route(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        data = dict(form)
        direction = "sales" if data.get("doc_type") == "subscription_invoice" else "purchasing"

        if not data.get("custom_interval_days"):
            data.pop("custom_interval_days", None)
        else:
            data["custom_interval_days"] = int(data["custom_interval_days"])

        if data.get("discount_pct"):
            try:
                data["discount_pct"] = float(data["discount_pct"])
            except ValueError:
                data.pop("discount_pct", None)

        line_items = []
        idx = 0
        while f"li_desc_{idx}" in data or f"li_qty_{idx}" in data:
            desc = data.pop(f"li_desc_{idx}", "").strip()
            qty_raw = data.pop(f"li_qty_{idx}", "1")
            price_raw = data.pop(f"li_price_{idx}", "0")
            if desc or qty_raw:
                try:
                    qty = float(qty_raw) if qty_raw else 1
                    price = float(price_raw) if price_raw else 0
                except ValueError:
                    qty, price = 1, 0
                line_items.append({"description": desc, "quantity": qty, "unit_price": price})
            idx += 1
        if line_items:
            data["line_items"] = line_items

        data.pop("status", None)

        try:
            result = await api.create_subscription(token, data)
            doc_id = result.get("entity_id") or result.get("id") or ""
            return RedirectResponse(f"/subscriptions/{doc_id}", status_code=303)
        except APIError as e:
            return RedirectResponse(f"/subscriptions/new?direction={direction}&error={e}", status_code=303)

    # --- Detail page ---

    @app.get("/subscriptions/{entity_id}")
    async def subscription_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            sub = await api.get_subscription(token, entity_id)
        except APIError:
            return RedirectResponse("/subscriptions", status_code=302)

        status = sub.get("status", "active")
        is_draft = status == "draft"
        direction = "sales" if sub.get("doc_type") == "subscription_invoice" else "purchasing"
        title = sub.get("name") or entity_id

        contact_name = sub.get("contact_name") or sub.get("contact_company_name") or ""
        if not contact_name and sub.get("contact_id"):
            try:
                contact = await api.get_contact(token, sub["contact_id"])
                contact_name = contact.get("name") or contact.get("company_name") or sub["contact_id"]
            except APIError:
                contact_name = sub["contact_id"]

        freq = sub.get("frequency", "")
        interval = sub.get("custom_interval_days")
        freq_display = freq.capitalize() + (f" ({interval} days)" if interval and freq == "custom" else "")

        actions = []
        if status != "cancelled":
            actions.append(
                Form(Button("Generate Now", type="submit", cls="btn btn--primary btn--sm"),
                     method="post", action=f"/subscriptions/{entity_id}/generate")
            )
        if status == "active":
            actions.append(
                Form(Button("Pause", type="submit", cls="btn btn--warning btn--sm"),
                     method="post", action=f"/subscriptions/{entity_id}/pause")
            )
        elif status == "paused":
            actions.append(
                Form(Button("Resume", type="submit", cls="btn btn--success btn--sm"),
                     method="post", action=f"/subscriptions/{entity_id}/resume")
            )
        if status != "cancelled":
            actions.append(
                Form(Button("Cancel", type="submit", cls="btn btn--danger btn--sm"),
                     method="post", action=f"/subscriptions/{entity_id}/cancel")
            )
        actions.append(A("Back", href=f"/subscriptions?direction={direction}", cls="btn btn--ghost btn--sm"))

        if is_draft:
            draft_notice = Div(
                P("\u270f\ufe0f This subscription template is in Draft. Click any field below to edit.", cls="text-muted"),
                cls="flash flash--info",
                style="margin-bottom:1rem",
            )
        else:
            draft_notice = None

        content = Div(
            page_header(title, *actions),
            draft_notice,

            Section(
                H3("Details", cls="section-title"),
                Div(
                    Div(Label("Name"), _sub_display_cell(entity_id, "name", sub.get("name")) if is_draft else P(sub.get("name") or "-")),
                    Div(Label("Contact"), P(contact_name or "-")),
                    Div(Label("Currency"), _sub_display_cell(entity_id, "currency", sub.get("currency")) if is_draft else P(sub.get("currency") or "-")),
                    Div(Label("Payment Terms"), _sub_display_cell(entity_id, "payment_terms", sub.get("payment_terms")) if is_draft else P(sub.get("payment_terms") or "-")),
                    Div(Label("Notes"), _sub_display_cell(entity_id, "notes", sub.get("notes")) if is_draft else P(sub.get("notes") or "-")),
                    cls="form-grid form-grid--2col",
                ),
                cls="section-card",
            ),

            Section(
                H3("Schedule", cls="section-title"),
                Div(
                    Div(Label("Frequency"), _sub_display_cell(entity_id, "frequency", sub.get("frequency")) if is_draft else P(freq_display or "-")),
                    Div(Label("Start Date"), _sub_display_cell(entity_id, "start_date", sub.get("start_date")) if is_draft else P(sub.get("start_date") or "-")),
                    Div(Label("Custom Interval (days)"), _sub_display_cell(entity_id, "custom_interval_days", sub.get("custom_interval_days")) if is_draft else P(str(sub.get("custom_interval_days") or "-"))),
                    Div(Label("Next Run"), P(sub.get("next_run_date") or "-")),
                    Div(Label("Status"), _sub_status_badge(status)),
                    cls="form-grid form-grid--2col",
                ),
                cls="section-card",
            ),

            Section(
                H3("Line Items", cls="section-title"),
                _line_items_section(sub),
                cls="section-card",
            ),

            Section(
                H3("Generated Documents", cls="section-title"),
                _generated_docs_table(sub.get("generated_doc_ids") or []),
                cls="section-card",
            ),
            cls="page-content",
        )
        return base_shell(request, content, title=title)

    # --- Lifecycle action endpoints ---

    @app.post("/subscriptions/{entity_id}/generate")
    async def generate_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.generate_subscription(token, entity_id)
        except APIError:
            pass
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/pause")
    async def pause_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.pause_subscription(token, entity_id)
        except APIError:
            pass
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/resume")
    async def resume_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.resume_subscription(token, entity_id)
        except APIError:
            pass
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/cancel")
    async def cancel_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.cancel_subscription(token, entity_id)
        except APIError:
            pass
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)
