# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → Accounting: Bank Accounts."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, flash, page_header, page_title
from ui.config import COOKIE_NAME
from celerp.constants import ISO_4217_CURRENCIES as _ISO_CURRENCIES
from ui.components.table import EMPTY, add_new_option, searchable_select, display_enum

from ui.routes.accounting_import import ACCOUNT_TYPES

# The cash flow sections an account may be pinned to. The accounting API owns this
# list (celerp_accounting.routes.CASH_FLOW_CATEGORIES) and validates against it; the
# two run in separate processes, so a test asserts they still match.
CASH_FLOW_CATEGORIES = ("operating", "investing", "financing")
from ui.routes.settings import _token, _check_permission
from ui.routes.settings_general import _section_breadcrumb
from ui.i18n import t


# Raw bank-account type values. Canonical everywhere (API, comparisons); the
# human label is resolved at render via display_enum(domain="bank_type").
_BANK_TYPES = ("checking", "savings", "credit_card")



def _accounting_settings_tabs(active: str) -> FT:
    tabs = [
        ("bank-accounts", t("settings_accounting.tab_bank_accounts")),
        ("chart", t("settings_accounting.chart_of_accounts")),
        ("rules", t("page.reconciliation_rules")),
        ("period-lock", t("page.period_lock")),
    ]
    return Div(
        *[
            A(label, href=f"/settings/accounting?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _bank_account_row(b: dict) -> FT:
    balance = b.get("balance", 0.0)
    bal_cls = "balance--positive" if balance >= 0 else "balance--negative"
    currency = b.get("currency", "")
    return Div(
        Div(
            Span(b.get("bank_name", ""), cls="account-name"),
            Span(f"{display_enum(b.get('bank_type', ''), 'bank_type')} · {b.get('account_number', '')} · {b.get('chart_account_code', '')}", cls="bank-name-label"),
            cls="bank-info",
        ),
        Div(
            Span(f"{currency} {balance:,.2f}", cls=f"balance {bal_cls}"),
            # The balance is the ledger's. Say so when an opening balance was
            # entered here that no journal entry carries, rather than letting the
            # figure look wrong for no stated reason.
            *([P(t("acct.opening_balance_unbacked"), cls="flash flash--warning")]
              if b.get("opening_unbacked") else []),
            Div(
                A(t("btn.edit"), href=f"/settings/accounting/bank-accounts/{b['id']}/edit",
                  cls="btn btn--secondary btn--xs"),
                A(t("acct.reconcile"), href="/accounting/reconcile/start",
                  cls="btn btn--primary btn--xs"),
                Button(
                    t("inv.archive") if b.get("is_active") else t("btn.restore"),
                    hx_patch=f"/settings/accounting/bank-accounts/{b['id']}/toggle",
                    hx_target="#bank-accounts-list",
                    hx_swap="outerHTML",
                    cls="btn btn--secondary btn--xs",
                ),
                cls="row-actions flex-row gap-sm",
            ),
            cls="bank-balance-col flex-end gap-xs",
        ),
        cls="bank-account-card",
    )


def _bank_accounts_tab(banks: list[dict]) -> FT:
    active = [b for b in banks if b.get("is_active")]
    inactive = [b for b in banks if not b.get("is_active")]
    rows = [_bank_account_row(b) for b in active]
    archived = [_bank_account_row(b) for b in inactive] if inactive else []
    return Div(
        Div(
            A(t("acct.add_bank_account"), href="/settings/accounting/bank-accounts/new",
              cls="btn btn--primary"),
            cls="page-actions mb-md",
        ),
        Div(*rows, id="bank-accounts-list") if rows else Div(
            P(t("acct.no_bank_accounts_yet_add_one_to_start_tracking_cas"), cls="empty-state"),
            id="bank-accounts-list",
        ),
        *(
            [Details(
                Summary(t("acct.archived_accounts"), cls="text-muted mt-md"),
                Div(*archived),
            )] if archived else []
        ),
        cls="settings-card",
    )


def _period_lock_tab(lock_data: dict) -> FT:
    lock_date = lock_data.get("lock_date") or ""
    set_at = lock_data.get("lock_date_set_at") or ""
    return Div(
        H3(t("page.period_lock"), cls="section-title"),
        P(
            t("settings_accounting.period_lock_help"),
            cls="text-muted mb-md",
        ),
        Form(
            Div(
                Label(t("label.lock_through_date")),
                Input(type="date", name="lock_date", value=lock_date,
                      cls="form-input max-w-md"),
                cls="form-field",
            ),
            Div(
                Button(t("btn.update_lock_date"), type="submit", cls="btn btn--primary"),
                *(
                    [Button(t("btn.unlock_remove_lock"), type="submit", name="unlock", value="1",
                            cls="btn btn--outline ml-sm")]
                    if lock_date else []
                ),
                cls="form-actions mt-md",
            ),
            hx_post="/settings/accounting/period-lock",
            hx_target="#period-lock-content",
            hx_swap="outerHTML",
        ),
        *(
            [P(t("settings_accounting.currently_locked_through",
                 lock_date=lock_date,
                 updated=(set_at[:10] if set_at else t("settings_accounting.unknown"))),
               cls="text-muted mt-md")]
            if lock_date else []
        ),
        Hr(cls="section-divider mt-lg mb-lg"),
        H3(t("page.close_fiscal_year"), cls="section-title"),
        P(
            t("settings_accounting.close_year_help"),
            cls="text-muted mb-md",
        ),
        Form(
            Div(
                Label(t("label.fiscal_year_end_date")),
                Input(type="date", name="fiscal_year_end",
                      cls="form-input max-w-md"),
                cls="form-field",
            ),
            Div(
                Button(t("btn.close_year"), type="submit", cls="btn btn--danger",
                       hx_confirm=t("settings_accounting.close_year_confirm")),
                cls="form-actions mt-md",
            ),
            hx_post="/settings/accounting/close-year",
            hx_target="#period-lock-content",
            hx_swap="outerHTML",
        ),
        id="period-lock-content",
        cls="settings-card",
    )


def _rules_tab(rules: list[dict], banks: list[dict]) -> FT:
    bank_options = [Option(f"{b['bank_name']} {b.get('account_number', '')}", value=b["id"]) for b in banks]
    _bank_opt, _bank_js = add_new_option(t("settings_accounting.add_new_bank_account_option"), "/settings/accounting?tab=bank-accounts")
    _MATCH_TYPES = ("contains", "exact", "starts_with")

    rows = []
    for r in rules:
        rows.append(Tr(
            Td(r.get("match_pattern", EMPTY)),
            Td(display_enum(r.get("match_type"), "match_type") if r.get("match_type") else EMPTY),
            Td(r.get("target_account_code", EMPTY)),
            Td(r.get("default_memo") or EMPTY),
            Td(Span(t("th.active") if r.get("is_active") else t("settings.inactive"),
                    cls="badge badge--active" if r.get("is_active") else "badge badge--inactive")),
            Td(str(r.get("times_applied", 0))),
            Td(
                Button(t("btn.delete"),
                       hx_delete=f"/settings/accounting/rules/{r['id']}",
                       hx_target="#rules-list",
                       hx_swap="outerHTML",
                       hx_confirm=t("settings_accounting.delete_rule_confirm"),
                       cls="btn btn--xs btn--outline"),
            ),
        ))

    return Div(
        H3(t("page.reconciliation_rules"), cls="section-title"),
        P(t("acct.rules_autocategorise_matching_bank_statement_lines"),
          cls="text-muted mb-md"),
        Div(
            Table(
                Thead(Tr(Th(t("th.pattern")), Th(t("th.match_type")), Th(t("th.account")), Th(t("th.default_memo")),
                         Th(t("th.status")), Th(t("th.applied")), Th(""))),
                Tbody(*rows) if rows else Tbody(Tr(Td(t("acct.no_rules_yet"), colspan="7", cls="empty-state"))),
                cls="data-table",
            ),
            id="rules-list",
        ),
        Hr(cls="section-divider mt-lg mb-lg"),
        H3(t("page.add_rule"), cls="section-title"),
        Form(
            Div(
                Label(t("label.bank_account"), cls="form-label"),
                Select(*bank_options, _bank_opt, name="bank_account_id",
                       cls="form-input cell-input--select", onchange=_bank_js),
                cls="form-field",
            ),
            Div(
                Label(t("label.match_pattern"), cls="form-label"),
                Input(type="text", name="match_pattern", placeholder=t("settings_accounting.ph_match_pattern"),
                      cls="form-input", required=True),
                cls="form-field",
            ),
            Div(
                Label(t("th.match_type"), cls="form-label"),
                Select(*[Option(display_enum(v, "match_type"), value=v) for v in _MATCH_TYPES],
                       name="match_type", cls="form-input cell-input--select"),
                cls="form-field",
            ),
            Div(
                Label(t("label.target_account_code"), cls="form-label"),
                Input(type="text", name="target_account_code", placeholder=t("settings_accounting.ph_account_code"),
                      cls="form-input", required=True),
                cls="form-field",
            ),
            Div(
                Label(t("label.default_memo_optional"), cls="form-label"),
                Input(type="text", name="default_memo", cls="form-input"),
                cls="form-field",
            ),
            Div(
                Button(t("page.add_rule"), type="submit", cls="btn btn--primary"),
                cls="form-actions mt-md",
            ),
            hx_post="/settings/accounting/rules",
            hx_target="#rules-list",
            hx_swap="outerHTML",
        ),
        cls="settings-card",
    )


def _cash_flow_display_cell(code: str, cash_flow_category: str | None) -> FT:
    """Read-only cash flow section cell. Click fires HTMX GET to fetch the select
    editor in place, matching the click-to-edit cells used elsewhere (e.g. the
    document numbering table in settings_sales.py)."""
    display = (display_enum(cash_flow_category, "cash_flow") if cash_flow_category else "") or EMPTY
    return Td(
        Div(
            display,
            hx_get=f"/settings/accounting/chart/{code}/cash-flow/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title=t("settings.click_to_edit"), cls="editable-cell",
        ),
    )


def _cash_flow_edit_cell(a: dict) -> FT:
    """Table cell in edit mode: a select of the derived option plus the three
    cash flow sections. Fires HTMX PATCH on change, swaps itself back on save."""
    code = a.get("code", "")
    current = a.get("cash_flow_category") or ""
    restore_url = f"/settings/accounting/chart/{code}/cash-flow/display"
    esc_js = (
        f"if(event.key==='Escape'){{htmx.ajax('GET','{restore_url}',"
        f"{{target:this.closest('td'),swap:'outerHTML'}});event.preventDefault();}}"
    )
    return Td(
        Select(
            Option(t("acct.cash_flow_derived"), value="", selected=not current),
            *[Option(display_enum(x, "cash_flow"), value=x, selected=(x == current)) for x in CASH_FLOW_CATEGORIES],
            name="value",
            hx_patch=f"/settings/accounting/chart/{code}/cash-flow",
            hx_target="closest td", hx_swap="outerHTML", hx_trigger="change",
            cls="cell-input cell-input--select", autofocus=True,
            onkeydown=esc_js,
        ),
        cls="cell cell--editing",
    )


def _chart_table(chart: list[dict]) -> FT:
    def _row(a: dict) -> FT:
        code = a.get("code", "")
        return Tr(
            Td(code, cls="cell--mono"),
            Td(a.get("name", "")),
            Td(Span(display_enum(a.get("account_type", ""), "account_type"), cls=f"badge badge--{a.get('account_type', '')}")),
            Td(a.get("parent_code") or EMPTY),
            _cash_flow_display_cell(code, a.get("cash_flow_category")),
            Td(Span(t("th.active") if a.get("is_active", True) else t("settings.inactive"),
                    cls="badge badge--active" if a.get("is_active", True) else "badge badge--inactive")),
            Td(A(t("btn.edit"), href=f"/settings/accounting/chart/{code}/edit",
                 cls="btn btn--secondary btn--xs")),
            cls="data-row",
        )

    by_type: dict[str, list] = {}
    for a in chart:
        atype = a.get("account_type", "other")
        by_type.setdefault(atype, []).append(a)

    sections: list[FT] = []
    for atype in ACCOUNT_TYPES:
        accounts = by_type.get(atype, [])
        if not accounts:
            continue
        sections.append(Tr(Th(display_enum(atype, "account_type"), colspan="7", cls="section-header")))
        sections.extend(_row(a) for a in accounts)

    return Table(
        Thead(Tr(Th(t("th.code")), Th(t("th.name")), Th(t("th.doc_type")), Th(t("th.parent")),
                 Th(t("th.cash_flow_section")), Th(t("th.status")), Th(""))),
        Tbody(*sections),
        cls="data-table sticky-head",
    )


def _chart_tab(chart: list[dict]) -> FT:
    return Div(
        Div(
            A(t("acct.add_account"), href="/settings/accounting/chart/new", cls="btn btn--primary"),
            A(t("acct.import_chart_csv"), href="/accounting/import/chart", cls="btn btn--secondary"),
            Form(
                Button(t("btn.seed_default_chart"), type="submit", cls="btn btn--secondary"),
                hx_post="/settings/accounting/chart/seed",
                hx_target="#chart-content",
                hx_swap="outerHTML",
                style="display:inline",
            ) if not chart else None,
            cls="page-actions flex-row gap-sm mb-md",
        ),
        Div(_chart_table(chart), id="chart-content") if chart else Div(
            P(t("acct.no_accounts_yet_seed_the_default_chart_or_add_acco"), cls="empty-state"),
            id="chart-content",
        ),
        cls="settings-card",
    )


def _account_form(chart: list[dict], values: dict | None = None) -> FT:
    """Add/edit form for a chart account. Pass values to edit an existing one."""
    v = values or {}
    editing = bool(v)
    code = v.get("code", "")
    parent_opts = [("", EMPTY)] + [
        (a["code"], f"{a['code']} {a.get('name', '')}")
        for a in chart if a.get("code") and a.get("code") != code
    ]
    rows = [
        Tr(
            Td(t("th.code"), cls="detail-label"),
            Td(Span(code, cls="cell--mono")) if editing else
            Td(Input(type="text", name="code", placeholder=t("settings_accounting.ph_code"), maxlength="32",
                     cls="cell-input", required=True)),
        ),
        Tr(
            Td(t("th.name"), cls="detail-label"),
            Td(Input(type="text", name="name", value=v.get("name", ""),
                     placeholder=t("settings_accounting.ph_name"), cls="cell-input", required=True)),
        ),
        Tr(
            Td(t("th.doc_type"), cls="detail-label"),
            Td(Select(
                *[Option(display_enum(x, "account_type"), value=x, selected=(x == v.get("account_type", "asset")))
                  for x in ACCOUNT_TYPES],
                name="account_type", cls="cell-input cell-input--select",
            )),
        ),
        Tr(
            Td(t("th.parent"), cls="detail-label"),
            Td(searchable_select("parent_code", parent_opts, value=v.get("parent_code") or "")),
        ),
    ]
    rows.append(Tr(
        Td(t("th.cash_flow_section"), cls="detail-label"),
        Td(Select(
            Option(t("acct.cash_flow_derived"), value="",
                   selected=not v.get("cash_flow_category")),
            *[Option(display_enum(x, "cash_flow"), value=x, selected=(x == v.get("cash_flow_category")))
              for x in CASH_FLOW_CATEGORIES],
            name="cash_flow_category", cls="cell-input cell-input--select",
        )),
    ))
    if editing:
        rows.append(Tr(
            Td(t("th.status"), cls="detail-label"),
            Td(Select(
                Option(t("th.active"), value="true", selected=bool(v.get("is_active", True))),
                Option(t("settings.inactive"), value="false", selected=not v.get("is_active", True)),
                name="is_active", cls="cell-input cell-input--select",
            )),
        ))
    action = {"hx_patch": f"/settings/accounting/chart/{code}"} if editing else \
             {"hx_post": "/settings/accounting/chart/new"}
    return Form(
        Div(
            Table(*rows, cls="detail-table"),
            Div(
                Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                A(t("btn.cancel"), href="/settings/accounting?tab=chart",
                  cls="btn btn--secondary ml-sm"),
                cls="mt-md",
            ),
            Div(id="account-form-error"),
        ),
        onkeydown="if(event.key==='Escape'){window.location='/settings/accounting?tab=chart'}",
        hx_target="#account-form-error",
        hx_swap="innerHTML",
        **action,
    )


async def _account_error_page(request: Request, message: str) -> FT:
    return await base_shell(
        _section_breadcrumb(t("page.accounting")),
        page_header(
            t("settings_accounting.chart_of_accounts"),
            A(t("btn.back_to_settings"), href="/settings/accounting?tab=chart", cls="btn btn--secondary"),
        ),
        Div(P(message, cls="error-banner"), cls="settings-card"),
        title=page_title("settings_accounting.chart_of_accounts"),
        nav_active="settings-accounting",
        request=request,
    )


async def _validate_account(token: str, name: str, account_type: str,
                            parent_code: str, own_code: str) -> str | None:
    """Field-level validation shared by account create and patch. Returns an
    error message, or None when the fields are valid."""
    if not name:
        return t("settings_accounting.name_required")
    if account_type not in ACCOUNT_TYPES:
        return t("settings_accounting.account_type_invalid", types=", ".join(ACCOUNT_TYPES))
    if parent_code:
        if parent_code == own_code:
            return t("settings_accounting.account_own_parent")
        try:
            chart = (await api.get_chart(token)).get("items", [])
        except APIError as e:
            return str(e.detail)
        if not any(a.get("code") == parent_code for a in chart):
            return t("settings_accounting.parent_account_not_exist", code=parent_code)
    return None


def setup_routes(app):

    @app.get("/settings/accounting")
    async def settings_accounting_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_module_settings")):
            return r
        tab = request.query_params.get("tab", "bank-accounts")
        try:
            banks_data = await api.get_bank_accounts(token, include_inactive=True)
            banks = banks_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            banks = []

        if tab == "bank-accounts":
            content = _bank_accounts_tab(banks)
        elif tab == "chart":
            try:
                chart_data = await api.get_chart(token)
                content = _chart_tab(chart_data.get("items", []))
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                content = Div(P(str(e.detail), cls="error-banner"), cls="settings-card")
        elif tab == "rules":
            try:
                rules_data = await api.get_recon_rules(token)
                rules = rules_data.get("items", [])
            except Exception:
                rules = []
            content = _rules_tab(rules, banks)
        elif tab == "period-lock":
            try:
                lock_data = await api.get_period_lock(token)
            except Exception:
                lock_data = {}
            content = _period_lock_tab(lock_data)
        else:
            content = _bank_accounts_tab(banks)
            tab = "bank-accounts"

        msg = request.query_params.get("msg", "").strip()
        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(t("settings_accounting.finance_settings")),
            flash(msg) if msg else None,
            _accounting_settings_tabs(tab),
            content,
            title=page_title("settings_accounting.finance_settings"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.get("/settings/accounting/bank-accounts/new")
    async def new_bank_account_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "USD")
        except APIError:
            currency = "USD"

        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("acct.add_bank_account"),
                A(t("btn.back_to_settings"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
            ),
            Div(
                Form(
                    Div(
                        Table(
                            Tr(
                                Td(t("acct.bank_name"), cls="detail-label"),
                                Td(Input(type="text", name="bank_name", placeholder=t("settings_accounting.ph_bank_name"),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_number"), cls="detail-label"),
                                Td(Input(type="text", name="account_number", placeholder=t("settings_accounting.ph_account_number"),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_type"), cls="detail-label"),
                                Td(Select(
                                    *[Option(display_enum(v, "bank_type"), value=v) for v in _BANK_TYPES],
                                    name="bank_type", cls="cell-input cell-input--select",
                                )),
                            ),
                            Tr(
                                Td(t("th.currency"), cls="detail-label"),
                                Td(
                                    Select(
                                        *[Option(c, value=c, selected=(c == currency)) for c in sorted(_ISO_CURRENCIES)],
                                        name="currency", cls="cell-input cell-input--select", required=True,
                                    ),
                                    P(t("acct.using_a_foreigncurrency_account_the_multicurrency"), cls="form-hint"),
                                ),
                            ),
                            Tr(
                                Td(t("acct.opening_balance"), cls="detail-label"),
                                Td(Input(type="number", name="opening_balance", value="0",
                                         step="0.01", cls="cell-input cell-input--number")),
                            ),
                            cls="detail-table",
                        ),
                        Div(
                            Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                            A(t("btn.cancel"), href="/settings/accounting?tab=bank-accounts",
                              cls="btn btn--secondary ml-sm"),
                            cls="mt-md",
                        ),
                        Div(id="bank-form-error"),
                    ),
                    hx_post="/settings/accounting/bank-accounts/new",
                    hx_target="#bank-form-error",
                    hx_swap="innerHTML",
                ),
                cls="settings-card",
            ),
            title=page_title("acct.add_bank_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.post("/settings/accounting/bank-accounts/new")
    async def create_bank_account_submit(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        form = await request.form()
        bank_name = str(form.get("bank_name", "")).strip()
        account_number = str(form.get("account_number", "")).strip()
        bank_type = str(form.get("bank_type", "checking")).strip()
        currency = str(form.get("currency", "USD")).strip().upper()
        opening_balance_raw = str(form.get("opening_balance", "0")).strip()
        if not bank_name or not account_number:
            return P(t("acct.bank_name_and_account_number_are_required"), cls="error-banner")
        try:
            opening_balance = float(opening_balance_raw)
        except ValueError:
            return P(t("acct.opening_balance_must_be_a_number"), cls="error-banner")
        try:
            await api.create_bank_account(token, {
                "bank_name": bank_name,
                "account_number": account_number,
                "bank_type": bank_type,
                "currency": currency,
                "opening_balance": opening_balance,
            })
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=bank-accounts"})

    @app.get("/settings/accounting/bank-accounts/{bank_id}/edit")
    async def edit_bank_account_page(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            b = await api.get_bank_account(token, bank_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/settings/accounting?tab=bank-accounts", status_code=302)

        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("settings_accounting.edit_named", name=b.get("bank_name") or t("settings_accounting.bank_account")),
                A(t("btn.back_to_settings"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
            ),
            Div(
                Form(
                    Div(
                        Table(
                            Tr(
                                Td(t("acct.bank_name"), cls="detail-label"),
                                Td(Input(type="text", name="bank_name", value=b.get("bank_name", ""),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_number"), cls="detail-label"),
                                Td(Input(type="text", name="account_number", value=b.get("account_number", ""),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_type"), cls="detail-label"),
                                Td(Select(
                                    *[Option(display_enum(v, "bank_type"), value=v, selected=(v == b.get("bank_type")))
                                      for v in _BANK_TYPES],
                                    name="bank_type", cls="cell-input cell-input--select",
                                )),
                            ),
                            Tr(
                                Td(t("th.currency"), cls="detail-label"),
                                Td(Select(
                                    *[Option(c, value=c, selected=(c == b.get("currency", ""))) for c in sorted(_ISO_CURRENCIES)],
                                    name="currency", cls="cell-input cell-input--select", required=True,
                                )),
                            ),
                            cls="detail-table",
                        ),
                        Div(
                            Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                            A(t("btn.cancel"), href="/settings/accounting?tab=bank-accounts",
                              cls="btn btn--secondary ml-sm"),
                            cls="mt-md",
                        ),
                        Div(id="bank-form-error"),
                    ),
                    hx_patch=f"/settings/accounting/bank-accounts/{bank_id}",
                    hx_target="#bank-form-error",
                    hx_swap="innerHTML",
                ),
                cls="settings-card",
            ),
            title=page_title("settings_accounting.edit_bank_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.patch("/settings/accounting/bank-accounts/{bank_id}")
    async def patch_bank_account_route(request: Request, bank_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        form = await request.form()
        patch = {}
        for field in ("bank_name", "account_number", "bank_type", "currency"):
            v = str(form.get(field, "")).strip()
            if v:
                patch[field] = v
        if not patch:
            return P(t("acct.nothing_to_update"), cls="error-banner")
        try:
            await api.patch_bank_account(token, bank_id, patch)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=bank-accounts"})

    @app.patch("/settings/accounting/bank-accounts/{bank_id}/toggle")
    async def toggle_bank_account(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized")), id="bank-accounts-list")
        try:
            b = await api.get_bank_account(token, bank_id)
            await api.patch_bank_account(token, bank_id, {"is_active": not b.get("is_active", True)})
            banks_data = await api.get_bank_accounts(token, include_inactive=True)
            banks = banks_data.get("items", [])
        except APIError:
            banks = []
        active = [b for b in banks if b.get("is_active")]
        rows = [_bank_account_row(b) for b in active]
        return Div(*rows, id="bank-accounts-list") if rows else Div(
            P(t("acct.no_bank_accounts_yet_add_one_to_start_tracking_cas"), cls="empty-state"),
            id="bank-accounts-list",
        )

    @app.post("/settings/accounting/period-lock")
    async def post_period_lock(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        unlock = form.get("unlock")
        lock_date = None if unlock else (form.get("lock_date") or "").strip() or None
        try:
            result = await api.set_period_lock(token, lock_date)
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="period-lock-content")
        return _period_lock_tab(result)

    @app.post("/settings/accounting/close-year")
    async def post_close_year(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        fiscal_year_end = (form.get("fiscal_year_end") or "").strip()
        if not fiscal_year_end:
            return Div(P(t("acct.fiscal_year_end_date_is_required"), cls="error-banner"), id="period-lock-content")
        try:
            result = await api.close_fiscal_year(token, fiscal_year_end)
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="period-lock-content")
        lock_data = {"lock_date": result.get("lock_date")}
        content = _period_lock_tab(lock_data)
        # Prepend success message
        net = result.get("net_income", 0)
        msg = t("settings_accounting.fiscal_year_closed", date=fiscal_year_end, net=f"{net:,.2f}")
        return Div(P(msg, cls="success-banner mb-md"), content, id="period-lock-content")

    @app.post("/settings/accounting/rules")
    async def create_rule(request: Request):
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized")), id="rules-list")
        form = await request.form()
        data = {
            "bank_account_id": str(form.get("bank_account_id", "")).strip(),
            "match_pattern": str(form.get("match_pattern", "")).strip(),
            "match_type": str(form.get("match_type", "contains")).strip(),
            "target_account_code": str(form.get("target_account_code", "")).strip(),
            "default_memo": str(form.get("default_memo", "")).strip() or None,
        }
        if not data["bank_account_id"] or not data["match_pattern"] or not data["target_account_code"]:
            return Div(P(t("acct.bank_account_pattern_and_account_code_are_required"), cls="error-banner"), id="rules-list")
        try:
            await api.create_recon_rule(token, data)
            rules_data = await api.get_recon_rules(token)
            rules = rules_data.get("items", [])
            banks_data = await api.get_bank_accounts(token)
            banks = banks_data.get("items", [])
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="rules-list")
        return _rules_tab(rules, banks)

    @app.delete("/settings/accounting/rules/{rule_id}")
    async def delete_rule(request: Request, rule_id: str):
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized")), id="rules-list")
        try:
            await api.delete_recon_rule(token, rule_id)
            rules_data = await api.get_recon_rules(token)
            rules = rules_data.get("items", [])
            banks_data = await api.get_bank_accounts(token)
            banks = banks_data.get("items", [])
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="rules-list")
        return _rules_tab(rules, banks)

    @app.post("/settings/accounting/chart/seed")
    async def seed_chart_route(request: Request):
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized")), id="chart-content")
        try:
            await api.seed_chart(token)
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="chart-content")
        return Div(_chart_table(chart), id="chart-content")

    @app.get("/settings/accounting/chart/new")
    async def new_account_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _account_error_page(request, str(e.detail))
        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("acct.add_account"),
                A(t("btn.back_to_settings"), href="/settings/accounting?tab=chart", cls="btn btn--secondary"),
            ),
            Div(_account_form(chart), cls="settings-card"),
            title=page_title("acct.add_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.post("/settings/accounting/chart/new")
    async def create_account_submit(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        form = await request.form()
        code = str(form.get("code", "")).strip()
        name = str(form.get("name", "")).strip()
        account_type = str(form.get("account_type", "")).strip()
        parent_code = str(form.get("parent_code", "")).strip()
        if not code:
            return P(t("settings_accounting.code_required"), cls="error-banner")
        if len(code) > 32:
            return P(t("settings_accounting.code_too_long"), cls="error-banner")
        err = await _validate_account(token, name, account_type, parent_code, own_code=code)
        if err:
            return P(err, cls="error-banner")
        try:
            await api.create_account(token, {
                "code": code,
                "name": name,
                "account_type": account_type,
                "parent_code": parent_code or None,
                "cash_flow_category": str(form.get("cash_flow_category", "")).strip(),
            })
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=chart"})

    @app.get("/settings/accounting/chart/{code}/edit")
    async def edit_account_page(request: Request, code: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _account_error_page(request, str(e.detail))
        acct = next((a for a in chart if a.get("code") == code), None)
        if acct is None:
            return await _account_error_page(request, t("settings_accounting.account_not_found"))
        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("settings_accounting.edit_named", name=acct.get("name") or code),
                A(t("btn.back_to_settings"), href="/settings/accounting?tab=chart", cls="btn btn--secondary"),
            ),
            Div(_account_form(chart, values=acct), cls="settings-card"),
            title=page_title("settings_accounting.edit_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.patch("/settings/accounting/chart/{code}")
    async def patch_account_submit(request: Request, code: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        account_type = str(form.get("account_type", "")).strip()
        parent_code = str(form.get("parent_code", "")).strip()
        err = await _validate_account(token, name, account_type, parent_code, own_code=code)
        if err:
            return P(err, cls="error-banner")
        try:
            await api.patch_account(token, code, {
                "name": name,
                "account_type": account_type,
                "parent_code": parent_code or None,
                "is_active": str(form.get("is_active", "true")).strip() == "true",
                "cash_flow_category": str(form.get("cash_flow_category", "")).strip(),
            })
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=chart"})

    @app.get("/settings/accounting/chart/{code}/cash-flow/edit")
    async def cash_flow_field_edit(request: Request, code: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        acct = next((a for a in chart if a.get("code") == code), None)
        if acct is None:
            return P(t("settings_accounting.account_not_found"), cls="cell-error")
        return _cash_flow_edit_cell(acct)

    @app.get("/settings/accounting/chart/{code}/cash-flow/display")
    async def cash_flow_field_display(request: Request, code: str):
        """Return the read-only display cell (used by the Escape cancel handler)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        acct = next((a for a in chart if a.get("code") == code), None)
        if acct is None:
            return P(t("settings_accounting.account_not_found"), cls="cell-error")
        return _cash_flow_display_cell(code, acct.get("cash_flow_category"))

    @app.patch("/settings/accounting/chart/{code}/cash-flow")
    async def cash_flow_field_patch(request: Request, code: str):
        """Save just the cash flow override: a partial update through the same
        API endpoint patch_account_submit forwards to. An empty value clears the
        override back to the derived default, and an invalid one comes back as a
        422 whose message is shown in place rather than swallowed."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", "")).strip()
        try:
            acct = await api.patch_account(token, code, {"cash_flow_category": value})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _cash_flow_display_cell(code, acct.get("cash_flow_category"))
