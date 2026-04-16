# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → Accounting: Bank Accounts."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import COOKIE_NAME
from ui.components.table import EMPTY, add_new_option

from ui.routes.settings import _token, _check_role
from ui.routes.settings_general import _section_breadcrumb
from ui.i18n import t, get_lang


_BANK_TYPES = [
    ("checking", "Checking"),
    ("savings", "Savings"),
    ("credit_card", "Credit Card"),
]



def _accounting_settings_tabs(active: str) -> FT:
    tabs = [("bank-accounts", "Bank Accounts"), ("chart", "Chart of Accounts"), ("rules", "Reconciliation Rules"), ("period-lock", "Period Lock")]
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
            Span(f"{b.get('bank_type', '').replace('_', ' ').title()} · {b.get('account_number', '')} · {b.get('chart_account_code', '')}", cls="bank-name-label"),
            cls="bank-info",
        ),
        Div(
            Span(f"{currency} {balance:,.2f}", cls=f"balance {bal_cls}"),
            Div(
                A(t("btn.edit"), href=f"/settings/accounting/bank-accounts/{b['id']}/edit",
                  cls="btn btn--secondary btn--xs"),
                A(t("acct.reconcile"), href="/accounting/reconcile/start",
                  cls="btn btn--primary btn--xs"),
                Button(
                    "Archive" if b.get("is_active") else "Restore",
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
            "Lock all transactions before a certain date. "
            "Once locked, no journal entries, documents, or inventory adjustments can be created "
            "or modified for dates on or before the lock date.",
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
            [P(f"Currently locked through {lock_date}. Last updated: {set_at[:10] if set_at else 'unknown'}.",
               cls="text-muted mt-md")]
            if lock_date else []
        ),
        Hr(cls="section-divider mt-lg mb-lg"),
        H3(t("page.close_fiscal_year"), cls="section-title"),
        P(
            "Close a fiscal year to zero all revenue and expense accounts and transfer net income "
            "to Retained Earnings. This also locks the period through the year-end date.",
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
                       hx_confirm="This will create a closing journal entry and lock the period. Continue?"),
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
    _bank_opt, _bank_js = add_new_option("+ Add new bank account", "/settings/accounting?tab=bank-accounts")
    _MATCH_TYPES = [("contains", "Contains"), ("exact", "Exact"), ("starts_with", "Starts with")]

    rows = []
    for r in rules:
        rows.append(Tr(
            Td(r.get("match_pattern", EMPTY)),
            Td(r.get("match_type", EMPTY)),
            Td(r.get("target_account_code", EMPTY)),
            Td(r.get("default_memo") or EMPTY),
            Td(Span("Active" if r.get("is_active") else "Inactive",
                    cls="badge badge--active" if r.get("is_active") else "badge badge--inactive")),
            Td(str(r.get("times_applied", 0))),
            Td(
                Button(t("btn.delete"),
                       hx_delete=f"/settings/accounting/rules/{r['id']}",
                       hx_target="#rules-list",
                       hx_swap="outerHTML",
                       hx_confirm="Delete this rule?",
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
                Input(type="text", name="match_pattern", placeholder="e.g. ACME Corp",
                      cls="form-input", required=True),
                cls="form-field",
            ),
            Div(
                Label(t("th.match_type"), cls="form-label"),
                Select(*[Option(label, value=v) for v, label in _MATCH_TYPES],
                       name="match_type", cls="form-input cell-input--select"),
                cls="form-field",
            ),
            Div(
                Label(t("label.target_account_code"), cls="form-label"),
                Input(type="text", name="target_account_code", placeholder="e.g. 6950",
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

    by_type: dict[str, list] = {}
    for a in chart:
        atype = a.get("account_type", "other")
        by_type.setdefault(atype, []).append(a)

    sections: list[FT] = []
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


def _chart_tab(chart: list[dict]) -> FT:
    return Div(
        Div(
            A(t("acct.add_account"), href="/accounting/new", cls="btn btn--primary"),
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


def setup_routes(app):

    @app.get("/settings/accounting")
    async def settings_accounting_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
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
                chart = chart_data.get("items", [])
            except Exception:
                chart = []
            content = _chart_tab(chart)
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

        return base_shell(
            _section_breadcrumb("Accounting"),
            page_header("Finance Settings"),
            _accounting_settings_tabs(tab),
            content,
            title="Finance Settings - Celerp",
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

        return base_shell(
            _section_breadcrumb("Accounting"),
            page_header(
                "Add Bank Account",
                A(t("btn.back_to_settings"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
            ),
            Div(
                Form(
                    Div(
                        Table(
                            Tr(
                                Td(t("acct.bank_name"), cls="detail-label"),
                                Td(Input(type="text", name="bank_name", placeholder="e.g. Kasikorn Bank",
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_number"), cls="detail-label"),
                                Td(Input(type="text", name="account_number", placeholder="e.g. ****1234",
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_type"), cls="detail-label"),
                                Td(Select(
                                    *[Option(label, value=v) for v, label in _BANK_TYPES],
                                    name="bank_type", cls="cell-input cell-input--select",
                                )),
                            ),
                            Tr(
                                Td(t("th.currency"), cls="detail-label"),
                                Td(Input(type="text", name="currency", value=currency,
                                         maxlength="8", cls="cell-input", required=True)),
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
            title="Add Bank Account - Celerp",
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

        return base_shell(
            _section_breadcrumb("Accounting"),
            page_header(
                f"Edit {b.get('bank_name', 'Bank Account')}",
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
                                    *[Option(label, value=v, selected=(v == b.get("bank_type")))
                                      for v, label in _BANK_TYPES],
                                    name="bank_type", cls="cell-input cell-input--select",
                                )),
                            ),
                            Tr(
                                Td(t("th.currency"), cls="detail-label"),
                                Td(Input(type="text", name="currency", value=b.get("currency", ""),
                                         maxlength="8", cls="cell-input", required=True)),
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
            title=f"Edit Bank Account - Celerp",
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
        msg = f"Fiscal year closed through {fiscal_year_end}. Net income of {net:,.2f} transferred to Retained Earnings. Period locked."
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
