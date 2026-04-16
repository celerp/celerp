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




async def _get_fiscal_and_currency(token: str) -> tuple[str, str | None]:
    """Fetch company fiscal_year_start and currency; defaults on any error."""
    try:
        company = await api.get_company(token)
        return (company.get("fiscal_year_start") or "01-01", company.get("currency") or None)
    except Exception:
        return ("01-01", None)


async def _get_fiscal(token: str) -> str:
    """Fetch company fiscal_year_start; default to '01-01' on any error."""
    fy, _ = await _get_fiscal_and_currency(token)
    return fy




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
        return base_shell(
            page_header("AR Aging", A(t("label.back"), href="/reports", cls="btn btn--secondary")),
            _date_filter_bar("/reports/ar-aging", date_from, date_to, preset, settings_link="/settings/sales?tab=terms", lang=get_lang(request)),
            _aging_view(data, "AR", sort=sort, sort_dir=sort_dir, currency=currency),
            title="AR Aging - Celerp",
            nav_active="reports",
            request=request,
        )

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
        return base_shell(
            page_header("AP Aging", A(t("label.back"), href="/reports", cls="btn btn--secondary")),
            _date_filter_bar("/reports/ap-aging", date_from, date_to, preset, settings_link="/settings/sales?tab=terms", lang=get_lang(request)),
            _aging_view(data, "AP", sort=sort, sort_dir=sort_dir, currency=currency),
            title="AP Aging - Celerp",
            nav_active="reports",
            request=request,
        )

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

        return base_shell(
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
            title="Sales Report - Celerp",
            nav_active="reports",
            request=request,
        )

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

        return base_shell(
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
            title="Purchases Report - Celerp",
            nav_active="reports",
            request=request,
        )

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
            data = {"count": 0, "days_threshold": days, "items": []}

        return base_shell(
            page_header("Expiring Items", A(t("label.back"), href="/reports", cls="btn btn--secondary")),
            _date_filter_bar("/reports/expiring", date_from, date_to, preset,
                             extra_params=f"&days={days}",
                             lang=get_lang(request)),
            _expiring_view(data),
            title="Expiring Items - Celerp",
            nav_active="reports",
            request=request,
        )


# ---------------------------------------------------------------------------
# View components
# ---------------------------------------------------------------------------

def _date_presets(lang: str = "en") -> list[tuple[str, str]]:
    """Return date preset (key, label) pairs translated to the given language."""
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
    """Return the start date of the fiscal year that contains `reference`.

    fiscal_year_start: "MM-DD" string (e.g. "04-01" for April 1).
    """
    try:
        month, day = int(fiscal_year_start[:2]), int(fiscal_year_start[3:5])
    except (ValueError, IndexError):
        month, day = 1, 1
    candidate = reference.replace(month=month, day=day)
    # If candidate is in the future relative to reference, step back one year
    if candidate > reference:
        candidate = candidate.replace(year=candidate.year - 1)
    return candidate


def _resolve_preset(preset: str, fiscal_year_start: str = "01-01") -> tuple[str, str]:
    """Return (date_from, date_to) for a preset name. Empty strings mean no filter."""
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
    # "all" or unknown
    return ("", "")


def _parse_dates(request: Request, fiscal_year_start: str = "01-01") -> tuple[str, str, str]:
    """Extract date_from, date_to, preset from request. Returns (from, to, preset)."""
    preset = request.query_params.get("preset", "")
    if preset and preset != "custom":
        date_from, date_to = _resolve_preset(preset, fiscal_year_start)
        return date_from, date_to, preset
    date_from = request.query_params.get("from", "")
    date_to = request.query_params.get("to", "")
    if date_from or date_to:
        return date_from, date_to, "custom"
    # Default: this fiscal year
    dflt_from, dflt_to = _resolve_preset("this_fy", fiscal_year_start)
    return dflt_from, dflt_to, "this_fy"


def _date_filter_bar(base_url: str, date_from: str, date_to: str, active_preset: str,
                     settings_link: str = "", extra_params: str = "", lang: str = "en") -> FT:
    """Reusable date range filter bar with presets and custom inputs."""
    preset_links = []
    for key, label in _date_presets(lang):
        if key == "custom":
            continue
        href = f"{base_url}?preset={key}{extra_params}"
        preset_links.append(
            A(label, href=href, cls=f"preset-btn {'preset-btn--active' if key == active_preset else ''}"),
        )

    custom_form = Form(
        Input(type="date", name="from", value=date_from, cls="date-input"),
        Span("–", cls="date-sep"),
        Input(type="date", name="to", value=date_to, cls="date-input"),
        Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
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
        for k in ("customer_name", "contact_name", "customer_id", "contact_id"):
            if str(l.get(k, "") or "").strip() and str(l.get(k, "") or "").strip() != "unknown":
                return True
        return False

    lines = [l for l in raw_lines if _is_meaningful(l)]

    if not lines:
        return empty_state_cta("No data for this period. Try adjusting the date range.")

    def _get_outstanding(l: dict) -> float:
        return float(l.get("total", 0) or l.get("outstanding", 0) or 0)

    def _get_contact(l: dict) -> str:
        return str(l.get("customer_name", "") or l.get("contact_name", "") or l.get("customer_id", "") or l.get("contact_id", "") or EMPTY)

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
        return Tr(
            Td(_get_contact(l)),
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
            Thead(Tr(_th("Contact", "contact"), _th("Current", "current"), _th("1-30", "d30"), _th("31-60", "d60"), _th("61-90", "d90"), _th("90+", "d90plus"), _th("Total", "outstanding"))),
            Tbody(*[_row(l) for l in lines]),
            cls="data-table",
        ),
    )


def _age_badge(bucket: str) -> FT:
    cls_map = {
        "current": "badge--active",
        "1-30": "badge--warning",
        "31-60": "badge--warning",
        "61-90": "badge--alert",
        "90+": "badge--danger",
    }
    return Span(bucket, cls=f"badge {cls_map.get(bucket, 'badge--neutral')}")


def _normalize_line(line: dict, group_by: str) -> dict:
    """Normalize backend line data to {label, count, total} for display."""
    if group_by == "customer":
        return {
            "label": line.get("customer_name") or line.get("label", ""),
            "count": line.get("invoice_count") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total", 0),
            "_id": line.get("customer_id", ""),
            "_link": f"/docs?type=invoice",
        }
    if group_by == "supplier":
        return {
            "label": line.get("supplier_name") or line.get("label", ""),
            "count": line.get("po_count") or line.get("count", 0),
            "total": line.get("total_spend") or line.get("total", 0),
            "_id": line.get("supplier_id", ""),
            "_link": f"/docs?type=purchase_order",
        }
    if group_by == "item":
        return {
            "label": line.get("item_name") or line.get("label", ""),
            "count": line.get("qty_sold") or line.get("qty_purchased") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total_spend") or line.get("total", 0),
            "_id": line.get("item_id", ""),
            "_link": f"/inventory/{line.get('item_id', '')}",
        }
    if group_by == "period":
        return {
            "label": line.get("period") or line.get("label", ""),
            "count": line.get("invoice_count") or line.get("po_count") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total_spend") or line.get("total", 0),
            "_link": "",
        }
    if group_by == "price_range":
        pr = line.get("price_range") or line.get("label", "")
        return {
            "label": pr,
            "count": line.get("invoice_count") or line.get("po_count") or line.get("count", 0),
            "total": line.get("total_revenue") or line.get("total_spend") or line.get("total", 0),
            "_link": f"/docs?price_range={pr}",
        }
    # fallback (already normalized)
    return {
        "label": line.get("label", ""),
        "count": line.get("count", 0),
        "total": line.get("total", 0),
        "_link": "",
    }


def _sales_view(data: dict, sort: str = "amount", sort_dir: str = "desc", currency: str | None = None) -> FT:
    raw_lines = data.get("lines", [])
    total_rev = float(data.get("total_revenue") or data.get("total_spend") or data.get("total", 0))
    group_by = data.get("group_by", "")

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

    sort_keys = {
        "group": lambda l: str(l.get("label", "") or ""),
        "count": lambda l: float(l.get("count", 0) or 0),
        "amount": lambda l: float(l.get("total", 0) or 0),
    }
    lines = sorted(lines, key=sort_keys.get(sort, sort_keys["amount"]), reverse=(sort_dir == "desc"))

    def _th(label_txt: str, key: str) -> FT:
        nd = "asc" if (sort == key and sort_dir == "desc") else "desc"
        marker = " ▲" if (sort == key and sort_dir == "asc") else (" ▼" if sort == key else "")
        return Th(A(f"{label_txt}{marker}", href=f"?sort={key}&dir={nd}", cls="sort-link"))

    def _row(l: dict) -> FT:
        link = l.get("_link", "")
        label_cell = Td(A(l.get("label", "") or EMPTY, href=link, cls="link") if link else (l.get("label", "") or EMPTY))
        return Tr(
            label_cell,
            Td(str(l.get("count", "")) or EMPTY),
            Td(fmt_money(l.get('total', 0), currency), cls="cell--number"),
        )

    header_label = {
        "customer": "Customer", "supplier": "Supplier", "item": "Item",
        "period": "Period", "price_range": "Price Range",
    }.get(group_by, "Group")

    return Div(
        Div(Span(f"Total: {fmt_money(total_rev, currency)}", cls="val-chip"), cls="valuation-bar"),
        Table(
            Thead(Tr(_th(header_label, "group"), _th("Count", "count"), _th("Amount", "amount"))),
            Tbody(*[_row(l) for l in lines]),
            cls="data-table",
        ),
    )


def _expiring_view(data: dict) -> FT:
    count = data.get("count", 0)
    items = data.get("items", [])
    days = data.get("days_threshold", 30)

    if not items:
        return empty_state_cta("No data for this period. Try adjusting the date range.")

    def _row(i: dict) -> FT:
        return Tr(
            Td(i.get("sku", "") or EMPTY),
            Td(i.get("name", "") or EMPTY),
            Td((i.get("expiry_date") or "")[:10] or EMPTY),
            Td(str(i.get("days_left", "")) or EMPTY),
            Td(Span(i.get("status", "") or EMPTY, cls=f"badge badge--{i.get('status', '')}" if i.get("status") else "")),
        )

    return Div(
        Div(Span(f"{count} items expiring within {days} days", cls="val-chip val-chip--alert"), cls="valuation-bar"),
        Table(
            Thead(Tr(Th("SKU"), Th(t("th.name")), Th(t("th.expiry")), Th(t("th.days_left")), Th(t("th.status")))),
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
        hx_swap="innerHTML",
        hx_include="this",
        cls="filter-select",
    )
