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

_STATUS_COLORS = {
    "active": "badge--active",
    "paused": "badge--paused",
    "cancelled": "badge--void",
}


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
        return empty_state_cta("No subscription templates found.", "/subscriptions/new", "New Subscription")
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
        title = "New Subscription"
        try:
            contacts_resp = await api.list_contacts(token, {"limit": 500})
            contacts = contacts_resp.get("items", [])
        except APIError:
            contacts = []

        contact_opts = [{"value": c.get("entity_id", ""), "label": c.get("name", c.get("entity_id", ""))} for c in contacts]

        form = Form(
            Input(type="hidden", name="doc_type", value=doc_type),
            Div(
                Label("Name *"),
                Input(name="name", placeholder="e.g. Monthly Retainer", required=True),
            ),
            Div(
                Label("Contact *"),
                searchable_select("contact_id", contact_opts, placeholder="Select contact..."),
            ),
            Div(
                Label("Frequency"),
                Select(
                    *[Option(f.capitalize(), value=f) for f in _FREQUENCIES],
                    name="frequency",
                    id="frequency-select",
                    onchange="document.getElementById('custom-interval-row').style.display = this.value === 'custom' ? '' : 'none'",
                ),
            ),
            Div(
                Label("Custom Interval (days)"),
                Input(name="custom_interval_days", type="number", min="1", placeholder="30"),
                id="custom-interval-row",
                style="display:none",
            ),
            Div(
                Label("Start Date *"),
                Input(name="start_date", type="date", required=True),
            ),
            Div(
                Label("Payment Terms"),
                Input(name="payment_terms", placeholder="e.g. Net 30"),
            ),
            Div(
                Label("Notes"),
                Textarea(name="notes", rows="3"),
            ),
            Div(
                Input(type="hidden", name="status", value="active"),
            ),
            Div(
                Button("Create Subscription", type="submit", cls="btn btn--primary"),
                A("Cancel", href=f"/subscriptions?direction={direction}", cls="btn btn--ghost"),
                cls="form-actions",
            ),
            method="post",
            action="/subscriptions/new/create",
            cls="form form--wide",
        )

        content = Div(page_header(title), form, cls="page-content")
        return base_shell(request, content, title=title)

    @app.post("/subscriptions/new")
    async def create_subscription(request: Request):
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
            sub = await api.get_subscription(token, entity_id)
        except APIError:
            return RedirectResponse("/subscriptions", status_code=302)

        status = sub.get("status", "active")
        direction = "sales" if sub.get("doc_type") == "subscription_invoice" else "purchasing"
        title = sub.get("name") or entity_id

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
                     method="post", action=f"/subscriptions/{entity_id}/cancel",
                     hx_confirm="Cancel this subscription? This cannot be undone.")
            )

        content = Div(
            page_header(title, *actions),
            _schedule_section(sub, lang),
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
