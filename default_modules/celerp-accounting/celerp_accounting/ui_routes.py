# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import get_token as _token
from ui.routes.reports import _date_filter_bar, _get_fiscal, _parse_dates
from ui.i18n import t, get_lang




def setup_routes(app):

    @app.get("/accounting")
    async def accounting_page(request: Request):
        """Accounting landing — shows P&L by default (most useful for business owners)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        tab = request.query_params.get("tab", "pnl")
        lang = get_lang(request)
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
                    _date_filter_bar("/accounting", d_from, d_to, preset,
                                     settings_link="/settings?tab=company",
                                     extra_params="&tab=pnl",
                                     lang=lang),
                    _pnl_view(data, currency, lang=lang),
                )
            elif tab == "balance-sheet":
                from datetime import date as _date
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
                    Div(as_of_form, cls="date-filter-bar"),
                    _balance_sheet_view(data, currency, lang=lang),
                )
            elif tab == "trial-balance":
                trial_balance = await api.get_trial_balance(token)
                content = Div(_trial_balance_summary(trial_balance, currency, lang=lang), _trial_balance_table(trial_balance, currency))
            else:
                return RedirectResponse("/accounting", status_code=302)
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return RedirectResponse("/login", status_code=302)
            content = Div(f"Error loading data: {getattr(e, 'detail', str(e))}", cls="error-banner")

        return base_shell(
            page_header(t("nav.accounting", lang)),
            _accounting_tabs(tab, lang=lang),
            content,
            title="Accounting - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.get("/accounting/pnl")
    async def pnl_page(request: Request):
        """Redirect to tabbed accounting view."""
        qs = f"?tab=pnl"
        if request.query_params.get("from"):
            qs += f"&from={request.query_params['from']}"
        if request.query_params.get("to"):
            qs += f"&to={request.query_params['to']}"
        return RedirectResponse(f"/accounting{qs}", status_code=302)

    @app.get("/accounting/balance-sheet")
    async def balance_sheet_page(request: Request):
        """Redirect to tabbed accounting view."""
        return RedirectResponse("/accounting?tab=balance-sheet", status_code=302)


def _accounting_tabs(active: str, lang: str = "en") -> FT:
    tabs = [
        ("pnl", t("acct.tab_pnl", lang)),
        ("balance-sheet", t("acct.tab_balance_sheet", lang)),
        ("trial-balance", t("acct.tab_trial_balance", lang)),
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
            Td(l.get("code", "")),
            Td(l.get("name", "")),
            Td(fmt_money(l.get('total_debit', 0), currency), cls="cell--number"),
            Td(fmt_money(l.get('total_credit', 0), currency), cls="cell--number"),
        )
        for l in lines
    ]
    return Table(
        Thead(Tr(Th(t("th.code")), Th(t("th.account")), Th(t("th.debit")), Th(t("th.credit")))),
        Tbody(*rows),
        cls="data-table",
    )


def _chart_table(chart: list[dict]) -> FT:
    def _row(a: dict) -> FT:
        return Tr(
            Td(a.get("code", ""), cls="cell--mono"),
            Td(a.get("name", "")),
            Td(Span(a.get("account_type", ""), cls=f"badge badge--{a.get('account_type', '')}")),
            Td(a.get("parent_code", "")),
            Td(Span("Active" if a.get("is_active", True) else "Inactive",
                    cls="badge badge--active" if a.get("is_active", True) else "badge badge--inactive")),
            cls="data-row",
        )

    by_type = {}
    for a in chart:
        atype = a.get("account_type", "other")
        by_type.setdefault(atype, []).append(a)

    sections = []
    for atype in ("asset", "liability", "equity", "revenue", "cogs", "expense", "other"):
        accounts = by_type.get(atype, [])
        if not accounts:
            continue
        sections.append(Tr(Th(atype.title(), colspan="5", cls="section-header")))
        sections.extend(_row(a) for a in accounts)

    return Table(
        Thead(Tr(Th(t("th.code")), Th(t("th.name")), Th(t("th.doc_type")), Th(t("th.parent")), Th(t("th.status")))),
        Tbody(*sections),
        cls="data-table",
    )


def _trial_balance_summary(tb: dict, currency: str | None = None, lang: str = "en") -> FT:
    from ui.components.table import fmt_money
    balanced = tb.get("balanced", True)
    return Div(
        Span(f"{t('acct.total_debit', lang)}: {fmt_money(tb.get('total_debit', 0), currency)}", cls="val-chip"),
        Span(f"{t('acct.total_credit', lang)}: {fmt_money(tb.get('total_credit', 0), currency)}", cls="val-chip"),
        Span(t("acct.balanced", lang) if balanced else t("acct.out_of_balance", lang),
             cls="val-chip" if balanced else "val-chip val-chip--alert"),
        cls="valuation-bar",
    )


def _pnl_view(data: dict, currency: str | None = None, lang: str = "en") -> FT:
    from ui.components.table import fmt_money

    def _section(title, section_data, cls=""):
        lines = section_data.get("lines", [])
        rows = [Tr(Td(f"{l.get('code', '')} {l.get('name', '')}".strip()),
                   Td(fmt_money(l.get('amount', 0), currency), cls="cell--number"))
                for l in lines]
        return Div(
            H3(title, cls="report-section-title"),
            Table(Tbody(*rows), cls="data-table data-table--compact") if rows else P(t("acct.no_entries"), cls="empty-state"),
            P(Strong(fmt_money(section_data.get('total', 0), currency)), cls="section-total"),
            cls=f"report-section {cls}",
        )

    net = float(data.get("net_profit", 0))
    return Div(
        _section(t("acct.section_revenue", lang), data.get("revenue", {})),
        _section(t("acct.section_cogs", lang), data.get("cogs", {})),
        Div(P(Strong(f"{t('acct.gross_profit', lang)}: {fmt_money(data.get('gross_profit', 0), currency)}")), cls="report-subtotal"),
        _section(t("acct.section_operating_expenses", lang), data.get("expenses", {})),
        Div(
            P(Strong(f"{t('acct.net_profit', lang)}: {fmt_money(net, currency)}"),
              cls=f"net-profit {'net-profit--positive' if net >= 0 else 'net-profit--negative'}"),
            cls="report-total",
        ),
        cls="report-view",
    )


def _balance_sheet_view(data: dict, currency: str | None = None, lang: str = "en") -> FT:
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
    return Div(
        _section(t("acct.section_assets", lang), data.get("assets", {})),
        _section(t("acct.section_liabilities", lang), data.get("liabilities", {})),
        _section(t("acct.section_equity", lang), data.get("equity", {})),
        Div(
            Span(t("acct.balance_checks_out", lang) if balanced else t("acct.imbalance_detected", lang),
                 cls="val-chip" if balanced else "val-chip val-chip--alert"),
            cls="valuation-bar",
        ),
        cls="report-view",
    )


def _bank_accounts_view(banks: list[dict], currency: str | None = None) -> FT:
    from ui.components.table import fmt_money

    if not banks:
        return Div(
            P(t("acct.no_bank_accounts_configured"), cls="empty-state"),
            A(t("acct.manage_bank_accounts"), href="/settings/accounting?tab=bank-accounts",
              cls="btn btn--secondary btn--sm"),
            cls="report-section",
        )

    cards = []
    for b in banks:
        balance = float(b.get("balance", 0))
        cur = b.get("currency") or currency or ""
        bal_cls = "balance--positive" if balance >= 0 else "balance--negative"
        cards.append(Div(
            Div(
                Span(b.get("bank_name", ""), cls="account-name"),
                Span(
                    f"{b.get('bank_type', '').replace('_', ' ').title()} · {b.get('account_number', '')}",
                    cls="bank-name-label",
                ),
                cls="bank-info",
            ),
            Div(
                Span(f"{cur} {balance:,.2f}", cls=f"balance {bal_cls}"),
                Span(f"Account: {b.get('chart_account_code', '')}", cls="bank-name-label"),
                cls="bank-balance-col text-right",
            ),
            cls="bank-account-card",
        ))

    return Div(
        Div(
            A(t("acct.manage_bank_accounts"), href="/settings/accounting?tab=bank-accounts",
              cls="btn btn--secondary btn--sm"),
            cls="page-actions mb-md",
        ),
        *cards,
        cls="report-view",
    )


def setup_ui_routes(app) -> None:
    """Alias for setup_routes — used by module loader and conftest."""
    setup_routes(app)
