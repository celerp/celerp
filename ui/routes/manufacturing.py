# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.components.table import EMPTY, breadcrumbs, status_cards, empty_state_cta, format_value, add_new_option, searchable_select, search_bar, currency_symbol
from ui.config import get_token as _token
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)

# Canonical run statuses; "incomplete" = still needs attention (default queue view).
_INCOMPLETE_STATUSES = frozenset({"planned", "in_progress", "on_hold"})


def _company_cur(company: dict) -> str:
    """Company currency symbol. Currency lives under settings.currency (top-level is unset)."""
    code = company.get("currency") or (company.get("settings") or {}).get("currency") or ""
    return currency_symbol(code)




def _badge(status: str) -> FT:
    key = (status or "").lower().replace("_", "-")
    label = (status or "").replace("_", " ").title()
    return Span(label or EMPTY, cls=f"badge badge--{key}")


def _mfg_status_cards(orders: list[dict], active_status: str) -> FT:
    # Cards count the full set returned for the current view; the default view is "incomplete"
    # (planned + in_progress + on_hold), so completed/cancelled are reached via their own card.
    _CARD_DEFS = [
        ("planned", "Planned", "blue"),
        ("in_progress", "In Progress", "yellow"),
        ("on_hold", "On Hold", "orange"),
        ("completed", "Completed", "green"),
        ("cancelled", "Cancelled", "gray"),
    ]
    counts: dict[str, int] = {s: 0 for s, _, _ in _CARD_DEFS}
    for o in orders:
        s = str(o.get("status") or "").lower()
        if s in counts:
            counts[s] += 1
    cards = [
        {"label": label, "count": counts[s], "status": s, "color": color}
        for s, label, color in _CARD_DEFS
    ]
    return status_cards(cards, "/manufacturing", active_status or None)


def _order_row(order: dict) -> FT:
    oid = order.get("entity_id") or order.get("id", "")
    short_id = oid.split(":")[-1][:8] if oid else EMPTY
    inputs = order.get("inputs", [])
    return Tr(
        Td(A(f"#{short_id}", href=f"/manufacturing/{oid}", cls="link")),
        Td(format_value(order.get("order_type", order.get("description", "")))),
        Td(_badge(order.get("status", "draft"))),
        Td(format_value((order.get("created_at") or "")[:10])),
        Td(str(len(inputs)), cls="cell--number"),
    )


def _money(v, cur: str) -> str:
    if v in (None, ""):
        return EMPTY
    try:
        return f"{cur}{float(v):,.2f}"
    except (TypeError, ValueError):
        return EMPTY


def _to_make_row(r: dict, cur: str) -> FT:
    item_id = r.get("item_id", "")
    label = f"{r.get('sku') or item_id} - {r.get('name', '')}".strip(" -")
    due = r.get("due")
    return Tr(
        Td(A(label, href=f"/inventory/{item_id}?tab=manufacturing", cls="table-link")),
        Td(f"{float(r.get('to_make', 0)):g}", cls="cell--number"),
        Td(f"{float(r.get('demand', 0)):g} ({r.get('doc_count', 0)})", cls="cell--number",
           title=f"{float(r.get('demand', 0)):g} demanded across {r.get('doc_count', 0)} open document(s)"),
        Td(f"{float(r.get('on_hand', 0)):g}", cls="cell--number"),
        Td(due or EMPTY, cls="cell--center"),
        Td(_money(r.get("est_cost"), cur), cls="cell--number"),
        Td(f"{float(r.get('est_hours', 0)):g}", cls="cell--number"),
        Td(A("Make", href=f"/inventory/{item_id}?tab=manufacturing", cls="btn btn--xs btn--primary"),
           cls="cell--actions"),
        cls="data-row",
    )


def _to_make_table(rows: list[dict], cur: str) -> FT:
    if not rows:
        return Div(
            P("Nothing to make. Items appear here when an open invoice, pro forma, list or "
              "production order needs a product that has a recipe.", cls="hint"),
            id="mfg-table",
        )
    cl = f" ({cur})" if cur else ""
    # Headers centered (GDR 4a); numeric/currency CELLS right-aligned via cell--number on the td.
    return Table(
        Thead(Tr(
            Th("Item"), Th("To make"), Th("Demand"), Th("On hand"), Th("Due"),
            Th(f"Est. cost{cl}"), Th("Est. hours"), Th("", cls="cell--actions"),
        )),
        Tbody(*[_to_make_row(r, cur) for r in rows]),
        cls="data-table", id="mfg-table",
    )


def _order_table(orders: list[dict]) -> FT:
    if not orders:
        return Div(
            empty_state_cta("No production orders.", "Create Order", "/manufacturing/new"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(
            Th(t("th.order")), Th(t("th.doc_type")), Th(t("th.status")), Th(t("msg.created")), Th(t("th.inputs")),
        )),
        Tbody(*[_order_row(o) for o in orders]),
        cls="data-table",
        id="mfg-table",
    )


def _order_inputs_section(order: dict) -> FT:
    inputs = order.get("inputs", [])
    outputs = order.get("expected_outputs", [])
    steps_done = set(order.get("steps_completed", []))
    status = order.get("status", "draft")
    oid = order.get("entity_id", "")

    # T8: Inputs with consume buttons
    input_rows = []
    for inp in inputs:
        iid = inp.get("item_id", "")
        consumed_qty = float(inp.get("consumed_qty", 0) or 0)
        required_qty = float(inp.get("quantity", 0) or 0)
        consumed = f"consume:{iid}" in steps_done or consumed_qty >= required_qty

        consume_btn = ""
        if status == "in_progress" and not consumed:
            consume_btn = Details(
                Summary(t("mfg.consume"), cls="btn btn--primary btn--xs"),
                Form(
                    Div(
                        Label(t("th.qty"), cls="form-label"),
                        Input(type="number", name="quantity", value=str(required_qty - consumed_qty),
                              step="any", min="0", cls="form-input form-input--sm"),
                        cls="form-group",
                    ),
                    Input(type="hidden", name="item_id", value=iid),
                    Button(t("btn.confirm"), type="submit", cls="btn btn--primary btn--xs"),
                    hx_post=f"/manufacturing/{oid}/consume",
                    hx_target="#mfg-detail",
                    hx_swap="outerHTML",
                    cls="form-card",
                ),
            )

        input_rows.append(Tr(
            Td(format_value(iid)),
            Td(format_value(required_qty), cls="cell--number"),
            Td(str(consumed_qty), cls="cell--number"),
            Td("✓ Consumed" if consumed else "Pending", cls="cell--number"),
            Td(consume_btn),
        ))

    output_rows = [
        Tr(
            Td(format_value(o.get("sku"))),
            Td(format_value(o.get("name"))),
            Td(format_value(o.get("quantity")), cls="cell--number"),
        )
        for o in outputs
    ]

    # T8: Steps checklist
    steps = order.get("steps", [])
    step_rows = []
    for step in steps:
        sid = step.get("step_id", "")
        step_status = step.get("status", "pending")
        step_done = step_status in ("completed", "done")

        complete_btn = ""
        if status == "in_progress" and not step_done:
            complete_btn = Details(
                Summary(t("mfg.complete_step"), cls="btn btn--primary btn--xs"),
                Form(
                    Div(
                        Label(t("label.notes_optional"), cls="form-label"),
                        Textarea("", name="notes", rows="2", cls="form-input form-input--sm"),
                        cls="form-group",
                    ),
                    Input(type="hidden", name="step_id", value=sid),
                    Button(t("btn.confirm"), type="submit", cls="btn btn--primary btn--xs"),
                    hx_post=f"/manufacturing/{oid}/step",
                    hx_target="#mfg-detail",
                    hx_swap="outerHTML",
                    cls="form-card",
                ),
            )

        step_rows.append(Tr(
            Td("✓" if step_done else "○", cls="cell--number"),
            Td(str(step.get("name", sid))),
            Td(_badge(step_status)),
            Td(complete_btn),
        ))

    steps_section = ""
    if steps:
        steps_section = Div(
            H3(t("page.steps")),
            Table(
                Thead(Tr(Th(""), Th(t("th.step")), Th(t("th.status")), Th(""))),
                Tbody(*step_rows),
                cls="data-table data-table--compact",
            ),
            cls="steps-panel",
        )

    return Div(
        steps_section,
        Div(
            H3(t("page.inputs_bom")),
            Table(
                Thead(Tr(Th(t("label.item_id")), Th(t("th.required")), Th(t("th.consumed")), Th(t("th.status")), Th(""))),
                Tbody(*input_rows) if input_rows else Tbody(Tr(Td(t("mfg.no_inputs_defined"), colspan="5"))),
                cls="data-table data-table--compact",
            ),
            cls="bom-panel",
        ),
        Div(
            H3(t("page.expected_outputs")),
            Table(
                Thead(Tr(Th("SKU"), Th(t("th.name")), Th(t("th.quantity")))),
                Tbody(*output_rows) if output_rows else Tbody(Tr(Td(t("mfg.no_outputs_defined"), colspan="3"))),
                cls="data-table data-table--compact",
            ),
            cls="bom-panel",
        ),
        cls="bom-grid",
    )


def _action_buttons(order: dict, order_id: str) -> FT:
    status = order.get("status", "planned")
    btns = []

    def _post_btn(label, action, primary=True):
        return Form(
            Button(label, cls=f"btn btn--{'primary' if primary else 'secondary'}", type="submit"),
            method="post", action=f"/manufacturing/{order_id}/{action}",
            hx_post=f"/manufacturing/{order_id}/{action}", hx_target="#mfg-detail", hx_swap="outerHTML",
        )

    if status == "planned":
        btns.append(_post_btn(t("btn.start_order"), "start"))
    if status == "in_progress":
        btns.append(_post_btn(t("btn.complete_order"), "complete"))
        btns.append(_post_btn("Hold", "hold", primary=False))
    if status == "on_hold":
        btns.append(_post_btn("Resume", "resume"))
    if status not in ("completed", "cancelled"):
        btns.append(
            Form(
                Button(t("btn.cancel_order"), cls="btn btn--secondary", type="submit"),
                method="post", action=f"/manufacturing/{order_id}/cancel",
                hx_post=f"/manufacturing/{order_id}/cancel",
                hx_target="#mfg-detail",
                hx_swap="outerHTML",
            )
        )
    return Div(*btns, cls="action-bar") if btns else Div()


def _detail_panel(order: dict) -> FT:
    oid = order.get("entity_id", "")
    short_id = oid.split(":")[-1][:8] if oid else EMPTY
    return Div(
        Div(
            Div(
                Span(t("th.order"), cls="detail-label"),
                Span(f"#{short_id}", cls="detail-value"),
            ),
            Div(
                Span(t("th.doc_type"), cls="detail-label"),
                Span(format_value(order.get("order_type", order.get("description", ""))), cls="detail-value"),
            ),
            Div(
                Span(t("th.status"), cls="detail-label"),
                _badge(order.get("status", "draft")),
            ),
            Div(
                Span(t("th.description"), cls="detail-label"),
                Span(format_value(order.get("description")), cls="detail-value"),
            ),
            Div(
                Span(t("th.due_date"), cls="detail-label"),
                Span(format_value(order.get("due_date")), cls="detail-value"),
            ),
            Div(
                Span(t("mfg.est_cost"), cls="detail-label"),
                Span(format_value(order.get("estimated_cost")), cls="detail-value"),
            ),
            cls="detail-fields",
        ),
        _order_inputs_section(order),
        _action_buttons(order, oid),
        id="mfg-detail",
    )


def setup_routes(app):

    def _order_params(request: Request) -> tuple[dict, str, str, str]:
        """Shared q + date-range parsing for the orders list and its search fragment."""
        from ui.routes.reports import _date_filter_bar as _dfb, _parse_dates  # noqa: F401 (reused below)
        q = request.query_params.get("q", "")
        has_explicit_date = bool(request.query_params.get("preset")
                                 or request.query_params.get("from") or request.query_params.get("to"))
        if has_explicit_date:
            date_from, date_to, preset = _parse_dates(request)
        else:
            date_from, date_to, preset = "", "", "all"  # an order queue defaults to everything
        params: dict = {}
        if q:
            params["q"] = q
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return params, date_from, date_to, preset

    def _queue_tabs(active_tab: str) -> FT:
        # Two tabs of one page: "To Make" (product-demand board) and "In Production" (runs).
        return Div(
            A("To Make", href="/manufacturing", cls=f"category-tab{' category-tab--active' if active_tab == 'to_make' else ''}"),
            A("In Production", href="/manufacturing?tab=in_production",
              cls=f"category-tab{' category-tab--active' if active_tab == 'in_production' else ''}"),
            cls="category-tabs",
        )

    @app.get("/manufacturing")
    async def manufacturing_list(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        tab = "in_production" if request.query_params.get("tab") == "in_production" else "to_make"
        q = (request.query_params.get("q") or "").strip().lower()
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}
        cur = _company_cur(company)

        if tab == "to_make":
            try:
                rows = (await api.manufacturing_to_make(token)).get("items", [])
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                rows = []
            show_covered = request.query_params.get("view") == "all"
            if not show_covered:
                rows = [r for r in rows if float(r.get("to_make", 0)) > 0]
            if q:
                rows = [r for r in rows if q in f"{r.get('sku', '')} {r.get('name', '')}".lower()]
            body = (
                _queue_tabs("to_make"),
                P("Products with open demand to make, pooled across all open invoices, pro formas, "
                  "lists and production orders.", cls="hint"),
                Div(A("Show covered too" if not show_covered else "Hide covered",
                      href="/manufacturing?view=all" if not show_covered else "/manufacturing",
                      cls="btn btn--xs btn--ghost",
                      title="Also show products whose demand is already covered by on-hand stock"),
                    cls="mfg-filter-row"),
                _to_make_table(rows, cur),
            )
        else:
            try:
                orders_all = (await api.list_mfg_orders(token, {})).get("items", [])
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                orders_all = []
            active = (request.query_params.get("status") or "incomplete").lower()
            if active == "all":
                shown = orders_all
            elif active == "incomplete":
                shown = [o for o in orders_all if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
            else:
                shown = [o for o in orders_all if str(o.get("status") or "").lower() == active]
            body = (
                _queue_tabs("in_production"),
                Div(
                    _mfg_status_cards(orders_all, "" if active in ("incomplete", "all") else active),
                    A("All incomplete" if active != "all" else "Show all",
                      href="/manufacturing?tab=in_production" if active == "all" else "/manufacturing?tab=in_production&status=all",
                      cls="btn btn--xs btn--ghost"),
                    cls="mfg-filter-row",
                ),
                _order_table(shown),
            )

        search_url = "/manufacturing/to-make-search" if tab == "to_make" else "/manufacturing/search"
        return base_shell(
            page_header(
                "Production Queue",
                search_bar(placeholder="Search item / SKU...", target="#mfg-table", url=search_url),
            ),
            *body,
            title="Production Queue - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/to-make-search")
    async def to_make_search(request: Request):
        """To-Make board fragment for the header search box."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = (request.query_params.get("q") or "").strip().lower()
        try:
            company = await api.get_company(token)
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError:
            company, rows = {}, []
        rows = [r for r in rows if float(r.get("to_make", 0)) > 0]
        if q:
            rows = [r for r in rows if q in f"{r.get('sku', '')} {r.get('name', '')}".lower()]
        return _to_make_table(rows, _company_cur(company))

    @app.get("/manufacturing/search")
    async def manufacturing_search(request: Request):
        """Order-table fragment for the header search box (keeps the active date range)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        params, _f, _t, _p = _order_params(request)
        try:
            orders = (await api.list_mfg_orders(token, params)).get("items", [])
        except APIError:
            orders = []
        return _order_table(orders)

    async def _buildable_items(token: str) -> list[dict]:
        """Inventory items that have a manufacturing recipe (can be built)."""
        items = (await api.list_items(token, {"limit": 1000})).get("items", [])
        return [it for it in items if (it.get("recipe") or {}).get("components")]

    @app.get("/manufacturing/new")
    async def new_mfg_order(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            buildable = await _buildable_items(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            buildable = []
        return base_shell(
            page_header("New Manufacturing Order", A(t("btn.cancel"), href="/manufacturing", cls="btn btn--secondary")),
            _build_order_form(buildable),
            title="New Manufacturing Order - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/new")
    async def create_mfg_order(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        item_id = str(form.get("finished_item_id", "")).strip()
        try:
            qty = float(str(form.get("quantity", "1")))
        except ValueError:
            qty = 0.0

        async def _reshow(msg: str):
            try:
                buildable = await _buildable_items(token)
            except APIError:
                buildable = []
            return base_shell(
                page_header("New Manufacturing Order", A(t("btn.cancel"), href="/manufacturing", cls="btn btn--secondary")),
                flash(msg),
                _build_order_form(buildable, {"finished_item_id": item_id, "quantity": str(qty)}),
                title="New Manufacturing Order - Celerp",
                nav_active="manufacturing",
                request=request,
            )

        if not item_id or qty <= 0:
            return await _reshow("Select an item to build and a quantity greater than zero.")
        try:
            res = await api.build_item(token, item_id, qty)
            return RedirectResponse(f"/manufacturing/{res['id']}", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _reshow(e.detail)

    @app.get("/manufacturing/{order_id:path}")
    async def mfg_order_detail(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            order = await api.get_mfg_order(token, order_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if isinstance(e, APIError) and e.status == 404:
                return RedirectResponse("/manufacturing", status_code=302)
            order = {}
        oid = order.get("entity_id", order_id)
        short_id = oid.split(":")[-1][:8] if oid else order_id
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Manufacturing", "/manufacturing"), (f"Order #{short_id}", None)]),
            page_header(
                f"Manufacturing Order",
                A(t("btn.back_to_settings"), href="/manufacturing", cls="btn btn--secondary"),
            ),
            _detail_panel(order),
            title="Manufacturing Order - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/{order_id:path}/start")
    async def start_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.start_mfg_order(token, order_id)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    @app.post("/manufacturing/{order_id:path}/complete")
    async def complete_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.complete_mfg_order(token, order_id)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    @app.post("/manufacturing/{order_id:path}/cancel")
    async def cancel_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.cancel_mfg_order(token, order_id, reason)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    @app.post("/manufacturing/{order_id:path}/hold")
    async def hold_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.hold_mfg_order(token, order_id, reason)
            return _detail_panel(await api.get_mfg_order(token, order_id))
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    @app.post("/manufacturing/{order_id:path}/resume")
    async def resume_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.resume_mfg_order(token, order_id)
            return _detail_panel(await api.get_mfg_order(token, order_id))
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    # T8: Complete step
    @app.post("/manufacturing/{order_id:path}/step")
    async def complete_step_route(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        step_id = str(form.get("step_id", "")).strip()
        notes = str(form.get("notes", "")).strip() or None
        try:
            await api.complete_mfg_step(token, order_id, step_id, notes)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    # T8: Consume input
    @app.post("/manufacturing/{order_id:path}/consume")
    async def consume_input_route(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        item_id = str(form.get("item_id", "")).strip()
        try:
            quantity = float(str(form.get("quantity", "0")))
        except ValueError:
            quantity = 0.0
        try:
            await api.consume_mfg_input(token, order_id, item_id, quantity)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")


def _build_order_form(items: list[dict], prefill: dict | None = None) -> FT:
    """Build-order form: pick a manufacturable SKU + quantity; inputs expand from its recipe.

    Replaces the old free-form input-picker — the recipe now lives on the item, so an order
    is just "build N of this item" (GDR §2g — fewer clicks, no re-keying components).
    """
    p = prefill or {}
    if not items:
        return Div(
            P("No items have a manufacturing recipe yet. Define one first: open an inventory item "
              "and add components on its Manufacturing tab."),
            A("Go to inventory", href="/inventory", cls="btn btn--secondary"),
            cls="detail-card recipe-block",
        )
    opts = [(it.get("id") or it.get("entity_id"), f"{it.get('sku', '')} - {it.get('name', '')}".strip(" -")) for it in items]
    return Form(
        Div(
            Label("Item to build", For="finished_item_id"),
            searchable_select("finished_item_id", opts, value=p.get("finished_item_id", ""), placeholder="Search SKU…"),
            cls="form-group",
        ),
        Div(
            Label("Quantity to build", For="quantity"),
            Input(type="number", id="quantity", name="quantity", value=p.get("quantity", "1"), min="0.001", step="any", cls="form-input form-input--sm"),
            cls="form-group",
        ),
        P("Inputs are taken automatically from the item's recipe, scaled to this build quantity "
          "(a recipe that yields more than one unit per batch is divided down accordingly).", cls="hint"),
        Button("Create order", cls="btn btn--primary", type="submit"),
        method="post", action="/manufacturing/new", cls="form-card",
    )
