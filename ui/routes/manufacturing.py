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
    # A run lives on its product's Manufacturing tab; link there (the opaque run page is gone).
    out_id = order.get("output_item_id")
    outs = order.get("expected_outputs") or [{}]
    label = outs[0].get("sku") or outs[0].get("name") or order.get("description") or EMPTY
    href = f"/inventory/{out_id}?tab=manufacturing" if out_id else None
    name_cell = A(label, href=href, cls="table-link") if href else Span(label)
    inputs = order.get("inputs", [])
    return Tr(
        Td(name_cell),
        Td(_badge(order.get("status", "planned"))),
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
        Thead(Tr(Th("Product"), Th(t("th.status")), Th(t("msg.created")), Th(t("th.inputs")))),
        Tbody(*[_order_row(o) for o in orders]),
        cls="data-table",
        id="mfg-table",
    )


# The opaque per-run detail page was removed in the product-centric overhaul. A run now lives on
# its product's Manufacturing tab (the production block) and in the In Production queue; status
# actions are handled there (see ui/routes/inventory.py production-block routes).


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
