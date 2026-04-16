# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Bank reconciliation workspace UI routes."""

from __future__ import annotations

from datetime import date as _date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import fmt_money, EMPTY, add_new_option
from ui.config import get_token as _token
from ui.i18n import t, get_lang


# ── Status helpers ────────────────────────────────────────────────────────────

_STATUS_CLS = {
    "matched": "recon-row--matched",
    "created": "recon-row--matched",
    "suggested": "recon-row--suggested",
    "unmatched": "recon-row--unmatched",
    "skipped": "recon-row--skipped",
}

_STATUS_LABEL = {
    "matched": "Matched",
    "created": "Created",
    "suggested": "Suggested",
    "unmatched": "Unmatched",
    "skipped": "Skipped",
}


def _row_status_badge(status: str) -> FT:
    label = _STATUS_LABEL.get(status, status.title())
    return Span(label, cls=f"badge badge--recon-{status}")


# ── Statement line row ────────────────────────────────────────────────────────

def _stmt_line_row(line: dict, session_id: str, currency: str) -> FT:
    lid = line["id"]
    status = line.get("status", "unmatched")
    row_cls = "recon-row " + _STATUS_CLS.get(status, "")
    amount = float(line.get("amount", 0))
    amt_cls = "cell--number " + ("recon-amount--credit" if amount >= 0 else "recon-amount--debit")

    actions = []
    if status in ("unmatched", "suggested"):
        actions += [
            A(t("acct.match"),
                href=f"/accounting/reconcile/{session_id}/lines/{lid}/match-picker",
                hx_get=f"/accounting/reconcile/{session_id}/lines/{lid}/match-picker",
                hx_target=f"#recon-expand-{lid}",
                hx_swap="innerHTML",
                cls="btn btn--xs btn--secondary",
            ),
            A(t("acct.create"),
                hx_get=f"/accounting/reconcile/{session_id}/lines/{lid}/create-form",
                hx_target=f"#recon-expand-{lid}",
                hx_swap="innerHTML",
                cls="btn btn--xs btn--secondary",
            ),
            A(t("inv.split"),
                hx_get=f"/accounting/reconcile/{session_id}/lines/{lid}/split-form",
                hx_target=f"#recon-expand-{lid}",
                hx_swap="innerHTML",
                cls="btn btn--xs btn--secondary",
            ),
        ]
        if status == "suggested":
            actions.append(
                Button(t("btn.confirm"),
                    hx_post=f"/accounting/reconcile/{session_id}/lines/{lid}/match-confirm",
                    hx_target=f"#stmt-line-{lid}",
                    hx_swap="outerHTML",
                    cls="btn btn--xs btn--primary",
                )
            )
        actions.append(
            Button(t("btn.skip"),
                hx_post=f"/accounting/reconcile/{session_id}/lines/{lid}/skip",
                hx_target=f"#stmt-line-{lid}",
                hx_swap="outerHTML",
                cls="btn btn--xs btn--outline",
            )
        )
    elif status in ("matched", "created"):
        actions.append(
            Button(t("btn.undo"),
                hx_post=f"/accounting/reconcile/{session_id}/lines/{lid}/unmatch",
                hx_target=f"#stmt-line-{lid}",
                hx_swap="outerHTML",
                cls="btn btn--xs btn--outline",
            )
        )

    return Div(
        Div(
            Div(
                Span(line.get("line_date", "--"), cls="recon-date"),
                Span(line.get("description", "--"), cls="recon-desc"),
                Span(line.get("reference") or "", cls="recon-ref text-muted"),
                cls="recon-row-left",
            ),
            Div(
                Span(fmt_money(amount, currency), cls=amt_cls),
                _row_status_badge(status),
                Div(*actions, cls="row-actions"),
                cls="recon-row-right",
            ),
            cls="recon-row-main",
        ),
        Div(id=f"recon-expand-{lid}", cls="recon-row-expand"),
        id=f"stmt-line-{lid}",
        cls=row_cls,
    )


# ── Match picker (inline partial) ────────────────────────────────────────────

def _match_picker(session_id: str, line_id: str, unmatched_entries: list[dict], currency: str) -> FT:
    if not unmatched_entries:
        return Div(P(t("acct.no_unmatched_book_entries_found"), cls="empty-state"),
                   cls="recon-inline-form")

    rows = []
    for e in unmatched_entries[:50]:  # cap at 50 for performance
        rows.append(Tr(
            Td(e.get("ts", EMPTY)[:10]),
            Td(e.get("memo", EMPTY)),
            Td(fmt_money(e.get("amount", 0), currency), cls="cell--number"),
            Td(
                Button(t("acct.match"),
                    hx_post=f"/accounting/reconcile/{session_id}/lines/{line_id}/match-confirm",
                    hx_vals=f'{{"je_id": "{e["je_id"]}"}}',
                    hx_target=f"#stmt-line-{line_id}",
                    hx_swap="outerHTML",
                    cls="btn btn--xs btn--primary",
                )
            ),
        ))

    return Div(
        H4(t("page.select_book_entry_to_match")),
        Input(
            type="text",
            placeholder="Filter...",
            oninput="this.closest('.recon-inline-form').querySelectorAll('tr').forEach(r=>{"
                    "r.style.display=this.value&&!r.textContent.toLowerCase().includes(this.value.toLowerCase())?'none':'';});",
            cls="form-input mb-sm",
        ),
        Table(
            Thead(Tr(Th(t("th.date")), Th(t("th.memo")), Th(t("label.amount")), Th(""))),
            Tbody(*rows),
            cls="data-table data-table--compact",
        ),
        Button(t("btn.cancel"), onclick=f"document.getElementById('recon-expand-{line_id}').innerHTML=''",
               cls="btn btn--xs btn--outline mt-sm"),
        cls="recon-inline-form",
    )


# ── Create expense form (inline partial) ─────────────────────────────────────

def _create_form(session_id: str, line_id: str, line: dict, chart: list[dict], currency: str) -> FT:
    amount = abs(float(line.get("amount", 0)))
    account_options = [
        Option(f"{a['code']} {a['name']}", value=a["code"])
        for a in chart
        if a.get("account_type") in ("expense", "cogs", "asset", "liability")
    ]
    _acct_opt, _acct_js = add_new_option("+ Add new account", "/settings/accounting?tab=chart")
    return Div(
        H4(t("page.create_journal_entry")),
        Form(
            Div(
                Label(t("th.account"), cls="form-label"),
                Select(*account_options, _acct_opt, name="account_code",
                       cls="form-input cell-input--select", onchange=_acct_js),
                cls="form-field",
            ),
            Div(
                Label(t("th.memo"), cls="form-label"),
                Input(type="text", name="memo", value=line.get("description", ""),
                      cls="form-input"),
                cls="form-field",
            ),
            Div(
                Label(t("label.amount"), cls="form-label"),
                Input(type="number", name="amount", value=str(amount),
                      step="0.01", cls="form-input"),
                cls="form-field",
            ),
            Div(
                Button(t("btn.create_match"), type="submit", cls="btn btn--primary btn--sm"),
                Button(t("btn.cancel"), type="button",
                       onclick=f"document.getElementById('recon-expand-{line_id}').innerHTML=''",
                       cls="btn btn--outline btn--sm ml-sm"),
                cls="form-actions",
            ),
            hx_post=f"/accounting/reconcile/{session_id}/lines/{line_id}/create-confirm",
            hx_target=f"#stmt-line-{line_id}",
            hx_swap="outerHTML",
        ),
        cls="recon-inline-form",
    )


# ── Split form (inline partial) ───────────────────────────────────────────────

def _split_form(session_id: str, line_id: str, line: dict, chart: list[dict], currency: str) -> FT:
    amount = abs(float(line.get("amount", 0)))
    account_options = [
        Option(f"{a['code']} {a['name']}", value=a["code"])
        for a in chart
        if a.get("account_type") in ("expense", "cogs", "asset", "liability", "revenue")
    ]
    _acct_opt, _acct_js = add_new_option("+ Add new account", "/settings/accounting?tab=chart")
    return Div(
        H4(t("page.split_transaction")),
        Form(
            Div(
                P(f"Total: {fmt_money(float(line.get('amount', 0)), currency)}", cls="text-muted"),
                id="split-entries",
                *[
                    Div(
                        Select(*account_options, _acct_opt, name="account_code_0",
                               cls="form-input cell-input--select w-auto", onchange=_acct_js),
                        Input(type="number", name="amount_0", placeholder="Amount",
                              step="0.01", cls="form-input max-w-sm"),
                        Input(type="text", name="memo_0", placeholder="Memo", cls="form-input"),
                        cls="split-entry-row flex-row gap-sm mb-sm",
                    )
                ],
            ),
            Div(
                Button(t("btn.split_match"), type="submit", cls="btn btn--primary btn--sm"),
                Button(t("btn.cancel"), type="button",
                       onclick=f"document.getElementById('recon-expand-{line_id}').innerHTML=''",
                       cls="btn btn--outline btn--sm ml-sm"),
                cls="form-actions",
            ),
            hx_post=f"/accounting/reconcile/{session_id}/lines/{line_id}/split-confirm",
            hx_target=f"#stmt-line-{line_id}",
            hx_swap="outerHTML",
        ),
        cls="recon-inline-form",
    )


# ── Progress bar ──────────────────────────────────────────────────────────────

def _recon_progress(total: int, matched: int, created: int, skipped: int) -> FT:
    if total == 0:
        pct = 0
    else:
        pct = int((matched + created + skipped) / total * 100)
    return Div(
        Div(
            Span(f"{matched + created}/{total} resolved", cls="recon-progress-label"),
            Span(f"({skipped} skipped)", cls="recon-progress-label text-muted"),
        ),
        Div(
            Div(cls="recon-progress-fill", style=f"width:{pct}%;"),
            cls="recon-progress",
        ),
    )


# ── Main workspace ────────────────────────────────────────────────────────────

def _workspace_view(
    session_id: str,
    recon: dict,
    bank: dict,
    lines: list[dict],
    book_entries: list[dict],
    currency: str,
) -> FT:
    total = len(lines)
    matched = sum(1 for l in lines if l["status"] in ("matched", "created"))
    suggested = sum(1 for l in lines if l["status"] == "suggested")
    skipped = sum(1 for l in lines if l["status"] == "skipped")
    unmatched = total - matched - suggested - skipped

    stmt_bal = float(recon.get("statement_balance", 0))
    difference = recon.get("difference", 0)
    tol = float(recon.get("tolerance", 1.0))
    diff_cls = "recon-diff--ok" if abs(float(difference)) < 0.01 else (
        "recon-diff--warn" if abs(float(difference)) <= tol else "recon-diff--bad"
    )

    # Build sets for unmatched book entries
    matched_je_ids = {l["matched_je_id"] for l in lines if l.get("matched_je_id")}
    unmatched_entries = [e for e in book_entries if e["je_id"] not in matched_je_ids]

    bank_name = bank.get("bank_name", "")
    acc_num = bank.get("account_number", "")

    header = Div(
        Div(
            H2(f"Reconciliation: {bank_name} {acc_num}", cls="recon-title"),
            Span(f"Statement date: {recon.get('statement_date', '--')}", cls="text-muted"),
        ),
        Div(
            Span(f"Statement: {fmt_money(stmt_bal, currency)}", cls="val-chip"),
            Span(f"Diff: {fmt_money(float(difference), currency)}", cls=f"val-chip {diff_cls}"),
        ),
        cls="recon-header",
    )

    toolbar = Div(
        Button(
            "Auto-Match All",
            hx_post=f"/accounting/reconcile/{session_id}/auto-match",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
            cls="btn btn--secondary btn--sm",
        ),
        Button(
            f"Confirm Suggestions ({suggested})",
            hx_post=f"/accounting/reconcile/{session_id}/bulk-confirm",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
            cls="btn btn--secondary btn--sm",
            disabled="" if suggested == 0 else None,
        ),
        *(
            [Button(t("btn.complete"),
                hx_post=f"/accounting/reconcile/{session_id}/complete",
                hx_target="body",
                hx_swap="outerHTML",
                cls="btn btn--primary btn--sm",
            )]
            if abs(float(difference)) < 0.01 else
            [Button(t("btn.write_off_diff"),
                hx_post=f"/accounting/reconcile/{session_id}/write-off",
                hx_target="#recon-workspace",
                hx_swap="outerHTML",
                cls="btn btn--outline btn--sm",
            )]
            if abs(float(difference)) <= tol else []
        ),
        cls="recon-toolbar",
    )

    bank_panel = Div(
        H3(t("page.bank_statement"), cls="recon-panel-title"),
        _recon_progress(total, matched, created=sum(1 for l in lines if l["status"] == "created"),
                        skipped=skipped),
        Div(
            *[_stmt_line_row(l, session_id, currency) for l in lines],
            id="stmt-lines-panel",
            cls="recon-panel-body",
        ) if lines else Div(
            P(t("acct.no_statement_lines_imported_yet"), cls="empty-state"),
            Form(
                Div(
                    Label(t("label.upload_csv"), cls="form-label"),
                    Input(type="file", name="csv_file", accept=".csv", cls="form-input", required=True),
                    cls="form-field",
                ),
                Button(t("doc.import_csv"), type="submit", cls="btn btn--primary btn--sm"),
                hx_post=f"/accounting/reconcile/{session_id}/import",
                hx_target="#recon-workspace",
                hx_swap="outerHTML",
                hx_encoding="multipart/form-data",
            ),
        ),
        cls="recon-panel recon-panel--bank",
    )

    book_panel = Div(
        H3(t("page.book_entries"), cls="recon-panel-title"),
        Div(
            *[
                Div(
                    Div(
                        Span(e.get("ts", EMPTY)[:10], cls="recon-date"),
                        Span(e.get("memo", EMPTY), cls="recon-desc"),
                        cls="recon-row-left",
                    ),
                    Div(
                        Span(fmt_money(e.get("amount", 0), currency), cls="cell--number"),
                        cls="recon-row-right",
                    ),
                    cls="recon-row recon-row--unmatched",
                )
                for e in unmatched_entries
            ],
            cls="recon-panel-body",
        ) if unmatched_entries else Div(
            P(t("acct.all_book_entries_matched"), cls="empty-state"),
            cls="recon-panel-body",
        ),
        cls="recon-panel recon-panel--book",
    )

    return Div(
        header,
        toolbar,
        Div(
            Span(f"Unmatched: {unmatched}", cls="recon-stat"),
            Span(f"Suggested: {suggested}", cls="recon-stat recon-stat--suggested"),
            Span(f"Matched: {matched}", cls="recon-stat recon-stat--matched"),
            Span(f"Skipped: {skipped}", cls="recon-stat recon-stat--skipped"),
            cls="recon-stats-bar",
        ),
        Div(bank_panel, book_panel, cls="recon-panels"),
        id="recon-workspace",
        cls="recon-workspace",
    )


# ── Setup routes ─────────────────────────────────────────────────────────────

def setup_routes(app):

    @app.get("/accounting/reconcile/start")
    async def reconcile_start(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
            banks_data = await api.get_bank_accounts(token)
            banks = banks_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            banks = []
            currency = ""

        bank_options = [
            Option(f"{b['bank_name']} {b.get('account_number', '')}", value=b["id"])
            for b in banks if b.get("is_active", True)
        ]
        _bank_opt, _bank_js = add_new_option("+ Add new bank account", "/settings/accounting?tab=bank-accounts")

        if bank_options:
            bank_select_or_msg = Select(
                *bank_options, _bank_opt,
                name="bank_account_id",
                cls="form-input cell-input--select",
                required=True,
                onchange=_bank_js,
            )
        else:
            bank_select_or_msg = P(t("acct.no_bank_accounts_configured"),
                A(t("acct.add_bank_account"), href="/settings/accounting?tab=bank-accounts"),
            )

        form = Div(
            Form(
                Div(
                    Label(t("label.bank_account"), cls="form-label"),
                    bank_select_or_msg,
                    cls="form-field",
                ),
                Div(
                    Label(t("label.statement_date"), cls="form-label"),
                    Input(type="date", name="statement_date",
                          value=_date.today().isoformat(),
                          cls="form-input", required=True),
                    cls="form-field",
                ),
                Div(
                    Label(t("label.statement_closing_balance"), cls="form-label"),
                    Input(type="number", name="statement_balance", step="0.01",
                          placeholder="0.00", cls="form-input", required=True),
                    cls="form-field",
                ),
                Div(
                    Button(t("btn.start_reconciliation"), type="submit", cls="btn btn--primary"),
                    cls="form-actions",
                ),
                action="/accounting/reconcile/start",
                method="post",
            ),
            cls="settings-card",
        )

        return base_shell(
            page_header("Start Reconciliation",
                        A(t("btn.back_to_settings"), href="/accounting?tab=bank-accounts", cls="btn btn--secondary")),
            form,
            title="Start Reconciliation - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/start")
    async def reconcile_start_submit(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        bank_id = str(form.get("bank_account_id", "")).strip()
        stmt_date = str(form.get("statement_date", "")).strip()
        stmt_bal_raw = str(form.get("statement_balance", "0")).strip()
        if not bank_id or not stmt_date or not stmt_bal_raw:
            return RedirectResponse("/accounting/reconcile/start", status_code=302)
        try:
            stmt_bal = float(stmt_bal_raw)
            recon = await api.start_reconciliation(token, {
                "bank_account_id": bank_id,
                "statement_date": stmt_date,
                "statement_balance": stmt_bal,
            })
            session_id = recon.get("id") or recon.get("session_id")
            return RedirectResponse(f"/accounting/reconcile/{session_id}", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/accounting/reconcile/start", status_code=302)

    @app.get("/accounting/reconcile/{session_id}")
    async def reconcile_workspace(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
            recon = await api.get_reconciliation(token, session_id)
            lines_data = await api.get_statement_lines(token, session_id)
            lines = lines_data.get("items", [])
            bank = recon.get("bank_account", {})
            book_entries = recon.get("unreconciled_entries", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/accounting?tab=bank-accounts", status_code=302)

        workspace = _workspace_view(session_id, recon, bank, lines, book_entries, currency)
        return base_shell(
            workspace,
            title="Reconciliation Workspace - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/{session_id}/import")
    async def reconcile_import_csv(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_file = form.get("csv_file")
        if not csv_file or not hasattr(csv_file, "read"):
            return RedirectResponse(f"/accounting/reconcile/{session_id}", status_code=302)
        try:
            content = await csv_file.read()
            result = await api.import_recon_csv(token, session_id, content, csv_file.filename or "upload.csv")
            if result.get("needs_mapping"):
                return RedirectResponse(
                    f"/accounting/reconcile/{session_id}/column-mapper", status_code=302
                )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
        return RedirectResponse(f"/accounting/reconcile/{session_id}", status_code=302)

    @app.get("/accounting/reconcile/{session_id}/column-mapper")
    async def column_mapper_page(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        # Headers stored in query params from a prior import attempt
        headers_raw = request.query_params.get("headers", "")
        headers = [h.strip() for h in headers_raw.split(",") if h.strip()]

        _CANONICAL = ["date", "description", "amount", "debit", "credit", "balance", "reference", "ignore"]
        if not headers:
            return RedirectResponse(f"/accounting/reconcile/{session_id}", status_code=302)

        rows = []
        for h in headers:
            rows.append(Tr(
                Td(h),
                Td(Select(
                    *[Option(f, value=f) for f in _CANONICAL],
                    name=f"map_{h}",
                    cls="form-input cell-input--select",
                )),
            ))

        return base_shell(
            page_header("Map CSV Columns"),
            Div(
                P(t("acct.we_couldnt_autodetect_your_csv_columns_please_map"), cls="text-muted"),
                Form(
                    Table(
                        Thead(Tr(Th(t("th.csv_column")), Th(t("th.maps_to")))),
                        Tbody(*rows),
                        cls="data-table",
                    ),
                    Button(t("btn.confirm_mapping"), type="submit", cls="btn btn--primary mt-md"),
                    hx_post=f"/accounting/reconcile/{session_id}/confirm-import",
                    hx_target="body",
                    hx_swap="outerHTML",
                ),
                cls="settings-card",
            ),
            title="Map CSV Columns - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/{session_id}/confirm-import")
    async def confirm_import(request: Request, session_id: str):
        """Re-import with explicit column map from user."""
        import json as _json
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        column_map = {
            canonical: header
            for header, canonical in (
                (k[4:], v) for k, v in form.items() if k.startswith("map_")
            )
            if canonical != "ignore"
        }
        # We need the original CSV — stored in session or re-uploaded; redirect for now
        return RedirectResponse(f"/accounting/reconcile/{session_id}", status_code=302)

    @app.get("/accounting/reconcile/{session_id}/lines/{line_id}/match-picker")
    async def match_picker_partial(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
            recon = await api.get_reconciliation(token, session_id)
            lines_data = await api.get_statement_lines(token, session_id)
            lines = lines_data.get("items", [])
            matched_je_ids = {l["matched_je_id"] for l in lines if l.get("matched_je_id")}
            book_entries = [e for e in recon.get("unreconciled_entries", [])
                            if e["je_id"] not in matched_je_ids]
        except APIError:
            return P(t("acct.error_loading_entries"), cls="error-banner")
        return _match_picker(session_id, line_id, book_entries, currency)

    @app.get("/accounting/reconcile/{session_id}/lines/{line_id}/create-form")
    async def create_form_partial(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
            lines_data = await api.get_statement_lines(token, session_id)
            line = next((l for l in lines_data.get("items", []) if l["id"] == line_id), None)
            if not line:
                return P(t("acct.line_not_found"), cls="error-banner")
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError:
            return P(t("acct.error_loading_data"), cls="error-banner")
        return _create_form(session_id, line_id, line, chart, currency)

    @app.get("/accounting/reconcile/{session_id}/lines/{line_id}/split-form")
    async def split_form_partial(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
            lines_data = await api.get_statement_lines(token, session_id)
            line = next((l for l in lines_data.get("items", []) if l["id"] == line_id), None)
            if not line:
                return P(t("acct.line_not_found"), cls="error-banner")
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError:
            return P(t("acct.error_loading_data"), cls="error-banner")
        return _split_form(session_id, line_id, line, chart, currency)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/match-confirm")
    async def match_confirm(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
        except APIError:
            currency = ""
        form = await request.form()
        je_id = str(form.get("je_id", "")).strip()
        if not je_id:
            return P(t("acct.missing_jeid"), cls="error-banner")
        try:
            line = await api.match_recon_line(token, session_id, line_id, je_id)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _stmt_line_row(line, session_id, currency)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/create-confirm")
    async def create_confirm(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
        except APIError:
            currency = ""
        form = await request.form()
        account_code = str(form.get("account_code", "")).strip()
        memo = str(form.get("memo", "")).strip()
        amount_raw = str(form.get("amount", "")).strip()
        if not account_code:
            return P(t("acct.account_code_required"), cls="error-banner")
        data = {"account_code": account_code, "memo": memo}
        if amount_raw:
            try:
                data["amount"] = float(amount_raw)
            except ValueError:
                pass
        try:
            line = await api.create_recon_expense(token, session_id, line_id, data)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _stmt_line_row(line, session_id, currency)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/split-confirm")
    async def split_confirm(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
        except APIError:
            currency = ""
        form = await request.form()
        # Parse split-N fields
        splits = []
        i = 0
        while f"account_code_{i}" in form:
            code = str(form.get(f"account_code_{i}", "")).strip()
            amt_raw = str(form.get(f"amount_{i}", "")).strip()
            memo = str(form.get(f"memo_{i}", "")).strip()
            if code and amt_raw:
                try:
                    splits.append({"account_code": code, "amount": float(amt_raw), "memo": memo})
                except ValueError:
                    pass
            i += 1
        if not splits:
            return P(t("acct.at_least_one_split_entry_required"), cls="error-banner")
        try:
            line = await api.split_recon_line(token, session_id, line_id, splits)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _stmt_line_row(line, session_id, currency)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/skip")
    async def skip_line(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
        except APIError:
            currency = ""
        try:
            line = await api.skip_recon_line(token, session_id, line_id)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _stmt_line_row(line, session_id, currency)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/unmatch")
    async def unmatch_line(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "")
        except APIError:
            currency = ""
        try:
            line = await api.unmatch_recon_line(token, session_id, line_id)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _stmt_line_row(line, session_id, currency)

    @app.post("/accounting/reconcile/{session_id}/auto-match")
    async def trigger_auto_match(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            await api.auto_match_recon(token, session_id)
            company = await api.get_company(token)
            currency = company.get("currency", "")
            recon = await api.get_reconciliation(token, session_id)
            lines_data = await api.get_statement_lines(token, session_id)
            lines = lines_data.get("items", [])
            bank = recon.get("bank_account", {})
            book_entries = recon.get("unreconciled_entries", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return P(str(e.detail), cls="error-banner")
        return _workspace_view(session_id, recon, bank, lines, book_entries, currency)

    @app.post("/accounting/reconcile/{session_id}/bulk-confirm")
    async def trigger_bulk_confirm(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            await api.bulk_confirm_recon(token, session_id)
            company = await api.get_company(token)
            currency = company.get("currency", "")
            recon = await api.get_reconciliation(token, session_id)
            lines_data = await api.get_statement_lines(token, session_id)
            lines = lines_data.get("items", [])
            bank = recon.get("bank_account", {})
            book_entries = recon.get("unreconciled_entries", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return P(str(e.detail), cls="error-banner")
        return _workspace_view(session_id, recon, bank, lines, book_entries, currency)

    @app.post("/accounting/reconcile/{session_id}/complete")
    async def complete_recon(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            result = await api.complete_reconciliation(token, session_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return P(str(e.detail), cls="error-banner")
        return base_shell(
            page_header("Reconciliation Complete ✓"),
            Div(
                P(f"Reconciliation completed for {result.get('statement_date', '--')}.",
                  cls="success-banner"),
                A(t("btn._back_to_accounting"), href="/accounting?tab=bank-accounts",
                  cls="btn btn--primary"),
                cls="settings-card",
            ),
            title="Reconciliation Complete - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/{session_id}/write-off")
    async def trigger_write_off(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        try:
            await api.write_off_recon(token, session_id, {})
            company = await api.get_company(token)
            currency = company.get("currency", "")
            recon = await api.get_reconciliation(token, session_id)
            lines_data = await api.get_statement_lines(token, session_id)
            lines = lines_data.get("items", [])
            bank = recon.get("bank_account", {})
            book_entries = recon.get("unreconciled_entries", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return P(str(e.detail), cls="error-banner")
        return _workspace_view(session_id, recon, bank, lines, book_entries, currency)
