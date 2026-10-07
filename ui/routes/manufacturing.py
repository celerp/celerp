# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
import uuid

from ui.components.operation_key import (
    kept_operation_key, operation_key_attrs, operation_key_vals, required_operation_key,
)
from ui.components.posting_accounts import account_picker, distinct_name
from ui.components.shell import base_shell, page_header, page_title, toast_header
from ui.components.table import (EMPTY, status_cards, empty_state_cta, format_value, search_bar,
                                 bulk_toolbar, filter_th, display_enum, breadcrumbs, COLUMN_FILTER_JS)
from ui.config import get_token as _token
from ui.i18n import reconcile_reason, refusal_text, t

logger = logging.getLogger(__name__)

# Canonical run statuses; "incomplete" = still needs attention (default queue view).
_INCOMPLETE_STATUSES = frozenset({"planned", "in_progress", "on_hold"})

# Plain-language help for the status indicators, shown as hover tooltips so users know what each
# means. Values are i18n KEYS resolved at render time (R1), never frozen English at import.
RUN_STATUS_HELP = {
    "planned": "manufacturing.help_status_planned",
    "in_progress": "manufacturing.help_status_in_progress",
    "on_hold": "manufacturing.help_status_on_hold",
    "completed": "manufacturing.help_status_completed",
    "cancelled": "manufacturing.help_status_cancelled",
}
COVERAGE_HELP = {
    "short": "manufacturing.help_coverage_short",
    "partial": "manufacturing.help_coverage_partial",
    "covered": "manufacturing.help_coverage_covered",
}


def _badge(status: str) -> FT:
    raw = (status or "").lower()
    label = display_enum(raw, domain="mfg_run_status")
    key = RUN_STATUS_HELP.get(raw)
    help_txt = t(key) if key else ""
    return Span(label or EMPTY, cls=f"badge badge--{raw.replace('_', '-')}",
                **({"title": help_txt} if help_txt else {}))


def _mfg_status_cards(orders: list[dict], active_status: str, base_url: str) -> FT:
    """Status filter cards for the Work In Progress queue. 'Active' = planned+in_progress+on_hold
    (the default view). There is no 'All' card - we never show completed/cancelled and active runs
    together. Cards link within base_url (carrying the status) so the queue stays put when filtering."""
    _CARD_DEFS = [
        ("active", "blue"),
        ("planned", "blue"),
        ("in_progress", "yellow"),
        ("on_hold", "orange"),
        ("completed", "green"),
        ("cancelled", "gray"),
    ]
    statuses = [str(o.get("status") or "").lower() for o in orders]
    counts: dict[str, int] = {
        "active": sum(1 for s in statuses if s in _INCOMPLETE_STATUSES),
    }
    for s, _ in _CARD_DEFS:
        counts.setdefault(s, sum(1 for x in statuses if x == s))
    card_help = {"active": "manufacturing.help_active", **RUN_STATUS_HELP}

    def _card_label(s: str) -> str:
        return t("manufacturing.card_active") if s == "active" else display_enum(s, domain="mfg_run_status")

    cards = [
        {"label": _card_label(s), "count": counts[s], "status": s, "color": color,
         "title": t(card_help[s]) if s in card_help else ""}
        for s, color in _CARD_DEFS
    ]
    return status_cards(cards, base_url, active_status or None, show_all_card=False)


# Run priority labels (Phase-A scheduling). Order = ascending urgency for the picker.
_PRIORITIES = ("low", "normal", "high", "urgent")


def _priority_badge(priority: str | None) -> FT:
    p = (priority or "").lower()
    if p not in _PRIORITIES:
        return Span(EMPTY)
    return Span(display_enum(p, domain="mfg_priority"), cls=f"badge badge--prio-{p}")


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
            title=t("manufacturing.set_due_date_hint")),
        cls="cell--center",
    )
    prio_cell = Td(
        Div(_priority_badge(order.get("priority")), cls="editable-cell",
            hx_get=f"/manufacturing/runs/{rid}/edit/priority?current={order.get('priority') or ''}",
            hx_target="this", hx_swap="innerHTML", hx_trigger="dblclick",
            title=t("manufacturing.set_priority_hint")),
        cls="cell--center",
    )
    src_id, src_no = order.get("source_doc_id"), order.get("source_doc_number")
    if src_id and src_no:
        src_href = f"/lists/{src_id}" if str(src_id).startswith("list:") else f"/docs/{src_id}"
        src_cell = A(src_no, href=src_href, cls="table-link")
    else:
        src_cell = Span(t("manufacturing.to_stock"), cls="hint")
    return Tr(
        Td(Input(type="checkbox", cls="bulk-select", name="selected", value=rid), cls="col-checkbox"),
        Td(name_cell),
        Td(_badge(status)),
        prio_cell,
        due_cell,
        Td(format_value((order.get("created_at") or "")[:10])),
        Td(str(len(inputs)), cls="cell--number"),
        Td(src_cell),
        Td(A(t("manufacturing.run_sheet"), href=f"/manufacturing/{rid}/run-sheet/print", target="_blank",
             cls="btn btn--xs btn--secondary"), cls="cell--actions"),
        cls="data-row",
    )


def _qty(value, unit: str | None) -> str:
    """Format a quantity with the product's sell unit, e.g. '2 Pieces'."""
    n = f"{float(value or 0):g}"
    return f"{n} {unit}" if unit else n


# Demand-line coverage from FIFO pegging (supply = on hand + in progress), relabelled for the board.
# Values are i18n KEYS resolved at render (R1); "short" is deliberately relabelled "Needed".
_STATUS_LABELS = {"short": "manufacturing.coverage_short", "partial": "manufacturing.coverage_partial",
                  "covered": "manufacturing.coverage_covered"}
_STATUS_ORDER = {"short": 0, "partial": 1, "covered": 2}  # needed-first sort
# (raw doc-type value, display key); the type filter's counts key off the raw value.
_DTYPE_DEFS = [("all", "doc.all"), ("invoice", "nav.invoices"),
               ("production_order", "manufacturing.dtype_production_orders"), ("list", "nav.lists")]


def _status_badge(coverage: str) -> FT:
    help_key = COVERAGE_HELP.get(coverage)
    label_key = _STATUS_LABELS.get(coverage)
    label = t(label_key) if label_key else (coverage or EMPTY)
    return Span(label, cls=f"badge badge--peg-{coverage}",
                **({"title": t(help_key)} if help_key else {}))




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
        Td(display_enum(doc_type, domain="doc_type") if doc_type else EMPTY),
        Td(l.get("contact_name") or EMPTY),
        Td(l.get("due") or EMPTY, cls="cell--center"),
        Td(_qty(l.get("quantity", 0), unit), cls="cell--number"),
        Td(_qty(l.get("shortfall", 0), unit), cls="cell--number"),
        Td(_status_badge(l.get("coverage", "")), cls="cell--center"),
        cls="data-row",
    )


def _demand_table(lines: list[dict], kept_key: str = "") -> FT:
    if not lines:
        return Div(
            P(t("manufacturing.demand_empty"), cls="hint"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(
            Th(Input(type="checkbox", cls="bulk-select-all", title=t("label.select_all")),
                cls="col-checkbox"),
            Th(t("manufacturing.th_product")), Th(t("th.document")), Th(t("th.type")),
            filter_th(t("manufacturing.th_for"), 4), Th(t("manufacturing.th_due"), cls="cell--center"),
            Th(t("th.ordered"), cls="cell--number"), Th(t("manufacturing.th_short"), cls="cell--number"),
            filter_th(t("th.status"), 8, center=True),
        )),
        Tbody(*[_demand_row(l) for l in lines]),
        cls="data-table", id="mfg-table", **operation_key_attrs(kept_key),
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
    cards = [{"label": t(lbl_key), "count": counts.get(k, 0), "status": k,
              "color": "gray" if k == "all" else "blue",
              "_url": f"/manufacturing?type={k}", "_active_key": k}
             for k, lbl_key in _DTYPE_DEFS]
    return status_cards(cards, "/manufacturing", dtype, show_all_card=False)




def _order_table(orders: list[dict], today: str = "", kept_key: str = "") -> FT:
    if not orders:
        return Div(
            empty_state_cta(t("manufacturing.nothing_in_production"),
                            t("manufacturing.go_to_demand_planning"), "/manufacturing"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(
            Th(Input(type="checkbox", cls="bulk-select-all", title=t("label.select_all")),
                cls="col-checkbox"),
            filter_th(t("manufacturing.th_product"), 1), Th(t("th.status")),
            filter_th(t("manufacturing.th_priority"), 3, center=True),
            Th(t("manufacturing.th_due"), cls="cell--center"), Th(t("msg.created")),
            Th(t("th.inputs"), cls="cell--number"),
            Th(t("manufacturing.th_source_order")), Th(t("manufacturing.run_sheet"), cls="cell--actions"),
        )),
        Tbody(*[_order_row(o, today) for o in _sched_sort(orders)]),
        cls="data-table",
        id="mfg-table",
        **operation_key_attrs(kept_key),
    )


# The opaque per-run detail page was removed in the product-centric overhaul. A run now lives on
# its product's Manufacturing tab (the production block) and in the In Production queue; status
# actions are handled there (see ui/routes/inventory.py production-block routes).


# ── Work Centers (operational stations, master data like Locations) ──────────

def _wc_cell(wc_id: str, field: str, value, *, align: str = "left") -> FT:
    disp = value if value not in (None, "") else EMPTY
    return Td(
        Div(disp, cls="editable-cell",
            hx_get=f"/manufacturing/work-centers/{wc_id}/edit/{field}?current={'' if value is None else value}",
            hx_target="this", hx_swap="innerHTML", hx_trigger="dblclick",
            title=t("label.dblclick_to_edit")),
        cls=f"cell--{align}",
    )


def _num(value) -> str | None:
    return f"{float(value):g}" if value not in (None, "") else None


def _wc_row(wc: dict, loc_names: dict) -> FT:
    wid = wc["id"]
    wip = loc_names.get(wc.get("wip_location_id")) if wc.get("wip_location_id") else None
    is_default = bool(wc.get("is_default"))
    return Tr(
        _wc_cell(wid, "name", wc.get("name")),
        _wc_cell(wid, "wip_location_id", wip, align="left"),
        _wc_cell(wid, "labor_rate", _num(wc.get("labor_rate")), align="right"),
        _wc_cell(wid, "capacity", _num(wc.get("capacity")), align="right"),
        _wc_cell(wid, "hours_per_day", _num(wc.get("hours_per_day")), align="right"),
        Td(Button(t("manufacturing.wc_is_default") if is_default else t("manufacturing.wc_set_default"),
                  type="button",
                  cls=f"btn btn--xs {'btn--primary' if is_default else 'btn--secondary'}",
                  hx_patch=f"/manufacturing/work-centers/{wid}/is_default",
                  hx_target="#wc-table", hx_swap="outerHTML", disabled=is_default),
           cls="cell--center"),
        Td(Button(t("btn.delete"), type="button", cls="btn btn--xs btn--secondary",
                  hx_post=f"/manufacturing/work-centers/{wid}/delete", hx_target="#wc-table",
                  hx_swap="outerHTML", hx_confirm=t("manufacturing.wc_delete_confirm")),
           cls="cell--actions"),
        cls="data-row",
    )


def _wc_table(centers: list[dict], loc_names: dict) -> FT:
    rows = [_wc_row(w, loc_names) for w in centers]
    return Table(
        Thead(Tr(Th(t("th.name")), Th(t("manufacturing.th_wip_location")),
                 Th(t("manufacturing.th_labor_rate"), cls="cell--right"),
                 Th(t("manufacturing.th_capacity"), cls="cell--right"),
                 Th(t("manufacturing.th_hours_per_day"), cls="cell--right"),
                 Th(t("th.default"), cls="cell--center"),
                 Th("", cls="cell--actions"))),
        Tbody(*rows) if rows else Tbody(Tr(Td(t("manufacturing.no_work_centers"),
                                              colspan="7", cls="empty-row"))),
        cls="data-table", id="wc-table",
    )


def _reconcile_accounts(posting: dict) -> list[dict]:
    """The accounts a reconciled value can come off: those that have held purchased or opening
    inventory, and retained earnings for value the books never carried."""
    accounts = list((posting.get("older_stock") or {}).get("candidates") or [])
    accounts += [{"code": r["code"], "name": r.get("name") or ""} for r in posting.get("roles") or []
                 if r.get("role") == "retained_earnings" and r.get("code")]
    return accounts


def _reconcile_panel(run_id: str, needs: dict, accounts: list[dict], *, key: str, values: dict | None = None,
                     account: str = "", flash: str | None = None, kind: str = "error") -> FT:
    """The on-page form recording what a run needing reconciliation holds: a value per component
    still in the run and, when the books are kept, the account that value comes off."""
    flash_el = Div(flash, cls=f"flash flash--{kind}", role="status") if flash else ""
    if kind == "success" and not needs:
        return Div(flash_el, id="reconcile-panel", cls="detail-card recipe-block")
    if not needs.get("reason"):
        return Div(flash_el, P(t("mfg.not_unresolved"), cls="hint"),
                   id="reconcile-panel", cls="detail-card recipe-block")
    values = values or {}
    reason = needs["reason"]
    rows = [
        Tr(Td(" ".join(x for x in (c.get("sku"), distinct_name(c.get("sku"), c.get("name"))) if x) or c["item_id"]),
           Td(f"{c['quantity']:g}", cls="cell--number"),
           Td(Input(type="hidden", name="item_id", value=c["item_id"]),
              Input(type="number", name="value", value=values.get(c["item_id"], ""), step="any", min="0",
                    cls="form-input form-input--xs", aria_label=t("th.value")),
              cls="cell--number"))
        for c in needs.get("components") or []
    ]
    table = Table(
        Thead(Tr(Th(t("th.item")), Th(t("th.qty"), cls="cell--number"), Th(t("th.value"), cls="cell--number"))),
        Tbody(*rows), cls="data-table",
    ) if rows else P(t("manufacturing.reconcile_nothing_held"), cls="hint")
    unlotted = float(needs.get("unlotted") or 0)
    discard = Form(
        P(t("manufacturing.reconcile_unlotted", qty=f"{unlotted:g}"), cls="hint"),
        Input(type="hidden", name="idempotency_key", value=key),
        Div(Button(t("manufacturing.reconcile_discard"), type="submit", cls="btn btn--secondary"),
            cls="form-actions"),
        hx_post=f"/manufacturing/runs/{run_id}/repair-output", hx_target="#reconcile-panel",
        hx_swap="outerHTML", hx_disabled_elt="find button",
    ) if unlotted > 0 else ""
    received = needs.get("received") or []
    output = P(t("manufacturing.reconcile_received", lots=", ".join(
        f"{r.get('sku') or r['lot_item_id']} ({r['quantity']:g})" for r in received)), cls="hint") if received else ""
    picker = Div(
        Label(t("manufacturing.reconcile_account")),
        account_picker("account", accounts, value=account, aria_label=t("manufacturing.reconcile_account")),
        P(t("manufacturing.reconcile_account_hint"), cls="hint"),
        *[P(t("manufacturing.reconcile_room", account=code, room=room), cls="hint")
          for code, room in (needs.get("rooms") or {}).items() if Decimal(room)],
        cls="form-field",
    ) if accounts else ""
    return Div(
        flash_el,
        P(t("manufacturing.reconcile_intro", reason=reconcile_reason(reason)), cls="hint"),
        discard,
        output,
        Form(
            table, picker,
            Input(type="hidden", name="idempotency_key", value=key),
            Div(Button(t("manufacturing.reconcile_submit"), type="submit", cls="btn btn--primary"),
                cls="form-actions mt-md"),
            hx_post=f"/manufacturing/runs/{run_id}/reconcile", hx_target="#reconcile-panel",
            hx_swap="outerHTML", hx_disabled_elt="find button",
        ),
        id="reconcile-panel", cls="detail-card recipe-block",
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
            _intro("📊", t("manufacturing.demand_intro")),
            _type_filter_bar(all_lines, dtype),
            bulk_toolbar("mfg-table", [
                {"value": "make", "label": t("manufacturing.action_make_selected"),
                 "method": "post", "url": f"/manufacturing/make-selected?type={dtype}"},
                {"value": "make_complete", "label": t("manufacturing.action_make_complete"),
                 "method": "post", "url": f"/manufacturing/make-selected?type={dtype}&complete=1",
                 "confirm": t("manufacturing.confirm_make_complete")},
                {"value": "requirements", "label": t("manufacturing.action_print_requirements"),
                 "method": "open", "url": "/manufacturing/requirements"},
            ]),
            # The queue swaps in place by #mfg-table; the wrap stays and scrolls it on a narrow screen.
            Div(_demand_table(lines), cls="table-scroll-wrap"),
            Script(COLUMN_FILTER_JS),
        )
        return await base_shell(
            page_header(
                t("manufacturing.demand_planning"),
                search_bar(placeholder=t("manufacturing.search_demand_placeholder"), target="#mfg-table",
                           url=f"/manufacturing/to-make-search?type={dtype}",
                           label=t("manufacturing.search_demand_label")),
            ),
            *body,
            title=page_title("manufacturing.demand_planning"),
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
            _intro("🏭", t("manufacturing.wip_intro")),
            _mfg_status_cards(orders_all, active, "/manufacturing/production"),
            bulk_toolbar("mfg-table", [
                {"value": "start", "label": t("btn.start"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/start?status={active}"},
                {"value": "issue", "label": t("manufacturing.action_issue"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/issue?status={active}"},
                {"value": "return", "label": t("manufacturing.action_return"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/return?status={active}",
                 "confirm": t("manufacturing.confirm_return_runs")},
                {"value": "complete", "label": t("manufacturing.action_complete"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/complete?status={active}",
                 "confirm": t("manufacturing.confirm_complete")},
                {"value": "hold", "label": t("manufacturing.action_hold"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/hold?status={active}"},
                {"value": "resume", "label": t("btn.resume"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/resume?status={active}"},
                {"value": "cancel", "label": t("btn.cancel"), "method": "post",
                 "url": f"/manufacturing/runs/bulk/cancel?status={active}",
                 "confirm": t("manufacturing.confirm_cancel_runs")},
            ]),
            # The queue swaps in place by #mfg-table; the wrap stays and scrolls it on a narrow screen.
            Div(_order_table(shown, today=date.today().isoformat()), cls="table-scroll-wrap"),
            Script(COLUMN_FILTER_JS),
        )
        return await base_shell(
            page_header(
                t("manufacturing.work_in_progress"),
                search_bar(placeholder=t("manufacturing.search_wip_placeholder"), target="#mfg-table",
                           url="/manufacturing/search",
                           label=t("manufacturing.search_wip_label")),
            ),
            *body,
            title=page_title("manufacturing.work_in_progress"),
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
        error = kept = ""
        try:
            if wo_lines:
                result = await api.manufacturing_make_work_orders(
                    token, wo_lines, complete=complete,
                    idempotency_key=required_operation_key(form, "make_complete" if complete else "make"))
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            # Whether it was made is not known (the answer may have been lost): the board keeps
            # the action's key, so sending it again is the same action.
            error, kept = refusal_text(e.data or e.detail) or t("manufacturing.err_make"), kept_operation_key(form, e)
        try:
            rows = (await api.manufacturing_to_make(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            error = error or refusal_text(e.data or e.detail) or t("manufacturing.err_make")
        lines = _demand_filter(_demand_lines(rows), dtype)
        made = len(result.get("created", []))
        if error:
            msg, kind = error, "error"
        elif made:
            key = "manufacturing.made_completed_n" if complete else "manufacturing.created_n"
            msg, kind = t(key, n=made), "success"
        else:
            msg, kind = t("manufacturing.nothing_to_make_selected"), "info"
        return HTMLResponse(
            to_xml(_demand_table(lines, kept)),
            headers=toast_header(msg, kind),
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
                    Thead(Tr(Th(t("th.sku")), Th(t("th.item")), Th(t("th.quantity"), cls="cell--center"))),
                    Tbody(*[Tr(Td(i.get("sku") or EMPTY), Td(i.get("name") or EMPTY),
                               Td(_qty(i.get("quantity", 0), i.get("unit")), cls="cell--number"))
                            for i in items]),
                    cls="data-table",
                ),
            )

        products = data.get("products", [])
        head = (P(t("manufacturing.make_list", items=", ".join(
            f"{p.get('sku') or p.get('item_id')} ({_qty(p.get('quantity', 0), p.get('unit'))})"
            for p in products)), cls="hint") if products
            else P(t("manufacturing.no_shortfall_selection"), cls="hint"))
        return await base_shell(
            page_header(
                t("manufacturing.component_requirements"),
                Button(t("btn.print"), type="button", cls="btn btn--primary", onclick="window.print()"),
            ),
            _intro("📋", t("manufacturing.requirements_intro")),
            head,
            _need_table(t("manufacturing.raw_materials"), data.get("raw_materials", [])),
            _need_table(t("manufacturing.sub_assemblies"), data.get("sub_assemblies", [])),
            title=page_title("manufacturing.component_requirements"),
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/{order_id}/run-sheet/print")
    async def run_sheet_print(request: Request, order_id: str):
        """Standalone printable run sheet: one production run's calculated (scaled) input quantities
        (auto window.print(); the user saves it as a PDF). Reuses the worksheet print shell."""
        from ui.routes.inventory import _run_sheet_print_view

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            order = await api.get_mfg_order(token, order_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return HTMLResponse(str(e.detail), status_code=e.status or 404)
        items = (await api.list_items(token, {"limit": 1000, "status": "all"})).get("items", [])
        today = date.today().isoformat()
        return HTMLResponse(to_xml(_run_sheet_print_view(order, items, today)))

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

    # Per-action success toast KEY (R1); each holds a count-neutral "...: {n}" template (R7).
    _BULK_RUN_MSG = {"start": "manufacturing.bulk_started", "issue": "manufacturing.bulk_issued",
                     "return": "manufacturing.bulk_returned",
                     "complete": "manufacturing.bulk_completed", "hold": "manufacturing.bulk_hold",
                     "resume": "manufacturing.bulk_resumed", "cancel": "manufacturing.bulk_cancelled"}

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
        error = kept = ""
        try:
            if ids:
                result = await api.manufacturing_bulk_run_action(token, ids, action,
                                                                 idempotency_key=required_operation_key(form, action))
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            # As Make selected: the queue keeps the key of an action whose outcome is not known.
            error, kept = refusal_text(e.data or e.detail) or t("manufacturing.err_bulk_action"), kept_operation_key(form, e)
        try:
            orders = (await api.list_mfg_orders(token, {})).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            error = error or refusal_text(e.data or e.detail) or t("manufacturing.err_bulk_action")
        done = len(result.get("done", []))
        skipped = len(result.get("skipped", []))
        if error:
            msg, kind = error, "error"
        else:
            msg = t(_BULK_RUN_MSG.get(action, "manufacturing.bulk_updated"), n=done)
            if skipped:
                why = dict.fromkeys(refusal_text({**s, "message": s.get("reason")}) for s in result["skipped"])
                msg = ". ".join([msg, t("manufacturing.bulk_skipped", n=skipped),
                                 *(w.rstrip(".") for w in why if w)]) + "."
            kind = "success" if done else "info"
        return HTMLResponse(
            to_xml(_order_table(_runs_for_status(orders, status), today=date.today().isoformat(), kept_key=kept)),
            headers=toast_header(msg, kind),
        )

    async def _reconcile_context(token: str, run_id: str) -> tuple[dict, list[dict]]:
        needs = await api.mfg_reconcile_needs(token, run_id)
        try:
            accounts = _reconcile_accounts(await api.get_posting_accounts(token))
        except APIError as e:
            if e.status == 401:
                raise
            accounts = []  # Accounting is off or not the user's to keep: reconciling says so
        return needs, accounts

    @app.get("/manufacturing/runs/{run_id}/reconcile")
    async def reconcile_page(request: Request, run_id: str):
        """A run whose materials' value its history cannot prove: record what it holds so it can
        carry on. The notification raised for such a run links here."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            run = await api.get_mfg_order(token, run_id)
            needs, accounts = await _reconcile_context(token, run_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return HTMLResponse(str(e.detail), status_code=e.status or 404)
        out = (run.get("expected_outputs") or [{}])[0]
        product = out.get("sku") or out.get("name") or run.get("output_item_id")
        crumbs = [(t("manufacturing.work_in_progress"), "/manufacturing/production")]
        if run.get("output_item_id"):
            crumbs.append((product, f"/inventory/{run['output_item_id']}?tab=manufacturing"))
        crumbs.append(("WO-" + run_id.split(":")[-1][:8], None))
        return await base_shell(
            breadcrumbs(crumbs),
            page_header(t("manufacturing.reconcile_title")),
            _reconcile_panel(run_id, needs, accounts, key=uuid.uuid4().hex),
            title=page_title("manufacturing.reconcile_title"),
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/runs/{run_id}/reconcile")
    async def reconcile_submit(request: Request, run_id: str):
        """Send the values and account entered; on a refusal keep them on the form and say why."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        entered = dict(zip(form.getlist("item_id"), (str(v).strip() for v in form.getlist("value"))))
        account = str(form.get("account") or "")
        key = str(form.get("idempotency_key") or "")
        components = []
        for item_id, raw in entered.items():
            try:
                components.append({"item_id": item_id, "value": float(raw)})
            except ValueError:
                pass  # left out, so the refusal names it as having no value
        try:
            await api.reconcile_mfg_order(token, run_id, {"components": components, "account": account or None},
                                          idempotency_key=required_operation_key(form))
            return _reconcile_panel(run_id, {}, [], key=key, flash=t("manufacturing.reconcile_done"),
                                    kind="success")
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            refusal, key = refusal_text(e.data or e.detail), kept_operation_key(form, e) or uuid.uuid4().hex
        try:
            needs, accounts = await _reconcile_context(token, run_id)
        except APIError:
            return Div(Div(refusal, cls="flash flash--error", role="status"), id="reconcile-panel", cls="detail-card recipe-block")
        return _reconcile_panel(run_id, needs, accounts, key=key, values=entered, account=account, flash=refusal)

    @app.post("/manufacturing/runs/{run_id}/repair-output")
    async def repair_output_submit(request: Request, run_id: str):
        """Discard what an older release recorded as received without making any stock; on a
        refusal keep the offer on the page and say why."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        key = str(form.get("idempotency_key") or "")
        try:
            await api.repair_mfg_output(token, run_id, idempotency_key=required_operation_key(form))
            refusal, kind = t("manufacturing.reconcile_discarded"), "success"
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            refusal, kind = refusal_text(e.data or e.detail), "error"
            key = kept_operation_key(form, e) or uuid.uuid4().hex
        try:
            needs, accounts = await _reconcile_context(token, run_id)
        except APIError:
            return Div(Div(refusal, cls=f"flash flash--{kind}", role="status"), id="reconcile-panel",
                       cls="detail-card recipe-block")
        return _reconcile_panel(run_id, needs, accounts, key=uuid.uuid4().hex if kind == "success" else key,
                                flash=refusal, kind=kind)

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
                  "hx_trigger": "change", "onkeydown": escape_js, "hx_vals": operation_key_vals()}
        if field == "priority":
            return Select(
                Option("--", value="", selected=(current == "")),
                *[Option(display_enum(p, domain="mfg_priority"), value=p, selected=(p == current))
                  for p in _PRIORITIES],
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
        refused = ""
        if fields:
            try:
                await api.schedule_mfg_order(token, run_id, fields, idempotency_key=required_operation_key(form))
            except APIError as e:
                if e.status == 401:
                    return P(t("error.unauthorized"), cls="cell-error")
                refused = refusal_text(e.data or e.detail)  # e.g. the run closed meanwhile: say so
        table = await _incomplete_runs_table(token)
        return HTMLResponse(to_xml(table), headers=toast_header(refused, "error")) if refused else table

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

    async def _wc_error_response(token: str, message: str) -> HTMLResponse:
        # A refused work-center action re-renders the table and says why through
        # the corner toast, rather than leaving the row silently reverted.
        return HTMLResponse(to_xml(await _wc_table_response(token)),
                            headers=toast_header(message, "error"))

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
        # ESC leaves the field without saving: the cell goes straight back to the
        # value it was showing. The esc flag suppresses the blur save that would
        # otherwise fire as the editor loses focus.
        esc_js = ("if(event.key==='Escape'){event.preventDefault();event.stopPropagation();"
                  "this.dataset.esc='1';"
                  "this.closest('.editable-cell').textContent=this.dataset.escRestore;}")
        editor = {
            "onkeydown": esc_js,
            "data_esc_restore": current if current not in ("", EMPTY) else EMPTY,
        }
        common = {
            "hx_post": post, "hx_target": "#wc-table", "hx_swap": "outerHTML",
            "hx_trigger": "blur[!this.dataset.esc], keyup[key=='Enter']", **editor,
        }
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
                **editor,
            )
        if field in ("labor_rate", "capacity", "hours_per_day"):
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
        if field in ("labor_rate", "capacity", "hours_per_day"):
            try:
                value = float(raw) if raw else None
            except ValueError:
                value = None
            # A working day of zero or less is not a day: store it unset so the
            # board falls back to its default rather than estimating on nonsense.
            if field == "hours_per_day" and value is not None and value <= 0:
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
            # A refused edit (a name that collides, a value the API rejects, or a
            # caller without manage rights) must say why rather than silently
            # reverting the cell with no explanation.
            return await _wc_error_response(
                token, str(e.detail) or t("manufacturing.err_wc_save"))
        return await _wc_table_response(token)

    @app.patch("/manufacturing/work-centers/{wc_id}/is_default")
    async def work_center_set_default(request: Request, wc_id: str):
        """Make this center the company's default; the previous one is cleared."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.set_default_work_center(token, wc_id)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return await _wc_error_response(
                token, str(e.detail) or t("manufacturing.err_wc_default"))
        return await _wc_table_response(token)

    @app.post("/manufacturing/work-centers/{wc_id}/delete")
    async def work_center_delete(request: Request, wc_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.delete_work_center(token, wc_id)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            # A refused delete (the last center, or the default one) must say why
            # rather than leaving the row sitting there with no explanation.
            return await _wc_error_response(
                token, str(e.detail) or t("manufacturing.err_wc_delete"))
        return await _wc_table_response(token)
