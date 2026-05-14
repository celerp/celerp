# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import csv
import io
import asyncio
from datetime import date as _date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, StreamingResponse, PlainTextResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import get_token as _token
from ui.i18n import t, get_lang
from ui.routes.reports import _date_filter_bar, _get_fiscal, _parse_dates

# SVG icons (matching documents.py style)
_ICON_CSV_EXPORT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="12" y1="18" x2="12" y2="12"/>'
    '<polyline points="9 15 12 18 15 15"/></svg>'
)
_ICON_PRINT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="6 9 6 2 18 2 18 9"/>'
    '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
    '<rect x="6" y="14" width="12" height="8"/></svg>'
)


def _action_bar(tab: str, params: dict) -> FT:
    """Print + CSV icon buttons shown in the top-right corner of each accounting tab."""
    qs = "".join(f"&{k}={v}" for k, v in params.items() if v)
    print_href = f"/accounting/print/{tab}?{qs.lstrip('&')}"
    csv_href = f"/accounting/export/{tab}/csv?{qs.lstrip('&')}"
    return Div(
        A(
            NotStr(_ICON_PRINT),
            href=print_href,
            target="_blank",
            cls="btn btn--ghost btn--icon",
            title=t("btn.print"),
        ),
        A(
            NotStr(_ICON_CSV_EXPORT),
            href=csv_href,
            cls="btn btn--ghost btn--icon",
            title=t("btn.export_csv"),
        ),
        cls="page-actions flex-row gap-sm ml-auto",
    )


def _report_header(company: dict, title: str, subtitle: str = "") -> FT:
    """Professional report header for print views."""
    return Div(
        Div(
            H1(company.get("name", ""), cls="report-company-name"),
            P(company.get("address", ""), cls="report-company-address") if company.get("address") else None,
            P(f"Tax ID: {company['tax_id']}", cls="report-company-taxid") if company.get("tax_id") else None,
        ),
        Div(
            H2(title, cls="report-title"),
            P(subtitle, cls="report-subtitle") if subtitle else None,
            P(f"Printed: {_date.today().isoformat()}", cls="report-print-date"),
        ),
        cls="report-header",
    )


def _print_shell(company: dict, title: str, subtitle: str, body: FT) -> FT:
    """Minimal printable page: auto-triggers window.print() on load."""
    return Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(f"{title} - {company.get('name', 'Celerp')}"),
            Style(_PRINT_CSS),
        ),
        Body(
            _report_header(company, title, subtitle),
            body,
            Script("window.onload = function() { window.print(); }"),
        ),
    )


_PRINT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Arial', sans-serif; font-size: 10pt; color: #111; background: white; padding: 20mm; }

/* Report header */
.report-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8mm; padding-bottom: 4mm; border-bottom: 2px solid #111; }
.report-company-name { font-size: 14pt; font-weight: 700; margin-bottom: 2mm; }
.report-company-address, .report-company-taxid { font-size: 9pt; color: #555; line-height: 1.4; }
.report-title { font-size: 14pt; font-weight: 700; text-align: right; margin-bottom: 2mm; }
.report-subtitle { font-size: 9pt; color: #555; text-align: right; line-height: 1.4; }
.report-print-date { font-size: 8pt; color: #888; text-align: right; margin-top: 1mm; }

/* Sections */
.report-section { margin-bottom: 6mm; }
.report-section-title { font-size: 10pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: #333; border-bottom: 1px solid #ccc; padding-bottom: 1mm; margin-bottom: 2mm; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
thead th { background: #f5f5f5; font-weight: 700; text-align: left; padding: 1.5mm 2mm; border-bottom: 1px solid #ccc; }
thead th.cell--number { text-align: right; }
tbody td { padding: 1.2mm 2mm; border-bottom: 1px solid #eee; }
td.cell--number, td.cell--right { text-align: right; }
td.cell--muted { color: #888; font-size: 8pt; }
td.cell--mono { font-family: 'Courier New', monospace; font-size: 8.5pt; }

/* Totals */
.section-total { font-weight: 700; text-align: right; padding: 1.5mm 2mm; border-top: 1px solid #999; font-size: 9.5pt; margin-top: 1mm; }
.report-subtotal, .report-total { text-align: right; padding: 2mm 2mm; border-top: 2px solid #333; font-size: 10pt; margin-bottom: 4mm; }
.net-profit--positive { color: #1a7a3c; }
.net-profit--negative { color: #b91c1c; }

/* Balance status */
.valuation-bar { margin-top: 4mm; display: flex; gap: 4mm; font-size: 9pt; }
.val-chip { background: #f5f5f5; padding: 1mm 3mm; border: 1px solid #ccc; border-radius: 2px; }
.val-chip--alert { background: #fef2f2; border-color: #f87171; color: #b91c1c; }

/* Trial balance summary */
.trial-summary { display: flex; gap: 6mm; margin-bottom: 4mm; font-size: 9.5pt; }

/* Page breaks */
.report-section { page-break-inside: avoid; }

@page { margin: 0; size: A4 portrait; }
@media print {
  body { padding: 15mm; }
  .report-section { page-break-inside: avoid; }
}
"""


def setup_routes(app):

    @app.get("/accounting")
    async def accounting_page(request: Request):
        """Accounting landing — shows P&L by default (most useful for business owners)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        tab = request.query_params.get("tab", "pnl")
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            if tab == "pnl":
                fy = await _get_fiscal(token)
                d_from, d_to, preset = _parse_dates(request, fy)
                params = {}
                if d_from:
                    params["date_from"] = d_from
                if d_to:
                    params["date_to"] = d_to
                data = await api.get_pnl(token, params)
                content = Div(
                    Div(
                        _date_filter_bar("/accounting", d_from, d_to, preset,
                                         settings_link="/settings/general?tab=company",
                                         extra_params="&tab=pnl"),
                        _action_bar("pnl", {"date_from": d_from or "", "date_to": d_to or ""}),
                        cls="flex-row flex-between",
                    ),
                    _pnl_view(data, currency),
                )
            elif tab == "balance-sheet":
                as_of = request.query_params.get("as_of", "") or _date.today().isoformat()
                params = {"as_of": as_of} if as_of else {}
                data = await api.get_balance_sheet(token, params)
                as_of_form = Form(
                    Label(t("label.as_of_date"), cls="form-label"),
                    Input(type="date", name="as_of", value=as_of, cls="date-input"),
                    Input(type="hidden", name="tab", value="balance-sheet"),
                    Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
                    action="/accounting",
                    method="get",
                    cls="date-custom-form",
                )
                content = Div(
                    Div(
                        Div(as_of_form, cls="date-filter-bar"),
                        _action_bar("balance-sheet", {"as_of": as_of}),
                        cls="flex-row flex-between",
                    ),
                    _balance_sheet_view(data, currency),
                )
            elif tab == "trial-balance":
                trial_balance = await api.get_trial_balance(token)
                content = Div(
                    Div(
                        Div(cls="date-filter-bar"),  # spacer to keep action bar right-aligned
                        _action_bar("trial-balance", {}),
                        cls="flex-row flex-between",
                    ),
                    _trial_balance_summary(trial_balance, currency),
                    _trial_balance_table(trial_balance, currency),
                )
            else:
                return RedirectResponse("/accounting", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = Div(f"Error loading data: {e.detail}", cls="error-banner")

        return base_shell(
            page_header(t("page.accounting", get_lang(request))),
            _accounting_tabs(tab),
            content,
            title="Accounting - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.get("/accounting/pnl")
    async def pnl_page(request: Request):
        qs = "?tab=pnl"
        if request.query_params.get("from"):
            qs += f"&from={request.query_params['from']}"
        if request.query_params.get("to"):
            qs += f"&to={request.query_params['to']}"
        return RedirectResponse(f"/accounting{qs}", status_code=302)

    @app.get("/accounting/balance-sheet")
    async def balance_sheet_page(request: Request):
        return RedirectResponse("/accounting?tab=balance-sheet", status_code=302)

    # ── Print (PDF) routes ─────────────────────────────────────────────────

    @app.get("/accounting/print/pnl")
    async def pnl_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company, fy = await asyncio.gather(api.get_company(token), _get_fiscal(token))
            currency = company.get("currency")
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = {k: v for k, v in {"date_from": d_from, "date_to": d_to}.items() if v}
            data = await api.get_pnl(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return PlainTextResponse(f"Error: {e.detail}", status_code=500)

        subtitle_parts = []
        if d_from:
            subtitle_parts.append(f"From: {d_from}")
        if d_to:
            subtitle_parts.append(f"To: {d_to}")
        subtitle = "  |  ".join(subtitle_parts) if subtitle_parts else "All periods"

        body = _pnl_view(data, currency)
        return _print_shell(company, "Profit & Loss Statement", subtitle, body)

    @app.get("/accounting/print/balance-sheet")
    async def balance_sheet_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            as_of = request.query_params.get("as_of", "") or _date.today().isoformat()
            params = {"as_of": as_of}
            data = await api.get_balance_sheet(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return PlainTextResponse(f"Error: {e.detail}", status_code=500)

        body = _balance_sheet_view(data, currency)
        return _print_shell(company, "Balance Sheet", f"As of: {as_of}", body)

    @app.get("/accounting/print/trial-balance")
    async def trial_balance_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            data = await api.get_trial_balance(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return PlainTextResponse(f"Error: {e.detail}", status_code=500)

        body = Div(
            _trial_balance_summary(data, currency),
            _trial_balance_table(data, currency),
        )
        return _print_shell(company, "Trial Balance", f"As of: {_date.today().isoformat()}", body)

    # ── CSV export routes ──────────────────────────────────────────────────

    @app.get("/accounting/export/pnl/csv")
    async def pnl_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = {k: v for k, v in {"date_from": d_from, "date_to": d_to}.items() if v}
            data = await api.get_pnl(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return PlainTextResponse(f"Error: {e.detail}", status_code=500)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Section", "Code", "Account", "Amount", "% of Revenue"])
        rev_total = float(data.get("revenue", {}).get("total", 0) or 0)

        def _pct(amount: float) -> str:
            return f"{abs(amount) / rev_total * 100:.1f}%" if rev_total else ""

        for section_key, section_label in [
            ("revenue", "Revenue"),
            ("cogs", "Cost of Goods Sold"),
            ("expenses", "Operating Expenses"),
        ]:
            section = data.get(section_key, {})
            for line in section.get("lines", []):
                amt = float(line.get("amount", 0) or 0)
                w.writerow([section_label, line.get("code", ""), line.get("name", ""), amt, _pct(amt)])
            w.writerow([f"TOTAL {section_label}", "", "", section.get("total", 0), ""])

        w.writerow([])
        w.writerow(["Gross Profit", "", "", data.get("gross_profit", 0), ""])
        w.writerow(["Net Profit", "", "", data.get("net_profit", 0), ""])

        buf.seek(0)
        fname = f"pnl_{d_from or 'all'}_{d_to or 'all'}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/accounting/export/balance-sheet/csv")
    async def balance_sheet_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            as_of = request.query_params.get("as_of", "") or _date.today().isoformat()
            data = await api.get_balance_sheet(token, {"as_of": as_of})
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return PlainTextResponse(f"Error: {e.detail}", status_code=500)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Section", "Code", "Account", "Amount"])
        for section_key, section_label in [("assets", "Assets"), ("liabilities", "Liabilities"), ("equity", "Equity")]:
            section = data.get(section_key, {})
            for line in section.get("lines", []):
                w.writerow([section_label, line.get("code", ""), line.get("name", ""), line.get("amount", 0)])
            w.writerow([f"TOTAL {section_label}", "", "", section.get("total", 0)])
            w.writerow([])

        buf.seek(0)
        fname = f"balance_sheet_{as_of}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/accounting/export/trial-balance/csv")
    async def trial_balance_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            data = await api.get_trial_balance(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return PlainTextResponse(f"Error: {e.detail}", status_code=500)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Code", "Account", "Debit", "Credit"])
        for line in data.get("lines", []):
            w.writerow([line.get("code", ""), line.get("name", ""), line.get("total_debit", 0), line.get("total_credit", 0)])
        w.writerow([])
        w.writerow(["TOTAL", "", data.get("total_debit", 0), data.get("total_credit", 0)])

        buf.seek(0)
        fname = f"trial_balance_{_date.today().isoformat()}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )


def _accounting_tabs(active: str) -> FT:
    tabs = [
        ("pnl", "P&L"),
        ("balance-sheet", "Balance Sheet"),
        ("trial-balance", "Trial Balance"),
    ]
    return Div(
        *[
            A(label, href=f"/accounting?tab={key}",
              cls=f"tab-link {'tab-link--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="tab-bar",
    )


def _trial_balance_table(tb: dict, currency: str | None = None) -> FT:
    from ui.components.table import fmt_money
    lines = tb.get("lines", [])
    if not lines:
        return P(t("acct.no_trial_balance_entries"), cls="empty-state")
    rows = [
        Tr(
            Td(l.get("code", ""), cls="cell--mono"),
            Td(l.get("name", "")),
            Td(fmt_money(l.get('total_debit', 0), currency), cls="cell--number"),
            Td(fmt_money(l.get('total_credit', 0), currency), cls="cell--number"),
        )
        for l in lines
    ]
    return Table(
        Thead(Tr(Th(t("th.code")), Th(t("th.account")), Th(t("th.debit"), cls="cell--number"), Th(t("th.credit"), cls="cell--number"))),
        Tbody(*rows),
        cls="data-table",
    )


def _trial_balance_summary(tb: dict, currency: str | None = None) -> FT:
    from ui.components.table import fmt_money
    balanced = tb.get("balanced", True)
    return Div(
        Span(f"Total Debit: {fmt_money(tb.get('total_debit', 0), currency)}", cls="val-chip"),
        Span(f"Total Credit: {fmt_money(tb.get('total_credit', 0), currency)}", cls="val-chip"),
        Span("Balanced ✓" if balanced else "⚠ Out of balance",
             cls="val-chip" if balanced else "val-chip val-chip--alert"),
        cls="valuation-bar",
    )


def _pnl_view(data: dict, currency: str | None = None) -> FT:
    from ui.components.table import fmt_money

    rev_total = float(data.get("revenue", {}).get("total", 0) or 0)

    def _pct_of_rev(amount: float) -> str:
        if not rev_total:
            return "--"
        return f"{abs(amount) / rev_total * 100:.1f}%"

    def _section(title, section_data, show_pct: bool = False, cls=""):
        lines = section_data.get("lines", [])
        rows = []
        for l in lines:
            amt = float(l.get("amount", 0) or 0)
            cells = [
                Td(f"{l.get('code', '')} {l.get('name', '')}".strip()),
                Td(fmt_money(amt, currency), cls="cell--number"),
            ]
            if show_pct:
                cells.append(Td(_pct_of_rev(amt), cls="cell--right cell--muted"))
            rows.append(Tr(*cells))
        header_row = Tr(
            Th(t("th.account")),
            Th(t("label.amount"), cls="cell--number"),
            *([] if not show_pct else [Th(t("th._of_revenue"), cls="cell--right")]),
        )
        return Div(
            H3(title, cls="report-section-title"),
            Table(Thead(header_row), Tbody(*rows), cls="data-table data-table--compact") if rows else P(t("acct.no_entries"), cls="empty-state"),
            P(Strong(fmt_money(section_data.get('total', 0), currency)), cls="section-total"),
            cls=f"report-section {cls}",
        )

    net = float(data.get("net_profit", 0))
    return Div(
        _section("Revenue", data.get("revenue", {}), show_pct=True),
        _section("Cost of Goods Sold", data.get("cogs", {}), show_pct=True),
        Div(P(Strong(f"Gross Profit: {fmt_money(data.get('gross_profit', 0), currency)}")), cls="report-subtotal"),
        _section("Operating Expenses", data.get("expenses", {}), show_pct=True),
        Div(
            P(Strong(f"Net Profit: {fmt_money(net, currency)}"),
              cls=f"net-profit {'net-profit--positive' if net >= 0 else 'net-profit--negative'}"),
            cls="report-total",
        ),
        cls="report-view",
    )


def _balance_sheet_view(data: dict, currency: str | None = None) -> FT:
    from ui.components.table import fmt_money

    def _section(title, section_data):
        lines = section_data.get("lines", [])
        rows = [Tr(Td(f"{l.get('code', '')} {l.get('name', '')}".strip()),
                   Td(fmt_money(l.get('amount', 0), currency), cls="cell--number"))
                for l in lines]
        return Div(
            H3(title, cls="report-section-title"),
            Table(Tbody(*rows), cls="data-table data-table--compact") if rows else P(t("acct.no_entries"), cls="empty-state"),
            P(Strong(fmt_money(section_data.get('total', 0), currency)), cls="section-total"),
            cls="report-section",
        )

    balanced = data.get("balanced", True)
    total_assets = data.get("assets", {}).get("total", 0)
    total_liab = data.get("liabilities", {}).get("total", 0)
    total_equity = data.get("equity", {}).get("total", 0)
    return Div(
        _section("Assets", data.get("assets", {})),
        _section("Liabilities", data.get("liabilities", {})),
        _section("Equity", data.get("equity", {})),
        Div(
            P(
                Strong(f"Total Liabilities & Equity: {fmt_money(total_liab + total_equity, currency)}"),
                cls="section-total",
            ),
            Span("Balance checks out ✓" if balanced else "⚠ Imbalance detected",
                 cls="val-chip" if balanced else "val-chip val-chip--alert"),
            cls="valuation-bar",
        ),
        cls="report-view",
    )
