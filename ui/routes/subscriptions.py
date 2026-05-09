# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Subscription template UI routes.

Subscription templates are docs with doc_type "subscription_invoice" or "subscription_po".
Creation uses POST /docs; lifecycle actions use the /subscriptions API.
"""
from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import searchable_select, empty_state_cta, fmt_money
from ui.config import get_token as _token, get_role as _get_role
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)

_FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "annually", "custom"]
_CURRENCIES = ["USD", "EUR", "GBP", "THB", "SGD", "AUD", "CAD", "JPY", "CNY"]

_STATUS_COLORS = {
    "active": "badge--active",
    "paused": "badge--paused",
    "cancelled": "badge--void",
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
        + '<td><button type="button" onclick="this.closest(\'tr\').remove()" class="btn btn--ghost btn--sm">✕</button></td>';
    tbody.appendChild(tr);
}
</script>
"""


def _sub_status_badge(status: str):
    css = _STATUS_COLORS.get(status, "badge--draft")
    return Span(status.capitalize(), cls=f"badge {css}")


def _schedule_section(sub: dict, lang: str):
    freq = sub.get("frequency", "-")
    start = sub.get("start_date", "-")
    next_run = sub.get("next_run_date", "-")
    status = sub.get("status", "active")
    interval = sub.get("custom_interval_days")
    return Section(
        H3("Schedule", cls="section-title"),
        Div(
            Div(Label("Frequency"), P(freq.capitalize() + (f" ({interval} days)" if interval and freq == "custom" else ""))),
            Div(Label("Start Date"), P(start)),
            Div(Label("Next Run"), P(next_run)),
            Div(Label("Status"), _sub_status_badge(status)),
            cls="form-grid form-grid--2col",
        ),
        cls="section-card",
    )


def _line_items_section(sub: dict):
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


def _generated_docs_table(doc_ids: list, lang: str):
    if not doc_ids:
        return P("No documents generated yet.", cls="text-muted")
    rows = [Tr(Td(A(did, href=f"/docs/{did}"))) for did in doc_ids]
    return Table(Thead(Tr(Th("Generated Document"))), Tbody(*rows), cls="data-table")


def _template_row(sub: dict, lang: str):
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


def _template_table(subs: list, lang: str):
    if not subs:
        return empty_state_cta("No subscription templates found.", "New Subscription", "/subscriptions/new")
    rows = [_template_row(s, lang) for s in subs]
    return Table(
        Thead(Tr(Th("Name"), Th("Frequency"), Th("Next Run"), Th("Status"))),
        Tbody(*rows),
        cls="data-table",
    )


def _direction_tabs(direction: str):
    return Nav(
        A("Sales", href="/subscriptions?direction=sales",
          cls=f"tab-link {'tab-link--active' if direction == 'sales' else ''}"),
        A("Purchasing", href="/subscriptions?direction=purchasing",
          cls=f"tab-link {'tab-link--active' if direction == 'purchasing' else ''}"),
        cls="tab-nav",
    )


def setup_routes(app) -> None:

    @app.get("/subscriptions")
    async def subscriptions_list(request: Request, direction: str = "sales", status: str | None = None):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
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
            _template_table(subs, lang),
            cls="page-content",
        )
        return base_shell(request, content, title=title)

    @app.get("/subscriptions/new")
    async def new_subscription_form(request: Request, direction: str = "sales"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        doc_type = "subscription_invoice" if direction == "sales" else "subscription_po"
        contact_filter = "customer" if direction == "sales" else "vendor"
        title = "New Subscription"
        error = request.query_params.get("error")

        try:
            contacts_resp = await api.list_contacts(token, {"limit": 500, "contact_type": contact_filter})
            contacts = contacts_resp.get("items", [])
        except APIError:
            contacts = []

        # Bug 1 fixed: tuples not dicts
        contact_opts = [(c.get("entity_id", ""), c.get("name") or c.get("entity_id", "")) for c in contacts]
        contact_opts.append(("__new__", "+ Add new contact"))

        # Initial line item row
        def _li_row(idx: int):
            return Tr(
                Td(Input(type="text", name=f"li_desc_{idx}", placeholder="Description", cls="cell-input", style="width:100%")),
                Td(Input(type="number", name=f"li_qty_{idx}", value="1", min="0", step="any", cls="cell-input", style="width:70px")),
                Td(Input(type="number", name=f"li_price_{idx}", value="0", min="0", step="any", cls="cell-input", style="width:100px")),
                Td(Button("✕", type="button", onclick="this.closest('tr').remove()", cls="btn btn--ghost btn--sm")),
            )

        form = Form(
            Script(_LINE_ITEMS_JS),
            Input(type="hidden", name="doc_type", value=doc_type),

            error and Div(P(f"Error: {error}"), cls="flash flash--error"),

            Section(
                H3("Basic Info", cls="section-title"),
                # Name
                Div(
                    Div(Label("Name *", cls="form-label"), Input(name="name", placeholder="e.g. Monthly Retainer", required=True, cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                # Contact
                Div(
                    Div(Label("Contact *", cls="form-label"), searchable_select("contact_id", contact_opts, placeholder="Search contact..."), cls="form-group"),
                    cls="form-row",
                ),
                # Frequency + Start Date
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
                # Currency + Payment Terms
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

            # Line items
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

            # Notes + Discount
            Section(
                H3("Additional", cls="section-title"),
                Div(
                    Div(Label("Discount %", cls="form-label"), Input(name="discount_pct", type="number", min="0", max="100", step="0.01", value="0", cls="form-input"), cls="form-group"),
                    Div(Label("Notes", cls="form-label"), Textarea(name="notes", rows="3", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                cls="section-card",
            ),

            Input(type="hidden", name="status", value="active"),

            Div(
                Button("Create Subscription", type="submit", cls="btn btn--primary"),
                A("Cancel", href=f"/subscriptions?direction={direction}", cls="btn btn--ghost"),
                cls="form-actions",
            ),
            method="post",
            action="/subscriptions/new",  # Bug 2 fixed: correct POST route
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

        # Strip empty custom_interval_days
        if not data.get("custom_interval_days"):
            data.pop("custom_interval_days", None)
        else:
            data["custom_interval_days"] = int(data["custom_interval_days"])

        # Convert discount_pct
        if data.get("discount_pct"):
            try:
                data["discount_pct"] = float(data["discount_pct"])
            except ValueError:
                data.pop("discount_pct", None)

        # Build line_items from dynamic form fields
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

        # Remove status from POST data to avoid overriding backend default
        data.pop("status", None)

        try:
            result = await api.create_subscription(token, data)
            doc_id = result.get("entity_id") or result.get("id") or ""
            return RedirectResponse(f"/subscriptions/{doc_id}", status_code=303)
        except APIError as e:
            return RedirectResponse(f"/subscriptions/new?direction={direction}&error={e}", status_code=303)

    @app.get("/subscriptions/{entity_id}")
    async def subscription_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            sub = await api.get_subscription(token, entity_id)  # Bug 3 fixed: calls /docs/{entity_id}
        except APIError:
            return RedirectResponse("/subscriptions", status_code=302)

        status = sub.get("status", "active")
        direction = "sales" if sub.get("doc_type") == "subscription_invoice" else "purchasing"
        title = sub.get("name") or entity_id

        # Resolve contact name
        contact_id = sub.get("contact_id") or ""
        contact_name = sub.get("contact_name") or sub.get("contact_company_name") or contact_id
        currency = sub.get("currency") or "-"

        actions = []
        if status == "active":
            actions.append(
                Form(Button("Generate Now", type="submit", cls="btn btn--primary btn--sm"),
                     method="post", action=f"/subscriptions/{entity_id}/generate")
            )
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

        content = Div(
            page_header(title, *actions),

            # Summary card
            Section(
                H3("Details", cls="section-title"),
                Div(
                    Div(Label("Contact"), P(contact_name or "-")),
                    Div(Label("Currency"), P(currency)),
                    Div(Label("Payment Terms"), P(sub.get("payment_terms") or "-")),
                    Div(Label("Notes"), P(sub.get("notes") or "-")),
                    cls="form-grid form-grid--2col",
                ),
                cls="section-card",
            ),

            _schedule_section(sub, lang),

            Section(
                H3("Line Items", cls="section-title"),
                _line_items_section(sub),
                cls="section-card",
            ),

            Section(
                H3("Generated Documents", cls="section-title"),
                _generated_docs_table(sub.get("generated_doc_ids") or [], lang),
                cls="section-card",
            ),
            cls="page-content",
        )
        return base_shell(request, content, title=title)

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
