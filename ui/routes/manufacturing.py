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
from ui.components.table import (EMPTY, status_cards, empty_state_cta, format_value, search_bar,
                                 bulk_toolbar, filter_th, COLUMN_FILTER_JS)
from ui.config import get_token as _token
from ui.i18n import t

logger = logging.getLogger(__name__)

# Canonical run statuses; "incomplete" = still needs attention (default queue view).
_INCOMPLETE_STATUSES = frozenset({"planned", "in_progress", "on_hold"})

# Plain-language help for the status indicators, shown as hover tooltips so users know what each means.
RUN_STATUS_HELP = {
    "planned": "Created but not started - components not yet issued.",
    "in_progress": "Started - components issued and the run is being worked.",
    "on_hold": "Paused - work stopped for now; can be resumed.",
    "completed": "Finished - components consumed and finished goods received into stock.",
    "cancelled": "Stopped - this run will not be produced.",
}
COVERAGE_HELP = {
    "short": "Needed - no stock or in-progress run covers this order line; it must be made.",
    "partial": "Partial - some stock or in-progress production covers it; the rest must be made.",
    "covered": "Covered - fully covered by on-hand stock or in-progress runs; nothing to make.",
}


def _badge(status: str) -> FT:
    raw = (status or "").lower()
    label = (status or "").replace("_", " ").title()
    help_txt = RUN_STATUS_HELP.get(raw, "")
    return Span(label or EMPTY, cls=f"badge badge--{raw.replace('_', '-')}",
                **({"title": help_txt} if help_txt else {}))


def _mfg_status_cards(orders: list[dict], active_status: str, base_url: str) -> FT:
    """Status filter cards for the Work In Progress queue. 'Active' = planned+in_progress+on_hold
    (the default view). There is no 'All' card - we never show completed/cancelled and active runs
    together. Cards link within base_url (carrying the status) so the queue stays put when filtering."""
    _CARD_DEFS = [
        ("active", "Active", "blue"),
        ("planned", "Planned", "blue"),
        ("in_progress", "In Progress", "yellow"),
        ("on_hold", "On Hold", "orange"),
        ("completed", "Completed", "green"),
        ("cancelled", "Cancelled", "gray"),
    ]
    statuses = [str(o.get("status") or "").lower() for o in orders]
    counts: dict[str, int] = {
        "active": sum(1 for s in statuses if s in _INCOMPLETE_STATUSES),
    }
    for s, _, _ in _CARD_DEFS:
        counts.setdefault(s, sum(1 for x in statuses if x == s))
    card_help = {
        "active": "Runs still needing attention: planned, in progress or on hold.",
        **RUN_STATUS_HELP,
    }
    cards = [
        {"label": label, "count": counts[s], "status": s, "color": color, "title": card_help.get(s, "")}
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
    src_id, src_no = order.get("source_doc_id"), order.get("source_doc_number")
    if src_id and src_no:
        src_href = f"/lists/{src_id}" if str(src_id).startswith("list:") else f"/docs/{src_id}"
        src_cell = A(src_no, href=src_href, cls="table-link")
    else:
        src_cell = Span("To stock", cls="hint")
    return Tr(
        Td(Input(type="checkbox", cls="bulk-select", name="selected", value=rid), cls="col-checkbox"),
        Td(name_cell),
        Td(_badge(status)),
        prio_cell,
        due_cell,
        Td(format_value((order.get("created_at") or "")[:10])),
        Td(str(len(inputs)), cls="cell--number"),
        Td(src_cell),
        cls="data-row",
    )


def _qty(value, unit: str | None) -> str:
    """Format a quantity with the product's sell unit, e.g. '2 Pieces'."""
    n = f"{float(value or 0):g}"
    return f"{n} {unit}" if unit else n


# Demand-line coverage from FIFO pegging (supply = on hand + in progress), relabelled for the board.
_STATUS_LABELS = {"short": "Needed", "partial": "Partial", "covered": "Covered"}
_STATUS_ORDER = {"short": 0, "partial": 1, "covered": 2}  # needed-first sort
_DTYPE_DEFS = [("all", "All"), ("invoice", "Invoices"),
               ("production_order", "Production Orders"), ("list", "Lists")]


def _status_badge(coverage: str) -> FT:
    help_txt = COVERAGE_HELP.get(coverage, "")
    return Span(_STATUS_LABELS.get(coverage, coverage or EMPTY), cls=f"badge badge--peg-{coverage}",
                **({"title": help_txt} if help_txt else {}))




def _doc_href(doc_id: str, doc_type: str) -> str:
    return f"/lists/{doc_id}" if doc_type == "list" else f"/docs/{doc_id}"


def _demand_lines(rows: list[dict]) -> list[dict]:
    """Flatten the product-centric to-make rows into one entry per demanding document line, each
    carrying its product and the document's pegged coverage. Needed first, then partial, then
    covered; within a status, soonest due first (undated last)."""
    lines: list[dict] = []
    for r in rows:
        unit = r.get("unit")
        for d in r.get("docs", []):
            lines.append({
                "item_id": r.get("item_id", ""), "sku": r.get("sku"), "name": r.get("name"), "unit": unit,
                "doc_id": d.get("doc_id"), "doc_number": d.get("doc_number"),
                "doc_type": d.get("doc_type") or "", "contact_name": d.get("contact_name") or "",
                "due": d.get("due"), "quantity": d.get("quantity", 0),
                "shortfall": d.get("shortfall", 0), "coverage": d.get("coverage") or "short",
            })
    lines.sort(key=lambda l: (_STATUS_ORDER.get(l["coverage"], 0),
                              l["due"] is None, l["due"] or "", l["sku"] or l["name"] or ""))
    return lines


def _demand_row(l: dict) -> FT:
    item_id = l.get("item_id", "")
    label = f"{l.get('sku') or item_id} - {l.get('name', '')}".strip(" -")
    unit = l.get("unit")
    doc_type = l.get("doc_type") or ""
    doc_no = l.get("doc_number") or EMPTY
    doc_cell = (A(doc_no, href=_doc_href(l.get("doc_id"), doc_type), cls="table-link")
                if l.get("doc_id") else Span(doc_no))
    return Tr(
        # value carries the demand line (product + its document) so each makes a 1:1-linked work order.
        Td(Input(type="checkbox", cls="bulk-select", name="selected", value=f"{item_id}|{l.get('doc_id') or ''}"),
           cls="col-checkbox"),
        Td(A(label, href=f"/inventory/{item_id}?tab=manufacturing", cls="table-link")),
        Td(doc_cell),
        Td(doc_type.replace("_", " ").title() or EMPTY),
        Td(l.get("contact_name") or EMPTY),
        Td(l.get("due") or EMPTY, cls="cell--center"),
        Td(_qty(l.get("quantity", 0), unit), cls="cell--number"),
        Td(_qty(l.get("shortfall", 0), unit), cls="cell--number"),
        Td(_status_badge(l.get("coverage", "")), cls="cell--center"),
        cls="data-row",
    )


def _demand_table(lines: list[dict]) -> FT:
    if not lines:
        return Div(
            P("Nothing to make for this view. Demand lines appear here when an open invoice, list or "
              "production order needs more of a product than you have in stock or already in production.",
              cls="hint"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(
            Th(Input(type="checkbox", cls="bulk-select-all", title="Select all"), cls="col-checkbox"),
            Th("Product"), Th("Document"), Th("Type"), filter_th("For", 4), Th("Due", cls="cell--center"),
            Th("Ordered", cls="cell--number"), Th("Short", cls="cell--number"),
            filter_th("Status", 8, center=True),
        )),
        Tbody(*[_demand_row(l) for l in lines]),
        cls="data-table", id="mfg-table",
    )


def _demand_filter(lines: list[dict], dtype: str) -> list[dict]:
    """Filter flattened demand lines by document type (the board's single main filter).
    Finer filters (status, customer) are applied client-side via the column funnels."""
    if dtype == "all":
        return lines
    return [l for l in lines if l["doc_type"] == dtype]


def _type_filter_bar(all_lines: list[dict], dtype: str) -> FT:
    """The board's main filter: document type, with per-type line counts (cards like the WIP queue)."""
    counts = {"all": len(all_lines)}
    for key, _ in _DTYPE_DEFS[1:]:
        counts[key] = sum(1 for l in all_lines if l["doc_type"] == key)
    cards = [{"label": lbl, "count": counts.get(k, 0), "status": k,
              "color": "gray" if k == "all" else "blue",
              "_url": f"/manufacturing?type={k}", "_active_key": k}
             for k, lbl in _DTYPE_DEFS]
    return status_cards(cards, "/manufacturing", dtype, show_all_card=False)




def _order_table(orders: list[dict], today: str = "") -> FT:
    if not orders:
        return Div(
            empty_state_cta("Nothing in production yet.", "Go to Demand Planning", "/manufacturing"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(
            Th(Input(type="checkbox", cls="bulk-select-all", title="Select all"), cls="col-checkbox"),
            filter_th("Product", 1), Th(t("th.status")), filter_th("Priority", 3, center=True),
            Th("Due", cls="cell--center"), Th(t("msg.created")), Th(t("th.inputs"), cls="cell--number"),
            Th("Source order"),
        )),
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

    def _dp_filter_args(request: Request) -> tuple[str, str]:
        """(document-type, search) from the query string. Type is the board's main filter; finer
        filters (status, customer) are client-side column funnels."""
        dtype = (request.query_params.get("type") or "all").lower()
        q = (request.query_params.get("q") or "").strip().lower()
        return dtype, q

    def _dp_search(lines: list[dict], q: str) -> list[dict]:
        if not q:
            return lines
        return [l for l in lines
                if q in f"{l.get('sku', '')} {l.get('name', '')} {l.get('doc_number', '')}".lower()]

    @app.get("/manufacturing")
    async def demand_planning(request: Request):
        """Demand Planning board: one line per open demand document, with the product it needs and
        how well current supply (on hand + in progress) covers it. Document type is the main filter;
        the Status and For columns have Excel-style funnels. Tick lines and Make to start runs."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        # Legacy deep links to the runs queue moved to /manufacturing/production.
        if request.query_params.get("tab") == "in_production":
            status = request.query_params.get("status")
            return RedirectResponse(
                "/manufacturing/production" + (f"?status={status}" if status else ""), status_code=302)
        dtype, q = _dp_filter_args(request)
        try:
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            rows = []
        all_lines = _demand_lines(rows)
        lines = _dp_search(_demand_filter(all_lines, dtype), q)
        body = (
            _intro("📊", "Every open order that needs something made, and how well current stock and "
                         "in-progress runs cover it. Pick a document type below, or use the column "
                         "filters to narrow by status or customer; tick the lines you want and choose "
                         "Make to start a production run for each product that is short."),
            _type_filter_bar(all_lines, dtype),
            bulk_toolbar("mfg-table", [
                {"value": "make", "label": "Make selected",
                 "method": "post", "url": f"/manufacturing/make-selected?type={dtype}"},
                {"value": "make_complete", "label": "Make & complete",
                 "method": "post", "url": f"/manufacturing/make-selected?type={dtype}&complete=1",
                 "confirm": "Build the selected products now - issue components and receive the "
                            "finished goods? This updates stock and posts journal entries."},
                {"value": "requirements", "label": "Print component requirements",
                 "method": "open", "url": "/manufacturing/requirements"},
            ]),
            _demand_table(lines),
            Script(COLUMN_FILTER_JS),
        )
        return await base_shell(
            page_header(
                "Demand Planning",
                search_bar(placeholder="Search product / document...", target="#mfg-table",
                           url=f"/manufacturing/to-make-search?type={dtype}"),
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
        # "all" is no longer a view (completed/cancelled and active runs are never shown together);
        # legacy ?status=all links fall back to the default Active queue.
        if active in ("active", "incomplete", "all"):
            active = "active"
            shown = [o for o in orders_all if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
        else:
            shown = [o for o in orders_all if str(o.get("status") or "").lower() == active]
        if q:
            shown = [o for o in shown
                     if q in " ".join(str(x.get("sku", "")) for x in o.get("expected_outputs", [])).lower()
                     or q in str(o.get("description", "")).lower()]
        body = (
            _intro("🏭", "Production runs on the floor. Tick runs and choose an action to advance "
                         "them - issue components to start, receive finished goods to restock. Runs you "
                         "start from Demand Planning appear here. Double-click a due date or priority to "
                         "edit; press Esc to cancel."),
            _mfg_status_cards(orders_all, active, "/manufacturing/production"),
            bulk_toolbar("mfg-table", [
                {"value": "start", "label": "Start", "method": "post",
                 "url": f"/manufacturing/runs/bulk/start?status={active}"},
                {"value": "issue", "label": "Issue components", "method": "post",
                 "url": f"/manufacturing/runs/bulk/issue?status={active}"},
                {"value": "complete", "label": "Complete", "method": "post",
                 "url": f"/manufacturing/runs/bulk/complete?status={active}",
                 "confirm": "Complete the selected runs now - issue any outstanding components and "
                            "receive the finished goods? This updates stock and posts journal entries."},
                {"value": "hold", "label": "Put on hold", "method": "post",
                 "url": f"/manufacturing/runs/bulk/hold?status={active}"},
                {"value": "resume", "label": "Resume", "method": "post",
                 "url": f"/manufacturing/runs/bulk/resume?status={active}"},
                {"value": "cancel", "label": "Cancel", "method": "post",
                 "url": f"/manufacturing/runs/bulk/cancel?status={active}",
                 "confirm": "Cancel the selected production runs?"},
            ]),
            _order_table(shown, today=date.today().isoformat()),
            Script(COLUMN_FILTER_JS),
        )
        return await base_shell(
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

    @app.post("/manufacturing/make-selected")
    async def make_selected(request: Request):
        """Bulk 'Make selected' / 'Make & complete': build each distinct ticked product at its net
        shortfall (and, with ?complete=1, issue + receive + close each run), then refresh the demand
        board within the active filter (carried on the query string)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        dtype, _q = _dp_filter_args(request)
        complete = request.query_params.get("complete") in ("1", "true", "on")
        form = await request.form()
        # Each selected value is "item_id|doc_id" - one demand line -> one linked work order.
        wo_lines = []
        for val in dict.fromkeys(form.getlist("selected")):
            item_id, _, doc_id = val.partition("|")
            if item_id:
                wo_lines.append({"item_id": item_id, "doc_id": doc_id})
        result: dict = {"created": []}
        rows: list[dict] = []
        error = ""
        try:
            if wo_lines:
                result = await api.manufacturing_make_work_orders(token, wo_lines, complete=complete)
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            error = str(e.detail) or "Could not create the selected work orders."
        lines = _demand_filter(_demand_lines(rows), dtype)
        made = len(result.get("created", []))
        verb = "Made and completed" if complete else "Created"
        if error:
            toast = {"message": error, "type": "error"}
        elif made:
            toast = {"message": f"{verb} {made} work order(s).", "type": "success"}
        else:
            toast = {"message": "Nothing to make for the selected lines.", "type": "info"}
        return HTMLResponse(
            to_xml(_demand_table(lines)),
            headers={"HX-Trigger": json.dumps({"celerpToast": toast})},
        )

    @app.get("/manufacturing/requirements")
    async def requirements_print(request: Request):
        """Printable component-requirements pick list for the selected products (opens in a new tab
        from the demand board's 'Print component requirements' action)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        # Selected values are "item_id|doc_id" demand lines; requirements aggregate by product.
        ids = list(dict.fromkeys(
            v.partition("|")[0] for v in (request.query_params.get("ids") or "").split(",") if v))
        data = {"products": [], "raw_materials": [], "sub_assemblies": []}
        if ids:
            try:
                data = await api.manufacturing_requirements(token, ids)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)

        def _need_table(title: str, items: list[dict]) -> FT:
            if not items:
                return ""
            return Div(
                H2(title, cls="section-title"),
                Table(
                    Thead(Tr(Th("SKU"), Th("Item"), Th("Quantity", cls="cell--center"))),
                    Tbody(*[Tr(Td(i.get("sku") or EMPTY), Td(i.get("name") or EMPTY),
                               Td(_qty(i.get("quantity", 0), i.get("unit")), cls="cell--number"))
                            for i in items]),
                    cls="data-table",
                ),
            )

        products = data.get("products", [])
        head = P("Make: " + ", ".join(
            f"{p.get('sku') or p.get('item_id')} ({_qty(p.get('quantity', 0), p.get('unit'))})"
            for p in products), cls="hint") if products else P("No products with a shortfall in the selection.", cls="hint")
        return await base_shell(
            page_header(
                "Component Requirements",
                Button("Print", type="button", cls="btn btn--primary", onclick="window.print()"),
            ),
            _intro("📋", "Raw materials and sub-assemblies needed to make the selected products' "
                         "outstanding shortfall, exploded through the recipes and pooled."),
            head,
            _need_table("Raw materials", data.get("raw_materials", [])),
            _need_table("Sub-assemblies to make", data.get("sub_assemblies", [])),
            title="Component Requirements - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/to-make-search")
    async def to_make_search(request: Request):
        """Demand-board fragment for the header search box (keeps the active document-type filter)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        dtype, q = _dp_filter_args(request)
        try:
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError:
            rows = []
        lines = _dp_search(_demand_filter(_demand_lines(rows), dtype), q)
        return _demand_table(lines)

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

    def _runs_for_status(orders: list[dict], status: str) -> list[dict]:
        if status in ("active", "incomplete", "all"):
            return [o for o in orders if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
        return [o for o in orders if str(o.get("status") or "").lower() == status]

    async def _incomplete_runs_table(token: str) -> FT:
        """The In Production table for the active (incomplete) runs - used after a schedule edit."""
        try:
            orders = (await api.list_mfg_orders(token, {})).get("items", [])
        except APIError:
            orders = []
        return _order_table(_runs_for_status(orders, "active"), today=date.today().isoformat())

    _BULK_RUN_VERB = {"start": "Started", "issue": "Issued components for", "complete": "Completed",
                      "hold": "Put on hold", "resume": "Resumed", "cancel": "Cancelled"}

    @app.post("/manufacturing/runs/bulk/{action}")
    async def bulk_run_action(request: Request, action: str):
        """Apply a lifecycle action to the ticked runs, then refresh the queue within its filter."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        status = (request.query_params.get("status") or "active").lower()
        form = await request.form()
        ids = list(dict.fromkeys(form.getlist("selected")))
        result: dict = {"done": [], "skipped": []}
        orders: list[dict] = []
        error = ""
        try:
            if ids:
                result = await api.manufacturing_bulk_run_action(token, ids, action)
            orders = (await api.list_mfg_orders(token, {})).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            error = str(e.detail) or "Could not apply the action."
        done = len(result.get("done", []))
        skipped = len(result.get("skipped", []))
        if error:
            toast = {"message": error, "type": "error"}
        else:
            msg = f"{_BULK_RUN_VERB.get(action, 'Updated')} {done} run(s)."
            if skipped:
                msg += f" {skipped} skipped (not in a valid state)."
            toast = {"message": msg, "type": "success" if done else "info"}
        return HTMLResponse(
            to_xml(_order_table(_runs_for_status(orders, status), today=date.today().isoformat())),
            headers={"HX-Trigger": json.dumps({"celerpToast": toast})},
        )

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
