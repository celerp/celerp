# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import EMPTY, empty_state_cta, fmt_money
from ui.config import get_token as _token
from ui.i18n import t, get_lang


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_fiscal_and_currency(token: str) -> tuple[str, str | None]:
    """Fetch company fiscal_year_start and currency; defaults on any error."""
    try:
        company = await api.get_company(token)
        return (company.get("fiscal_year_start") or "01-01", company.get("currency") or None)
    except Exception:
        return ("01-01", None)


async def _get_fiscal(token: str) -> str:
    fy, _ = await _get_fiscal_and_currency(token)
    return fy


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _page_or_fragment(request: Request, *content, title: str, nav_active: str) -> FT:
    """Return full shell or HTMX fragment depending on request type."""
    if _is_htmx(request):
        return Div(*content, id="main-content", cls="main-content")
    return base_shell(*content, title=title, nav_active=nav_active, request=request)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def setup_routes(app):

    @app.get("/reports")
    async def reports_index(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header(t("page.reports", get_lang(request))),
            _report_index(lang=get_lang(request)),
            title="Reports - Celerp",
            nav_active="reports",
            request=request,
        )

    @app.get("/reports/ar-aging")
    async def ar_aging(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        fy, currency = await _get_fiscal_and_currency(token)
        date_from, date_to, preset = _parse_dates(request, fy)
        sort = request.query_params.get("sort", "outstanding")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            data = await api.get_ar_aging(token, params={"date_from": date_from, "date_to": date_to} if date_from else None)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = {"lines": [], "buckets": {}}
        content = [
            page_header("AR Aging", A(t("label.back"), href="/reports", cls="btn btn--secondary")),
            _date_filter_bar("/reports/ar-aging", date_from, date_to, preset, settings_link="/settings/sales?tab=terms", lang=get_lang(request)),
            _aging_view(data, "AR", sort=sort, sort_dir=sort_dir, currency=currency),
        ]
        return _page_or_fragment(request, *content, title="AR Aging - Celerp", nav_active="reports")

    @app.get("/reports/ap-aging")
    async def ap_aging(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        fy, currency = await _get_fiscal_and_currency(token)
        date_from, date_to, preset = _parse_dates(request, fy)
        sort = request.query_params.get("sort", "outstanding")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            data = await api.get_ap_aging(token, params={"date_from": date_from, "date_to": date_to} if date_from else None)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = {"lines": [], "buckets": {}}
        content = [
            page_header("AP Aging", A(t("label.back"), href="/reports", cls="btn btn--secondary")),
            _date_filter_bar("/reports/ap-aging", date_from, date_to, preset, settings_link="/settings/sales?tab=terms", lang=get_lang(request)),
            _aging_view(data, "AP", sort=sort, sort_dir=sort_dir, currency=currency),
        ]
        return _page_or_fragment(request, *content, title="AP Aging - Celerp", nav_active="reports")

    @app.get("/reports/sales")
    async def sales_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        group_by = request.query_params.get("group_by", "customer")
        sort = request.query_params.get("sort", "amount")
        sort_dir = request.query_params.get("dir", "desc")
        fy, currency = await _get_fiscal_and_currency(token)
        date_from, date_to, preset = _parse_dates(request, fy)
        params = {"group_by": group_by}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        try:
            data = await api.get_sales_report(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = {"lines": [], "group_by": group_by, "total": 0}

        content = [
            page_header(
                "Sales Report",
                _group_by_filter(group_by, "/reports/sales"),
                A(t("label.back"), href="/reports", cls="btn btn--secondary"),
            ),
            _date_filter_bar("/reports/sales", date_from, date_to, preset,
                             settings_link="/settings/sales?tab=terms",
                             extra_params=f"&group_by={group_by}",
                             lang=get_lang(request)),
            _sales_view(data, sort=sort, sort_dir=sort_dir, currency=currency),
        ]
        return _page_or_fragment(request, *content, title="Sales Report - Celerp", nav_active="reports")

    @app.get("/reports/purchases")
    async def purchases_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        group_by = request.query_params.get("group_by", "supplier")
        sort = request.query_params.get("sort", "amount")
        sort_dir = request.query_params.get("dir", "desc")
        fy, currency = await _get_fiscal_and_currency(token)
        date_from, date_to, preset = _parse_dates(request, fy)
        params = {"group_by": group_by}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        try:
            data = await api.get_purchases_report(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = {"lines": [], "group_by": group_by, "total": 0}

        content = [
            page_header(
                "Purchases Report",
                _group_by_filter(group_by, "/reports/purchases", first_option="supplier"),
                A(t("label.back"), href="/reports", cls="btn btn--secondary"),
            ),
            _date_filter_bar("/reports/purchases", date_from, date_to, preset,
                             settings_link="/settings/sales?tab=terms",
                             extra_params=f"&group_by={group_by}",
                             lang=get_lang(request)),
            _sales_view(data, sort=sort, sort_dir=sort_dir, currency=currency),
        ]
        return _page_or_fragment(request, *content, title="Purchases Report - Celerp", nav_active="reports")

    @app.get("/reports/expiring")
    async def expiring_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        days = int(request.query_params.get("days", 30))
        fy, currency = await _get_fiscal_and_currency(token)
        date_from, date_to, preset = _parse_dates(request, fy)
        try:
            data = await api.get_expiring(token, days)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = {"count": 0, "days_threshold": days, "lines": []}

        content = [
            page_header("Expiring Items", A(t("label.back"), href="/reports", cls="btn btn--secondary")),
            _expiring_view(data, days=days),
        ]
        return _page_or_fragment(request, *content, title="Expiring Items - Celerp", nav_active="reports")


# ---------------------------------------------------------------------------
# View components
# ---------------------------------------------------------------------------

def _date_presets(lang: str = "en") -> list[tuple[str, str]]:
    return [
        ("this_month", t("filter.this_month", lang)),
        ("last_3m", t("filter.last_3m", lang)),
        ("last_6m", t("filter.last_6m", lang)),
        ("last_12m", t("filter.last_12m", lang)),
        ("this_fy", t("filter.this_fy", lang)),
        ("last_fy", t("filter.last_fy", lang)),
        ("all", t("filter.all_time", lang)),
        ("custom", "Custom"),
    ]


def _fy_start(fiscal_year_start: str, reference: date) -> date:
    try:
        month, day = int(fiscal_year_start[:2]), int(fiscal_year_start[3:5])
    except (ValueError, IndexError):
        month, day = 1, 1
    candidate = reference.replace(month=month, day=day)
    if candidate > reference:
        candidate = candidate.replace(year=candidate.year - 1)
    return candidate


def _resolve_preset(preset: str, fiscal_year_start: str = "01-01") -> tuple[str, str]:
    today = date.today()
    if preset == "this_month":
        return (today.replace(day=1).isoformat(), today.isoformat())
    if preset == "last_3m":
        return ((today - timedelta(days=90)).isoformat(), today.isoformat())
    if preset == "last_6m":
        return ((today - timedelta(days=180)).isoformat(), today.isoformat())
    if preset == "last_12m":
        return ((today - timedelta(days=365)).isoformat(), today.isoformat())
    if preset == "this_fy":
        fy_start = _fy_start(fiscal_year_start, today)
        return (fy_start.isoformat(), today.isoformat())
    if preset == "last_fy":
        this_start = _fy_start(fiscal_year_start, today)
        last_start = _fy_start(fiscal_year_start, this_start - timedelta(days=1))
        last_end = this_start - timedelta(days=1)
        return (last_start.isoformat(), last_end.isoformat())
    return ("", "")


def _parse_dates(request: Request, fiscal_year_start: str = "01-01") -> tuple[str, str, str]:
    preset = request.query_params.get("preset", "")
    if preset and preset != "custom":
        date_from, date_to = _resolve_preset(preset, fiscal_year_start)
        return date_from, date_to, preset
    date_from = request.query_params.get("from", "")
    date_to = request.query_params.get("to", "")
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        return date_from, date_to, "custom"
    dflt_from, dflt_to = _resolve_preset("this_fy", fiscal_year_start)
    return dflt_from, dflt_to, "this_fy"


def _date_filter_bar(base_url: str, date_from: str, date_to: str, active_preset: str,
                     settings_link: str = "", extra_params: str = "", lang: str = "en") -> FT:
    preset_links = []
    for key, label in _date_presets(lang):
        if key == "custom":
            continue
        href = f"{base_url}?preset={key}{extra_params}"
        preset_links.append(
            A(label, href=href, cls=f"preset-btn {'preset-btn--active' if key == active_preset else ''}"),
        )

    custom_form = Form(
        Input(type="date", name="from", value=date_from, max=date_to or "", cls="date-input", id="dfb-from"),
        Span("--", cls="date-sep"),
        Input(type="date", name="to", value=date_to, min=date_from or "", cls="date-input", id="dfb-to"),
        Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
        Script(
            "(function(){"
            "var f=document.getElementById('dfb-from'),t=document.getElementById('dfb-to');"
            "if(!f||!t)return;"
            "f.addEventListener('change',function(){if(f.value)t.min=f.value;});"
            "t.addEventListener('change',function(){if(t.value)f.max=t.value;});"
            "})();"
        ),
        action=base_url,
        method="get",
        cls="date-custom-form",
    )

    parts: list[FT] = [
        Div(*preset_links, cls="preset-bar"),
        custom_form,
    ]
    if settings_link:
        parts.append(A("⚙", href=settings_link, cls="settings-gear", title="Related settings"))

    return Div(*parts, cls="date-filter-bar")


def _report_index(lang: str = "en") -> FT:
    reports = [
        ("/reports/ar-aging", t("page.ar_aging", lang), t("rpt.ar_aging_desc", lang)),
        ("/reports/ap-aging", t("page.ap_aging", lang), t("rpt.ap_aging_desc", lang)),
        ("/reports/sales?group_by=customer", t("page.sales_report", lang), t("rpt.sales_desc", lang)),
        ("/reports/purchases?group_by=supplier", t("page.purchases_report", lang), t("rpt.purchases_desc", lang)),
        ("/reports/expiring?days=30", t("page.expiring_items", lang), t("rpt.expiring_desc", lang)),
        ("/accounting/pnl", t("page.profit_loss", lang), t("rpt.pnl_desc", lang)),
        ("/accounting/balance-sheet", t("page.balance_sheet", lang), t("rpt.balance_sheet_desc", lang)),
    ]
    return Div(
        *[
            A(
                Strong(name),
                P(desc, cls="quick-link-desc"),
                href=href,
                cls="quick-link-card",
            )
            for href, name, desc in reports
        ],
        cls="quick-links-grid",
    )


def _aging_view(data: dict, label: str, sort: str = "outstanding", sort_dir: str = "desc", currency: str | None = None) -> FT:
    raw_lines = data.get("lines", [])
    buckets = data.get("buckets", {})

    def _is_meaningful(l: dict) -> bool:
        total = float(l.get("total", 0) or l.get("outstanding", 0) or 0)
        if total != 0:
            return True
        for k in ("customer_name", "contact_name", "customer_id", "contact_id", "supplier_name", "supplier_id"):
            if str(l.get(k, "") or "").strip() and str(l.get(k, "") or "").strip() != "unknown":
                return True
        return False

    lines = [l for l in raw_lines if _is_meaningful(l)]

    if not lines:
        return empty_state_cta("No data for this period. Try adjusting the date range.")

    def _get_outstanding(l: dict) -> float:
        return float(l.get("total", 0) or l.get("outstanding", 0) or 0)

    def _get_contact(l: dict) -> str:
        return str(l.get("customer_name", "") or l.get("supplier_name", "") or l.get("contact_name", "") or
                   l.get("customer_id", "") or l.get("supplier_id", "") or l.get("contact_id", "") or EMPTY)

    def _get_contact_id(l: dict) -> str:
        return str(l.get("customer_id", "") or l.get("supplier_id", "") or l.get("contact_id", "") or "")

    sort_keys = {
        "contact": lambda l: _get_contact(l).lower(),
        "outstanding": lambda l: _get_outstanding(l),
        "current": lambda l: float(l.get("current", 0) or 0),
        "d30": lambda l: float(l.get("d30", 0) or 0),
        "d60": lambda l: float(l.get("d60", 0) or 0),
        "d90": lambda l: float(l.get("d90", 0) or 0),
        "d90plus": lambda l: float(l.get("d90plus", 0) or 0),
    }
    lines = sorted(lines, key=sort_keys.get(sort, sort_keys["outstanding"]), reverse=(sort_dir == "desc"))

    def _th(label_txt: str, key: str) -> FT:
        nd = "asc" if (sort == key and sort_dir == "desc") else "desc"
        marker = " ▲" if (sort == key and sort_dir == "asc") else (" ▼" if sort == key else "")
        return Th(A(f"{label_txt}{marker}", href=f"?sort={key}&dir={nd}", cls="sort-link"))

    def _row(l: dict) -> FT:
        cid = _get_contact_id(l)
        name = _get_contact(l)
        contact_cell = Td(A(name, href=f"/crm/contacts/{cid}", cls="link") if cid and cid not in ("unlinked", "Unlinked", "unknown") else name)
        return Tr(
            contact_cell,
            Td(fmt_money(l.get('current', 0), currency), cls="cell--number"),
            Td(fmt_money(l.get('d30', 0), currency), cls="cell--number"),
            Td(fmt_money(l.get('d60', 0), currency), cls="cell--number"),
            Td(fmt_money(l.get('d90', 0), currency), cls="cell--number"),
            Td(fmt_money(l.get('d90plus', 0), currency), cls="cell--number"),
            Td(fmt_money(_get_outstanding(l), currency), cls="cell--number"),
        )

    bucket_totals = Div(
        *[
            Span(f"{k}: {fmt_money(float(v), currency)}", cls=f"val-chip {'val-chip--alert' if k not in ('current',) else ''}")
            for k, v in (buckets.items() if isinstance(buckets, dict) else [])
        ],
        cls="valuation-bar",
    )

    return Div(
        bucket_totals,
        Table(
            Thead(Tr(
                _th("Contact", "contact"),
                _th("Current", "current"),
                _th("1-30", "d30"),
                _th("31-60", "d60"),
                _th("61-90", "d90"),
                _th("90+", "d90plus"),
                _th("Total", "outstanding"),
            )),
            Tbody(*[_row(l) for l in lines]),
            cls="data-table",
        ),
    )


def _normalize_line(line: dict, group_by: str) -> dict:
    """Normalize backend line to display dict, passing through all extra fields."""
    base: dict
    if group_by == "customer":
        base = {
            "label": line.get("customer_name") or line.get("label", ""),
            "count": line.get("invoice_count") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total", 0),
            "_id": line.get("customer_id", ""),
            "_link": "/docs?type=invoice",
        }
    elif group_by == "supplier":
        base = {
            "label": line.get("supplier_name") or line.get("label", ""),
            "count": line.get("po_count") or line.get("count", 0),
            "total": line.get("total_spend") or line.get("total", 0),
            "_id": line.get("supplier_id", ""),
            "_link": "/docs?type=purchase_order",
        }
    elif group_by == "item":
        base = {
            "label": line.get("item_name") or line.get("label", ""),
            "count": line.get("qty_sold") or line.get("qty_purchased") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total_spend") or line.get("total", 0),
            "_id": line.get("item_id", ""),
            "_link": f"/inventory/{line.get('item_id', '')}",
        }
    elif group_by == "period":
        base = {
            "label": line.get("period") or line.get("label", ""),
            "count": line.get("invoice_count") or line.get("po_count") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total_spend") or line.get("total", 0),
            "_link": "",
        }
    elif group_by == "price_range":
        pr = line.get("price_range") or line.get("label", "")
        base = {
            "label": pr,
            "item_count": line.get("item_count", 0),
            "qty_sold": line.get("qty_sold", 0),
            "total": line.get("total_revenue") or line.get("total", 0),
            "_link": f"/docs?price_range={pr}",
        }
    else:
        base = {
            "label": line.get("label", ""),
            "count": line.get("count", 0),
            "total": line.get("total", 0),
            "_link": "",
        }
    # Pass through all remaining fields from the original line
    for k, v in line.items():
        if k not in base:
            base[k] = v
    return base


def _kpi(label: str, value: str, cls_suffix: str = "") -> FT:
    return Div(
        Span(label, cls="report-kpi__label"),
        Span(value, cls=f"report-kpi__value{cls_suffix}"),
        cls="report-kpi",
    )


def _summary_bar(data: dict, currency: str | None, group_by: str) -> FT:
    """Render the KPI summary bar for sales/purchases reports."""
    total_rev = float(data.get("total_revenue") or data.get("total_spend") or data.get("total", 0))
    total_cost = float(data.get("total_cost", 0) or 0)
    gross_profit = float(data.get("gross_profit", 0) or 0)
    is_purchases = bool(data.get("total_spend") and not data.get("total_revenue"))

    kpis = []
    if is_purchases:
        kpis.append(_kpi("Total Spend", fmt_money(total_rev, currency)))
    else:
        kpis.append(_kpi("Total Revenue", fmt_money(total_rev, currency)))
    if total_cost:
        kpis.append(_kpi("Total Cost", fmt_money(total_cost, currency)))
    if gross_profit:
        gp_cls = " report-kpi__value--positive" if gross_profit >= 0 else " report-kpi__value--negative"
        kpis.append(_kpi("Gross Profit", fmt_money(gross_profit, currency), gp_cls))
        if total_rev:
            margin = gross_profit / total_rev * 100
            margin_cls = " report-kpi__value--positive" if margin >= 0 else " report-kpi__value--negative"
            kpis.append(_kpi("Margin %", f"{margin:.1f}%", margin_cls))

    return Div(*kpis, cls="report-summary-bar")


def _sales_view_columns(group_by: str, is_purchases: bool = False) -> list[tuple[str, str, str]]:
    """Return list of (header, key, css_class) for the report table columns."""
    if group_by == "customer":
        return [
            ("Customer", "label", ""),
            ("# Invoices", "count", "cell--right"),
            ("Revenue", "total", "cell--number"),
            ("Cost", "total_cost", "cell--number"),
            ("Gross Profit", "gross_profit", "cell--number"),
            ("Margin %", "margin_pct", "cell--right"),
        ]
    if group_by == "supplier":
        return [
            ("Supplier", "label", ""),
            ("# Orders", "count", "cell--right"),
            ("Spend", "total", "cell--number"),
        ]
    if group_by == "item":
        if is_purchases:
            return [
                ("Item", "label", ""),
                ("Qty Purchased", "qty_purchased", "cell--right"),
                ("Avg Unit Cost", "avg_unit_cost", "cell--number"),
                ("Total Spend", "total", "cell--number"),
            ]
        return [
            ("Item", "label", ""),
            ("Qty Sold", "qty_sold", "cell--right"),
            ("Avg Price", "avg_price", "cell--number"),
            ("Revenue", "total", "cell--number"),
            ("Cost", "total_cost", "cell--number"),
            ("Gross Profit", "gross_profit", "cell--number"),
            ("Margin %", "margin_pct", "cell--right"),
        ]
    if group_by == "period":
        label = "Period"
        count_hdr = "# Orders" if is_purchases else "# Invoices"
        amount_hdr = "Spend" if is_purchases else "Revenue"
        cols = [(label, "label", ""), (count_hdr, "count", "cell--right"), (amount_hdr, "total", "cell--number")]
        if not is_purchases:
            cols += [("Cost", "total_cost", "cell--number"), ("Gross Profit", "gross_profit", "cell--number")]
        return cols
    if group_by == "price_range":
        return [
            ("Price Range", "label", ""),
            ("# Items", "item_count", "cell--right"),
            ("Qty Sold", "qty_sold", "cell--right"),
            ("Revenue", "total", "cell--number"),
            ("Cost", "total_cost", "cell--number"),
            ("Gross Profit", "gross_profit", "cell--number"),
            ("Margin %", "margin_pct", "cell--right"),
        ]
    # fallback
    return [("Group", "label", ""), ("Count", "count", "cell--right"), ("Amount", "total", "cell--number")]


def _sales_view(data: dict, sort: str = "amount", sort_dir: str = "desc", currency: str | None = None) -> FT:
    raw_lines = data.get("lines", [])
    group_by = data.get("group_by", "")
    is_purchases = bool(data.get("total_spend") and not data.get("total_revenue"))

    def _is_meaningful(l: dict) -> bool:
        if float(l.get("total", 0) or 0) != 0:
            return True
        if int(l.get("count", 0) or 0) != 0:
            return True
        return bool(str(l.get("label", "") or "").strip())

    lines = [_normalize_line(l, group_by) for l in raw_lines]
    lines = [l for l in lines if _is_meaningful(l)]

    if not lines:
        return empty_state_cta("No data for this period. Try adjusting the date range.")

    cols = _sales_view_columns(group_by, is_purchases)

    sort_key_map = {
        "group": lambda l: str(l.get("label", "") or ""),
        "count": lambda l: float(l.get("count", 0) or 0),
        "amount": lambda l: float(l.get("total", 0) or 0),
    }
    lines = sorted(lines, key=sort_key_map.get(sort, sort_key_map["amount"]), reverse=(sort_dir == "desc"))

    def _th(label_txt: str, key: str) -> FT:
        nd = "asc" if (sort == key and sort_dir == "desc") else "desc"
        marker = " ▲" if (sort == key and sort_dir == "asc") else (" ▼" if sort == key else "")
        return Th(A(f"{label_txt}{marker}", href=f"?sort={key}&dir={nd}", cls="sort-link"))

    def _cell(l: dict, key: str, css: str) -> FT:
        val = l.get(key)
        if val is None:
            return Td(EMPTY, cls=css)
        if key in ("total", "total_cost", "gross_profit", "avg_price", "avg_unit_cost"):
            return Td(fmt_money(val, currency), cls=css)
        if key == "margin_pct":
            v = float(val or 0)
            color_cls = " report-kpi__value--positive" if v >= 0 else " report-kpi__value--negative"
            return Td(Span(f"{v:.1f}%", cls=f"report-kpi__value{color_cls}"), cls=css)
        link = l.get("_link", "")
        if key == "label" and link:
            return Td(A(str(val) or EMPTY, href=link, cls="link"), cls=css)
        return Td(str(val) if val != "" else EMPTY, cls=css)

    def _row(l: dict) -> FT:
        return Tr(*[_cell(l, key, css) for _, key, css in cols])

    th_row = Tr(*[_th(hdr, "group" if key == "label" else ("count" if key == "count" else "amount" if key == "total" else key)) for hdr, key, _ in cols])

    return Div(
        _summary_bar(data, currency, group_by),
        Table(
            Thead(th_row),
            Tbody(*[_row(l) for l in lines]),
            cls="data-table",
        ),
    )


def _expiring_view(data: dict, days: int = 30) -> FT:
    count = data.get("count", 0)
    items = data.get("lines", [])
    threshold = data.get("days_threshold", days)

    days_select = Select(
        *[Option(f"{d} days", value=str(d), selected=(d == threshold)) for d in [7, 14, 30, 60, 90]],
        name="days",
        hx_get="/reports/expiring",
        hx_trigger="change",
        hx_target="#main-content",
        hx_swap="outerHTML",
        hx_include="this",
        cls="filter-select",
    )

    if not items:
        return Div(
            Div(days_select, cls="filter-bar"),
            empty_state_cta("No items expiring in this timeframe."),
        )

    def _row(i: dict) -> FT:
        dr = i.get("days_remaining")
        return Tr(
            Td(i.get("sku", "") or EMPTY),
            Td(i.get("name", "") or EMPTY),
            Td(i.get("category", "") or EMPTY),
            Td((i.get("expires_at") or "")[:10] or EMPTY),
            Td(str(dr) if dr is not None else EMPTY),
            Td(Span(i.get("status", "") or EMPTY, cls=f"badge badge--{i.get('status', '')}" if i.get("status") else "")),
        )

    return Div(
        Div(
            Span(f"{count} items expiring within {threshold} days", cls="val-chip val-chip--alert"),
            days_select,
            cls="valuation-bar",
            style="gap:12px",
        ),
        Table(
            Thead(Tr(Th("SKU"), Th(t("th.name")), Th("Category"), Th(t("th.expiry")), Th(t("th.days_left")), Th(t("th.status")))),
            Tbody(*[_row(i) for i in items]),
            cls="data-table",
        ),
    )


def _group_by_filter(active: str, base_url: str, first_option: str = "customer") -> FT:
    options_map = {
        "customer": ["customer", "item", "period", "price_range"],
        "supplier": ["supplier", "item", "period", "price_range"],
    }
    options = options_map.get(first_option, ["customer", "item", "period", "price_range"])
    labels = {"customer": "Customer", "supplier": "Supplier", "item": "Item", "period": "Period", "price_range": "Price Range"}
    return Select(
        *[Option(labels.get(o, o.title()), value=o, selected=(o == active)) for o in options],
        name="group_by",
        hx_get=base_url,
        hx_trigger="change",
        hx_target="#main-content",
        hx_swap="outerHTML",
        hx_include="this",
        cls="filter-select",
    )

# Alias for module loader compatibility
setup_ui_routes = setup_routes
