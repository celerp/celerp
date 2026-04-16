# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.components.table import EMPTY, searchable_select, breadcrumbs, status_cards, empty_state_cta, pagination
from ui.config import get_token as _token
from ui.components.table import fmt_money
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)




_PER_PAGE = 50


def setup_ui_routes(app):

    @app.get("/subscriptions")
    async def subscriptions_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        page = int(request.query_params.get("page", 1))
        try:
            per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
        except (ValueError, TypeError):
            per_page = _PER_PAGE
        status_filter = request.query_params.get("status", "")
        params = {"limit": per_page, "offset": (page - 1) * per_page}
        if status_filter:
            params["status"] = status_filter
        try:
            resp = await api.list_subscriptions(token, params)
            subs = resp.get("items", []) if isinstance(resp, dict) else resp
            total = resp.get("total", len(subs)) if isinstance(resp, dict) else len(subs)
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return RedirectResponse("/login", status_code=302)
            subs, total = [], 0

        return base_shell(
            page_header(
                "Subscriptions",
                A(t("page.new_subscription"), href="/subscriptions/new", cls="btn btn--primary"),
                A(t("doc.import_csv"), href="/subscriptions/import", cls="btn btn--secondary"),
            ),
            _sub_status_cards(subs, status_filter),
            _sub_table(subs),
            pagination(page, total, per_page, "/subscriptions", f"status={status_filter}".strip("&=")),
            title="Subscriptions - Celerp",
            nav_active="subscriptions",
            request=request,
        )

    @app.get("/subscriptions/new")
    async def new_subscription(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            contact_resp = await api.list_contacts(token, {"limit": 500})
            contacts = contact_resp.get("items", [])
            terms = await api.get_payment_terms(token)
        except (APIError, Exception) as e:
            logger.warning("API error loading new subscription form: %s", getattr(e, 'detail', str(e)))
            contacts, terms = [], []
        return base_shell(
            page_header("New Subscription", A(t("btn.cancel"), href="/subscriptions", cls="btn btn--secondary")),
            _sub_form(contacts=contacts, terms=terms),
            title="New Subscription - Celerp",
            nav_active="subscriptions",
            request=request,
        )

    @app.post("/subscriptions/new")
    async def create_subscription(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        data = {
            "name": str(form.get("name", "")).strip(),
            "doc_type": str(form.get("doc_type", "invoice")),
            "frequency": str(form.get("frequency", "monthly")),
            "start_date": str(form.get("start_date", "")),
            "contact_id": str(form.get("contact_id", "")).strip() or None,
            "payment_terms": str(form.get("payment_terms", "")).strip() or None,
        }
        interval_days = str(form.get("interval_days", "")).strip()
        if interval_days.isdigit():
            data["interval_days"] = int(interval_days)
        try:
            result = await api.create_subscription(token, data)
            sub_id = result.get("id", "")
            return RedirectResponse(f"/subscriptions/{sub_id}" if sub_id else "/subscriptions", status_code=302)
        except (APIError, Exception) as e:
            try:
                contact_resp = await api.list_contacts(token, {"limit": 500})
                contacts = contact_resp.get("items", [])
                terms = await api.get_payment_terms(token)
            except APIError:
                contacts, terms = [], []
            return base_shell(
                page_header("New Subscription", A(t("btn.cancel"), href="/subscriptions", cls="btn btn--secondary")),
                flash(getattr(e, 'detail', str(e))),
                _sub_form(data, contacts=contacts, terms=terms),
                title="New Subscription - Celerp",
                nav_active="subscriptions",
                request=request,
            )

    @app.post("/subscriptions/{entity_id}/pause")
    async def pause_sub(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.pause_subscription(token, entity_id)
            sub = await api.get_subscription(token, entity_id)
            return _sub_row(sub)
        except (APIError, Exception) as e:
            logger.warning("API error on pause subscription %s: %s", entity_id, getattr(e, 'detail', str(e)))
            return RedirectResponse("/subscriptions", status_code=302)

    @app.post("/subscriptions/{entity_id}/resume")
    async def resume_sub(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.resume_subscription(token, entity_id)
            sub = await api.get_subscription(token, entity_id)
            return _sub_row(sub)
        except (APIError, Exception) as e:
            logger.warning("API error on resume subscription %s: %s", entity_id, getattr(e, 'detail', str(e)))
            return RedirectResponse("/subscriptions", status_code=302)

    @app.post("/subscriptions/{entity_id}/generate")
    async def generate_sub(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            result = await api.generate_subscription(token, entity_id)
            sub = await api.get_subscription(token, entity_id)
            return _sub_row(sub)
        except (APIError, Exception) as e:
            logger.warning("API error on generate subscription %s: %s", entity_id, getattr(e, 'detail', str(e)))
            return RedirectResponse("/subscriptions", status_code=302)

    # ── T4: Subscription detail page ──────────────────────────────────────

    @app.get("/subscriptions/{entity_id}")
    async def sub_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            sub = await api.get_subscription(token, entity_id)
            try:
                company = await api.get_company(token)
                currency = company.get("currency") or None
            except Exception:
                currency = None
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and getattr(e, 'status', None) == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/subscriptions", status_code=302)
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Subscriptions", "/subscriptions"), (sub.get("name", entity_id), None)]),
            page_header(sub.get("name", "Subscription"), A(t("btn.back_to_settings"), href="/subscriptions", cls="btn btn--secondary")),
            _sub_detail_card(sub, currency=currency),
            title=f"{sub.get('name', 'Subscription')} - Celerp",
            nav_active="subscriptions",
            request=request,
        )

    @app.get("/subscriptions/{entity_id}/field/{field}/edit")
    async def sub_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            sub = await api.get_subscription(token, entity_id)
        except (APIError, Exception) as e:
            return P(f"Error: {getattr(e, 'detail', str(e))}", cls="cell-error")
        val = str(sub.get(field, "") or "")
        _FREQ = ["weekly", "biweekly", "monthly", "quarterly", "annually", "custom"]
        if field == "frequency":
            input_el = Select(
                *[Option(f, value=f, selected=(f == val)) for f in _FREQ],
                name="value",
                hx_patch=f"/subscriptions/{entity_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
            )
        elif field in ("next_run", "end_date", "start_date"):
            input_el = Input(
                type="date", name="value", value=val[:10] if val else "",
                hx_patch=f"/subscriptions/{entity_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
            )
        elif field == "unit_price":
            input_el = Input(
                type="number", name="value", value=val, step="0.01",
                hx_patch=f"/subscriptions/{entity_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="blur delay:200ms",
                cls="cell-input cell-input--number", autofocus=True,
            )
        else:
            input_el = Input(
                type="text", name="value", value=val,
                hx_patch=f"/subscriptions/{entity_id}/field/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
            )
        return Td(input_el, cls="cell cell--editing")

    @app.patch("/subscriptions/{entity_id}/field/{field}")
    async def sub_field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        _EDITABLE_SUB = {"name", "frequency", "next_run", "end_date", "start_date", "payment_terms"}
        if field not in _EDITABLE_SUB:
            return P(t("label.not_editable"), cls="cell-error")
        try:
            await api.patch_subscription(token, entity_id, {field: value})
            sub = await api.get_subscription(token, entity_id)
        except (APIError, Exception) as e:
            return P(str(getattr(e, 'detail', str(e))), cls="cell-error")
        return _sub_display_cell(entity_id, field, sub.get(field))


def _sub_status_cards(subs: list[dict], active_status: str) -> FT:
    counts = {"active": 0, "paused": 0, "cancelled": 0}
    for s in subs:
        st = str(s.get("status") or "").lower()
        if st in counts:
            counts[st] += 1
    cards = [
        {"label": "Active", "count": counts["active"], "status": "active", "color": "green"},
        {"label": "Paused", "count": counts["paused"], "status": "paused", "color": "yellow"},
        {"label": "Cancelled", "count": counts["cancelled"], "status": "cancelled", "color": "gray"},
    ]
    return status_cards(cards, "/subscriptions", active_status or None)


def _sub_display_cell(entity_id: str, field: str, value) -> FT:
    if field in ("next_run", "end_date", "start_date"):
        display = str(value)[:10] if value else EMPTY
    else:
        display = str(value) if value and str(value).strip() else EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/subscriptions/{entity_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _sub_detail_card(sub: dict, currency: str | None = None) -> FT:
    eid = sub.get("entity_id", "")
    status = sub.get("status", "active")
    fields = [
        ("name", "Name"),
        ("doc_type", "Document Type"),
        ("frequency", "Frequency"),
        ("start_date", "Start Date"),
        ("next_run", "Next Generation Date"),
        ("end_date", "End Date"),
        ("payment_terms", "Payment Terms"),
        ("contact_id", "Contact"),
    ]
    _editable = {"name", "frequency", "next_run", "end_date", "start_date", "payment_terms"}

    def _cell(key: str, val) -> FT:
        if key in _editable:
            return _sub_display_cell(eid, key, val)
        if key == "doc_type":
            raw = str(val or "")
            key2 = raw.lower().replace(" ", "-").replace("_", "-")
            return Td(Span(raw.replace("_", " ").title() or EMPTY, cls=f"badge badge--{key2}"))
        if key == "status":
            raw = str(val or "")
            return Td(Span(raw or EMPTY, cls=f"badge badge--{raw.lower()}"))
        return Td(str(val) if val and str(val).strip() else EMPTY)

    line_items = sub.get("line_items", [])
    li_rows = []
    for li in line_items:
        li_rows.append(Tr(
            Td(str(li.get("item_id") or li.get("description") or EMPTY)),
            Td(str(li.get("quantity", 1))),
            Td(fmt_money(float(li.get('unit_price', 0)), currency), cls="cell--number"),
            cls="data-row",
        ))

    return Div(
        Div(
            Table(
                *[Tr(Td(label, cls="detail-label"), _cell(key, sub.get(key))) for key, label in fields],
                cls="detail-table",
            ),
            cls="detail-card",
        ),
        Div(
            H3(t("th.status"), cls="section-title"),
            Span(status, cls=f"badge badge--{status}"),
            cls="section mt-md mb-md",
        ),
        Div(
            H3(t("page.line_items"), cls="section-title"),
            Table(
                Thead(Tr(Th(t("th.itemdescription")), Th(t("th.qty")), Th(t("th.unit_price")))),
                Tbody(*li_rows) if li_rows else Tbody(Tr(Td(t("doc.no_line_items"), colspan="3", cls="empty-state-msg"))),
                cls="data-table data-table--compact",
            ),
            cls="section",
        ) if line_items else "",
    )


def _sub_row(s: dict) -> FT:
    """Render a single subscription table row (used for both full table and HTMX swaps)."""
    eid = s.get("entity_id", "")
    status = s.get("status", "active")
    is_paused = status == "paused"
    return Tr(
        Td(s.get("name", "") or EMPTY),
        Td(Span(s.get("doc_type", "").replace("_", " ").title(),
                cls=f"badge badge--{s.get('doc_type', '')}")),
        Td(s.get("frequency", "") or EMPTY),
        Td((s.get("next_run") or "")[:10] or EMPTY),
        Td(Span(status, cls=f"badge badge--{status}")),
        Td(
            Button(
                "Resume" if is_paused else "Pause",
                hx_post=f"/subscriptions/{eid}/{'resume' if is_paused else 'pause'}",
                hx_target=f"#sub-{eid}",
                hx_swap="outerHTML",
                cls="btn btn--xs btn--secondary",
            ),
            Button(t("btn.generate_now"),
                hx_post=f"/subscriptions/{eid}/generate",
                hx_target=f"#sub-{eid}",
                hx_swap="outerHTML",
                cls="btn btn--xs btn--primary ml-xs",
            ),
            cls="cell-actions",
        ),
        id=f"sub-{eid}",
        cls="data-row",
    )


def _sub_table(subs: list[dict]) -> FT:
    if not subs:
        return empty_state_cta("No subscriptions.", "Create Subscription", "/subscriptions/new")

    return Table(
        Thead(Tr(
            Th(t("th.name")), Th(t("th.doc_type")), Th(t("th.frequency")), Th(t("th.next_run")),
            Th(t("th.status")), Th(t("th.actions")),
        )),
        Tbody(*[_sub_row(s) for s in subs]),
        cls="data-table",
    )


def _sub_form(defaults: dict | None = None, contacts: list[dict] | None = None, terms: list[dict] | None = None) -> FT:
    d = defaults or {}
    contacts = contacts or []
    terms = terms or []

    # Contact picker: searchable if >10 contacts, plain input if fewer
    contact_opts = [(c.get("entity_id", ""), c.get("name", c.get("entity_id", ""))) for c in contacts]
    if len(contact_opts) > 10:
        contact_field = Div(
            Label(t("label.contact_optional"), For="contact_id", cls="form-label"),
            searchable_select(
                name="contact_id",
                options=contact_opts,
                value=d.get("contact_id", ""),
                placeholder="Search contacts...",
                cls_extra="form-input",
            ),
            cls="form-group",
        )
    else:
        contact_field = Div(
            Label(t("label.contact_id_optional"), For="contact_id", cls="form-label"),
            Input(type="text", id="contact_id", name="contact_id",
                  value=d.get("contact_id", ""), placeholder="contact:...", cls="form-input"),
            cls="form-group",
        )

    # Payment terms: searchable if >10 terms
    terms_opts = [term.get("name", "") for term in terms if term.get("name")]
    if len(terms_opts) > 10:
        terms_field = Div(
            Label(t("label.payment_terms_optional"), For="payment_terms", cls="form-label"),
            searchable_select(
                name="payment_terms",
                options=terms_opts,
                value=d.get("payment_terms", ""),
                placeholder="Select payment terms...",
                cls_extra="form-input",
            ),
            cls="form-group",
        )
    else:
        terms_field = Div(
            Label(t("label.payment_terms_optional"), For="payment_terms", cls="form-label"),
            Input(type="text", id="payment_terms", name="payment_terms",
                  value=d.get("payment_terms", ""), placeholder="Net 30", cls="form-input"),
            cls="form-group",
        )

    return Form(
        Div(
            Label(t("th.name"), For="name", cls="form-label"),
            Input(type="text", id="name", name="name", value=d.get("name", ""),
                  placeholder="e.g. Monthly Rent Invoice", required=True, cls="form-input"),
            cls="form-group",
        ),
        Div(
            Label(t("label.document_type"), For="doc_type", cls="form-label"),
            Select(
                Option(t("label.invoice"), value="invoice", selected=d.get("doc_type") == "invoice"),
                Option(t("label.purchase_order"), value="purchase_order", selected=d.get("doc_type") == "purchase_order"),
                id="doc_type", name="doc_type", cls="form-input",
            ),
            cls="form-group",
        ),
        Div(
            Label(t("th.frequency"), For="frequency", cls="form-label"),
            Select(
                Option(t("label.weekly"), value="weekly"),
                Option(t("label.monthly"), value="monthly", selected=True),
                Option(t("label.quarterly"), value="quarterly"),
                Option(t("label.annually"), value="annually"),
                Option(t("label.custom_days"), value="custom"),
                id="frequency", name="frequency", cls="form-input",
            ),
            cls="form-group",
        ),
        Div(
            Label(t("label.custom_interval_days"), For="interval_days", cls="form-label"),
            Input(type="number", id="interval_days", name="interval_days",
                  value=d.get("interval_days", ""), min="1", cls="form-input"),
            cls="form-group",
        ),
        Div(
            Label(t("label.start_date"), For="start_date", cls="form-label"),
            Input(type="date", id="start_date", name="start_date",
                  value=d.get("start_date", ""), required=True, cls="form-input"),
            cls="form-group",
        ),
        contact_field,
        terms_field,
        Button(t("btn.create_subscription"), type="submit", cls="btn btn--primary"),
        method="post",
        action="/subscriptions/new",
        cls="form-card",
    )
