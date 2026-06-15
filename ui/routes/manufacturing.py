# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import json
import logging
from datetime import date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

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


def _mfg_status_cards(orders: list[dict], active_status: str, base_url: str) -> FT:
    """Status filter cards for the Work In Progress queue. 'Active' = planned+in_progress+on_hold
    (the default view); 'All' shows every run including completed/cancelled. Cards link within
    base_url (carrying the status) so the queue stays put when filtering."""
    _CARD_DEFS = [
        ("all", "All", "gray"),
        ("active", "Active", "blue"),
        ("planned", "Planned", "blue"),
        ("in_progress", "In Progress", "yellow"),
        ("on_hold", "On Hold", "orange"),
        ("completed", "Completed", "green"),
        ("cancelled", "Cancelled", "gray"),
    ]
    statuses = [str(o.get("status") or "").lower() for o in orders]
    counts: dict[str, int] = {
        "all": len(statuses),
        "active": sum(1 for s in statuses if s in _INCOMPLETE_STATUSES),
    }
    for s, _, _ in _CARD_DEFS:
        counts.setdefault(s, sum(1 for x in statuses if x == s))
    cards = [
        {"label": label, "count": counts[s], "status": s, "color": color}
        for s, label, color in _CARD_DEFS
    ]
    return status_cards(cards, base_url, active_status or None, show_all_card=False)


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


def _qty(value, unit: str | None) -> str:
    """Format a quantity with the product's sell unit, e.g. '2 Pieces'."""
    n = f"{float(value or 0):g}"
    return f"{n} {unit}" if unit else n


def _safe(entity_id: str) -> str:
    """DOM-id-safe form of an entity id (colons break CSS selectors)."""
    return (entity_id or "").replace(":", "-")


_PEG_LABELS = {"covered": "Covered", "partial": "Partial", "short": "Short"}


def _peg_badge(coverage: str) -> FT:
    return Span(_PEG_LABELS.get(coverage, coverage or EMPTY), cls=f"badge badge--peg-{coverage}")


def _docs_fragment(docs: list[dict], unit: str | None) -> FT:
    """The pegging drill-down: each demanding document with how much of it is covered by available
    supply (on hand + in progress), pegged soonest-due first."""
    if not docs:
        return P("No open documents drive this demand.", cls="hint")
    rows = []
    for d in sorted(docs, key=lambda x: (x.get("due") is None, x.get("due") or "")):
        rows.append(Tr(
            Td(d.get("doc_number") or EMPTY),
            Td((d.get("doc_type") or "").replace("_", " ").title()),
            Td(d.get("contact_name") or EMPTY),
            Td(d.get("due") or EMPTY, cls="cell--center"),
            Td(_qty(d.get("quantity", 0), unit), cls="cell--number"),
            Td(_qty(d.get("covered", 0), unit), cls="cell--number"),
            Td(_qty(d.get("shortfall", 0), unit), cls="cell--number"),
            Td(_peg_badge(d.get("coverage", "")), cls="cell--center"),
            cls="data-row",
        ))
    return Table(
        Thead(Tr(Th("Document"), Th("Type"), Th("For"), Th("Due", cls="cell--center"),
                 Th("Ordered"), Th("Covered"), Th("Short"), Th("Status", cls="cell--center"))),
        Tbody(*rows), cls="data-table data-table--nested",
    )


def _to_make_rows(r: dict, cur: str) -> list[FT]:
    """A product's data row plus its (hidden) pegging drill-down row, lazy-loaded on expand."""
    item_id = r.get("item_id", "")
    sid = _safe(item_id)
    label = f"{r.get('sku') or item_id} - {r.get('name', '')}".strip(" -")
    due = r.get("due")
    unit = r.get("unit")
    expand = Button(
        "▸", type="button", cls="dp-expand", title="Show the orders behind this demand",
        hx_get=f"/manufacturing/to-make/{item_id}/docs?unit={unit or ''}",
        hx_target=f"#dp-docs-{sid}", hx_swap="innerHTML",
        onclick=(f"this.classList.toggle('dp-expand--open');"
                 f"var r=document.getElementById('dp-docs-row-{sid}');if(r)r.hidden=!r.hidden;"),
    ) if r.get("docs") else Span(EMPTY)
    data_row = Tr(
        Td(Input(type="checkbox", cls="dp-select", name="selected", value=item_id), cls="col-checkbox"),
        Td(A(label, href=f"/inventory/{item_id}?tab=manufacturing", cls="table-link")),
        Td(_qty(r.get("to_make", 0), unit), cls="cell--number"),
        Td(f"{_qty(r.get('demand', 0), unit)} ({r.get('doc_count', 0)})", cls="cell--number",
           title=f"{float(r.get('demand', 0)):g} demanded across {r.get('doc_count', 0)} open document(s)"),
        Td(_qty(r.get("on_hand", 0), unit), cls="cell--number"),
        Td(_qty(r.get("in_progress", 0), unit), cls="cell--number"),
        Td(due or EMPTY, cls="cell--center"),
        Td(_money(r.get("est_cost"), cur), cls="cell--number"),
        Td(f"{float(r.get('est_hours', 0)):g}", cls="cell--number"),
        Td(expand, cls="cell--center"),
        cls="data-row",
    )
    detail_row = Tr(
        Td(Div(id=f"dp-docs-{sid}"), colspan="10", cls="dp-docs-cell"),
        id=f"dp-docs-row-{sid}", cls="dp-docs-row", hidden=True,
    )
    return [data_row, detail_row]


def _to_make_table(rows: list[dict], cur: str) -> FT:
    if not rows:
        return Div(
            P("Nothing to make. Items appear here when an open invoice, list or production order "
              "needs more of a product than you have in stock or already in production.", cls="hint"),
            id="mfg-table",
        )
    cl = f" ({cur})" if cur else ""
    body = [el for r in rows for el in _to_make_rows(r, cur)]
    # Headers centered (GDR 4a); numeric/currency CELLS right-aligned via cell--number on the td.
    return Table(
        Thead(Tr(
            Th(Input(type="checkbox", id="dp-select-all", title="Select all"), cls="col-checkbox"),
            Th("Item"), Th("To make"), Th("Demand"), Th("On hand"), Th("In progress"), Th("Due"),
            Th(f"Est. cost{cl}"), Th("Est. hours"), Th("", cls="cell--actions"),
        )),
        Tbody(*body),
        cls="data-table", id="mfg-table",
    )


# Demand-Planning row selection: select-all toggle + a live "[N selected]" count that enables the
# bulk Make button. Only checked .dp-select boxes submit (hx-include), so no hidden field is needed.
_DP_SELECT_JS = """
(function(){
  function boxes(){return Array.prototype.slice.call(document.querySelectorAll('#mfg-table .dp-select'));}
  function update(){
    var b=boxes(),n=b.filter(function(c){return c.checked}).length;
    var cnt=document.getElementById('dp-count');if(cnt)cnt.textContent=n+' selected';
    var btn=document.getElementById('dp-make-btn');if(btn)btn.disabled=n===0;
    var all=document.getElementById('dp-select-all');
    if(all){all.checked=n>0&&n===b.length;all.indeterminate=n>0&&n<b.length;}
  }
  document.addEventListener('change',function(e){
    var t=e.target;if(!t)return;
    if(t.id==='dp-select-all'){boxes().forEach(function(c){c.checked=t.checked});update();}
    else if(t.classList&&t.classList.contains('dp-select')){update();}
  });
  document.addEventListener('htmx:afterSwap',function(e){
    if(e.detail&&e.detail.target&&e.detail.target.id==='mfg-table')update();
  });
  update();
})();
"""


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

    def _intro(icon: str, text: str) -> FT:
        return Div(Span(icon, cls="info-banner-icon"), Span(text), cls="info-banner")

    @app.get("/manufacturing")
    async def demand_planning(request: Request):
        """Demand Planning board: products with more open demand than on-hand + in-progress supply.
        Tick products and Make to start runs; expand a row for the per-order pegging breakdown."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        # Legacy deep links to the runs queue moved to /manufacturing/production.
        if request.query_params.get("tab") == "in_production":
            status = request.query_params.get("status")
            return RedirectResponse(
                "/manufacturing/production" + (f"?status={status}" if status else ""), status_code=302)
        q = (request.query_params.get("q") or "").strip().lower()
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}
        cur = _company_cur(company)
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
            _intro("📊", "What you need to make to fill open orders. Each row is a product with more "
                         "demand (from invoices, lists and production orders) than you have in stock or "
                         "already in production. Tick products and choose Make to start runs; click the "
                         "arrow to see which orders drive each shortfall."),
            Div(
                Span("0 selected", id="dp-count", cls="bulk-count"),
                Button("Make selected", id="dp-make-btn", type="button", disabled=True,
                       cls="btn btn--sm btn--primary",
                       hx_post="/manufacturing/make-selected", hx_include=".dp-select",
                       hx_target="#mfg-table", hx_swap="outerHTML",
                       title="Start a production run for each selected product at its shortfall"),
                A("Show covered too" if not show_covered else "Hide covered",
                  href="/manufacturing?view=all" if not show_covered else "/manufacturing",
                  cls="btn btn--xs btn--ghost",
                  title="Also show products already covered by stock or in-progress runs"),
                cls="bulk-action-bar mfg-filter-row", id="dp-bulkbar",
            ),
            _to_make_table(rows, cur),
            Script(_DP_SELECT_JS),
        )
        return base_shell(
            page_header(
                "Demand Planning",
                search_bar(placeholder="Search item / SKU...", target="#mfg-table",
                           url="/manufacturing/to-make-search"),
            ),
            *body,
            title="Demand Planning - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/production")
    async def work_in_progress(request: Request):
        """Work In Progress: the production-run queue (issue components, then receive finished goods).
        Status cards are the single filter; the default view is Active (planned/in_progress/on_hold)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = (request.query_params.get("q") or "").strip().lower()
        try:
            orders_all = (await api.list_mfg_orders(token, {})).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            orders_all = []
        active = (request.query_params.get("status") or "active").lower()
        if active in ("active", "incomplete"):
            active = "active"
            shown = [o for o in orders_all if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
        elif active == "all":
            shown = orders_all
        else:
            shown = [o for o in orders_all if str(o.get("status") or "").lower() == active]
        if q:
            shown = [o for o in shown
                     if q in " ".join(str(x.get("sku", "")) for x in o.get("expected_outputs", [])).lower()
                     or q in str(o.get("description", "")).lower()]
        body = (
            _intro("🏭", "Production runs on the floor. Issue components to start a run, then receive "
                         "finished goods to restock. Runs you start from Demand Planning appear here. "
                         "Double-click a due date or priority to edit; press Esc to cancel."),
            _mfg_status_cards(orders_all, active, "/manufacturing/production"),
            _order_table(shown, today=date.today().isoformat()),
        )
        return base_shell(
            page_header(
                "Work In Progress",
                search_bar(placeholder="Search run / SKU...", target="#mfg-table",
                           url="/manufacturing/search"),
            ),
            *body,
            title="Work In Progress - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/to-make/{item_id}/docs")
    async def to_make_docs(request: Request, item_id: str):
        """The pegging drill-down fragment for one product on the Demand Planning board."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        unit = request.query_params.get("unit") or None
        try:
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError:
            rows = []
        row = next((r for r in rows if r.get("item_id") == item_id), None)
        return _docs_fragment((row or {}).get("docs", []), unit)

    @app.post("/manufacturing/make-selected")
    async def make_selected(request: Request):
        """Bulk 'Make selected': build each ticked product at its shortfall, then refresh the board."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        ids = form.getlist("selected")
        cur = ""
        result: dict = {"built": []}
        rows: list[dict] = []
        try:
            company = await api.get_company(token)
            cur = _company_cur(company)
            if ids:
                result = await api.manufacturing_bulk_build(token, ids)
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
        rows = [r for r in rows if float(r.get("to_make", 0)) > 0]
        built = len(result.get("built", []))
        msg = (f"Started {built} production run(s)." if built
               else "Nothing to make for the selected products.")
        return HTMLResponse(
            to_xml(_to_make_table(rows, cur)),
            headers={"HX-Trigger": json.dumps(
                {"celerpToast": {"message": msg, "type": "success" if built else "info"}})},
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
        # ESC cancels the edit: restore the cell's display chip (innerHTML of the editable-cell)
        # without persisting. Mirrors the inventory inline-edit escape pattern.
        escape_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','/manufacturing/runs/{run_id}/cell/{field}?current={current}',"
            f"{{target:this.closest('.editable-cell'),swap:'innerHTML'}});"
            f"event.preventDefault();event.stopPropagation();}}"
        )
        common = {"hx_post": post, "hx_target": "#mfg-table", "hx_swap": "outerHTML",
                  "hx_trigger": "change", "onkeydown": escape_js}
        if field == "priority":
            return Select(
                Option("--", value="", selected=(current == "")),
                *[Option(p.title(), value=p, selected=(p == current)) for p in _PRIORITIES],
                name="priority", cls="cell-input cell-input--select", **common,
            )
        # default: due_date
        return Input(type="date", name="due_date", value=current, cls="cell-input cell-input--xs", **common)

    @app.get("/manufacturing/runs/{run_id}/cell/{field}")
    async def run_field_cell(request: Request, run_id: str, field: str):
        """Restore an inline cell to its display chip (ESC-cancel from the editor)."""
        current = request.query_params.get("current", "")
        if field == "priority":
            return _priority_badge(current or None)
        return current or EMPTY

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
        # Work centers are now configured in Manufacturing Settings; keep the old URL working.
        return RedirectResponse("/settings/manufacturing#work-centers", status_code=302)

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
