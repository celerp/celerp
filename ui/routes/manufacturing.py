# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging
from datetime import date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import EMPTY, status_cards, empty_state_cta, format_value, search_bar, currency_symbol
from ui.config import get_token as _token
from ui.i18n import t

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


# Run priority labels (Phase-A scheduling). Order = ascending urgency for the picker.
_PRIORITIES = ("low", "normal", "high", "urgent")


def _priority_badge(priority: str | None) -> FT:
    p = (priority or "").lower()
    if p not in _PRIORITIES:
        return Span(EMPTY)
    return Span(p.title(), cls=f"badge badge--prio-{p}")


def _sched_sort(runs: list[dict]) -> list[dict]:
    """Scheduling order (GDR 2n): no-due-date first, then earliest due, then newest created.
    Two stable passes compose the key (newest-first then due-asc with undated on top)."""
    runs = sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True)
    runs.sort(key=lambda r: (r.get("due_date") is not None, r.get("due_date") or ""))
    return runs


def _order_row(order: dict, today: str = "") -> FT:
    # A run lives on its product's Manufacturing tab; link there (the opaque run page is gone).
    rid = order.get("id")
    out_id = order.get("output_item_id")
    outs = order.get("expected_outputs") or [{}]
    label = outs[0].get("sku") or outs[0].get("name") or order.get("description") or EMPTY
    href = f"/inventory/{out_id}?tab=manufacturing" if out_id else None
    name_cell = A(label, href=href, cls="table-link") if href else Span(label)
    status = order.get("status", "planned")
    due = order.get("due_date")
    overdue = bool(due and today and due < today and status not in ("completed", "cancelled"))
    inputs = order.get("inputs", [])
    # Double-click to edit due date / priority (system-standard click-to-edit). The editable-cell
    # chip lives in an inner Div - it is display:inline-block, so it must NOT be the <td> itself
    # (that would drop the cell out of the table's column layout). The cell passes its current value
    # so the edit fragment can prefill without a second fetch.
    due_cell = Td(
        Div(due or EMPTY, cls="editable-cell" + (" cell--alert" if overdue else ""),
            hx_get=f"/manufacturing/runs/{rid}/edit/due_date?current={due or ''}",
            hx_target="this", hx_swap="innerHTML", hx_trigger="dblclick",
            title="Double-click to set a due date"),
        cls="cell--center",
    )
    prio_cell = Td(
        Div(_priority_badge(order.get("priority")), cls="editable-cell",
            hx_get=f"/manufacturing/runs/{rid}/edit/priority?current={order.get('priority') or ''}",
            hx_target="this", hx_swap="innerHTML", hx_trigger="dblclick",
            title="Double-click to set priority"),
        cls="cell--center",
    )
    return Tr(
        Td(name_cell),
        Td(_badge(status)),
        prio_cell,
        due_cell,
        Td(format_value((order.get("created_at") or "")[:10])),
        Td(str(len(inputs)), cls="cell--number"),
        cls="data-row",
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


def _order_table(orders: list[dict], today: str = "") -> FT:
    if not orders:
        return Div(
            empty_state_cta("Nothing in production yet.", "View To Make", "/manufacturing"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(Th("Product"), Th(t("th.status")), Th("Priority", cls="cell--center"),
                 Th("Due", cls="cell--center"), Th(t("msg.created")), Th(t("th.inputs")))),
        Tbody(*[_order_row(o, today) for o in _sched_sort(orders)]),
        cls="data-table",
        id="mfg-table",
    )


# The opaque per-run detail page was removed in the product-centric overhaul. A run now lives on
# its product's Manufacturing tab (the production block) and in the In Production queue; status
# actions are handled there (see ui/routes/inventory.py production-block routes).


# ── Work Centers (operational stations, master data like Locations) ──────────

def _wc_cell(wc_id: str, field: str, value, *, center: bool = False) -> FT:
    disp = value if value not in (None, "") else EMPTY
    return Td(
        Div(disp, cls="editable-cell",
            hx_get=f"/manufacturing/work-centers/{wc_id}/edit/{field}?current={'' if value is None else value}",
            hx_target="this", hx_swap="innerHTML", hx_trigger="dblclick", title="Double-click to edit"),
        cls="cell--center" if center else "",
    )


def _wc_row(wc: dict, loc_names: dict) -> FT:
    wid = wc["id"]
    rate = wc.get("labor_rate")
    rate_disp = f"{float(rate):g}" if rate not in (None, "") else None
    cap = wc.get("capacity")
    cap_disp = f"{float(cap):g}" if cap not in (None, "") else None
    wip = loc_names.get(wc.get("wip_location_id")) if wc.get("wip_location_id") else None
    return Tr(
        _wc_cell(wid, "name", wc.get("name")),
        _wc_cell(wid, "wip_location_id", wip, center=True),
        _wc_cell(wid, "labor_rate", rate_disp, center=True),
        _wc_cell(wid, "capacity", cap_disp, center=True),
        Td(Button(t("btn.delete"), type="button", cls="btn btn--xs btn--secondary",
                  hx_post=f"/manufacturing/work-centers/{wid}/delete", hx_target="#wc-table",
                  hx_swap="outerHTML", hx_confirm="Delete this work center?"), cls="cell--actions"),
        cls="data-row",
    )


def _wc_table(centers: list[dict], loc_names: dict) -> FT:
    rows = [_wc_row(w, loc_names) for w in centers]
    return Table(
        Thead(Tr(Th("Name"), Th("WIP location", cls="cell--center"),
                 Th("Labor rate / hr", cls="cell--center"), Th("Capacity", cls="cell--center"),
                 Th("", cls="cell--actions"))),
        Tbody(*rows) if rows else Tbody(Tr(Td("No work centers yet.", colspan="5", cls="empty-row"))),
        cls="data-table", id="wc-table",
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
                _order_table(shown, today=date.today().isoformat()),
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
        return _order_table(orders, today=date.today().isoformat())

    async def _incomplete_runs_table(token: str) -> FT:
        """The In Production table for the active (incomplete) runs - used after a schedule edit."""
        try:
            orders = (await api.list_mfg_orders(token, {})).get("items", [])
        except APIError:
            orders = []
        shown = [o for o in orders if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
        return _order_table(shown, today=date.today().isoformat())

    @app.get("/manufacturing/runs/{run_id}/edit/{field}")
    async def run_field_edit(request: Request, run_id: str, field: str):
        """Inline editor for a run's due date / priority (double-click to edit on the queue)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        current = request.query_params.get("current", "")
        post = f"/manufacturing/runs/{run_id}/schedule"
        common = {"hx_post": post, "hx_target": "#mfg-table", "hx_swap": "outerHTML", "hx_trigger": "change"}
        if field == "priority":
            return Select(
                Option("--", value="", selected=(current == "")),
                *[Option(p.title(), value=p, selected=(p == current)) for p in _PRIORITIES],
                name="priority", cls="cell-input cell-input--select", **common,
            )
        # default: due_date
        return Input(type="date", name="due_date", value=current, cls="cell-input cell-input--xs", **common)

    @app.post("/manufacturing/runs/{run_id}/schedule")
    async def run_schedule(request: Request, run_id: str):
        """Persist a scheduling edit and refresh the In Production table."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        fields = {k: str(form[k]) for k in ("due_date", "priority", "planned_start") if k in form}
        if fields:
            try:
                await api.schedule_mfg_order(token, run_id, fields)
            except APIError as e:
                if e.status == 401:
                    return P(t("error.unauthorized"), cls="cell-error")
        return await _incomplete_runs_table(token)

    # ── Work Centers ──────────────────────────────────────────────────────
    async def _wc_table_response(token: str) -> FT:
        centers, locations = [], []
        try:
            centers = (await api.list_work_centers(token)).get("items", [])
            locations = (await api.get_locations(token)).get("items", [])
        except APIError:
            pass
        loc_names = {l.get("id"): l.get("name") for l in locations}
        return _wc_table(centers, loc_names)

    @app.get("/manufacturing/work-centers")
    async def work_centers_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header(
                "Work Centers",
                Button("Add work center", type="button", cls="btn btn--sm btn--primary",
                       hx_post="/manufacturing/work-centers/new", hx_target="#wc-table", hx_swap="outerHTML"),
            ),
            P("Operational stations (e.g. Bench, Polishing, Oven). Double-click a cell to edit.", cls="hint"),
            await _wc_table_response(token),
            title="Work Centers - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/work-centers/new")
    async def work_center_new(request: Request):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.create_work_center(token, {"name": "New work center"})
        except APIError:
            # A "New work center" already exists - just refresh; the user can rename it.
            pass
        return await _wc_table_response(token)

    @app.get("/manufacturing/work-centers/{wc_id}/edit/{field}")
    async def work_center_edit(request: Request, wc_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        current = request.query_params.get("current", "")
        post = f"/manufacturing/work-centers/{wc_id}/save/{field}"
        common = {"hx_post": post, "hx_target": "#wc-table", "hx_swap": "outerHTML", "hx_trigger": "blur, keyup[key=='Enter']"}
        if field == "wip_location_id":
            locations = []
            try:
                locations = (await api.get_locations(token)).get("items", [])
            except APIError:
                pass
            # current here is the location NAME (as displayed); match by name for the selected option.
            return Select(
                Option("--", value="", selected=(current in ("", EMPTY))),
                *[Option(l.get("name"), value=l.get("id"), selected=(l.get("name") == current)) for l in locations],
                name="value", cls="cell-input cell-input--select",
                hx_post=post, hx_target="#wc-table", hx_swap="outerHTML", hx_trigger="change",
            )
        if field in ("labor_rate", "capacity"):
            return Input(type="number", step="any", min="0", name="value",
                         value="" if current == EMPTY else current, cls="cell-input cell-input--xs", **common)
        return Input(type="text", name="value", value="" if current == EMPTY else current,
                     cls="cell-input cell-input--xs", **common)

    @app.post("/manufacturing/work-centers/{wc_id}/save/{field}")
    async def work_center_save(request: Request, wc_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        raw = str(form.get("value", "")).strip()
        if field in ("labor_rate", "capacity"):
            try:
                value = float(raw) if raw else None
            except ValueError:
                value = None
        else:
            value = raw or None
        if field == "name" and not value:
            return await _wc_table_response(token)  # ignore a blank rename
        try:
            await api.patch_work_center(token, wc_id, {field: value})
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
        return await _wc_table_response(token)

    @app.post("/manufacturing/work-centers/{wc_id}/delete")
    async def work_center_delete(request: Request, wc_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.delete_work_center(token, wc_id)
        except APIError:
            pass
        return await _wc_table_response(token)
