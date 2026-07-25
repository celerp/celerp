# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The accounting workspace: the journal, and posting entries into it.

Reading the books happens under /reports; this module is the side you act on.
"""

from __future__ import annotations

import csv
import io
import math as _math
import re
import json as _json
import uuid
import asyncio
from datetime import date as _date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, StreamingResponse, PlainTextResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.currency import CURRENCIES
from ui.components.report_kit import (
    action_bar, csv_row, date_params as _date_params, fname_date as _fname_date,
    href as _href, period_subtitle as _period_subtitle, plain_error_response as _plain_error_response,
    print_shell as _print_shell, totals_chips as _totals_chips,
)
from ui.components.table import empty_state_cta, fmt_money, searchable_select
from ui.config import get_token as _token, get_role as _get_role
from celerp.services.permissions import role_has_permission
from ui.i18n import t, get_lang
from ui.routes.documents import _action_error
from ui.routes.financial_reports import MOVED_TABS, REPORTS, ledger_path
from ui.routes.reports import _date_filter_bar, _get_fiscal, _parse_dates
from celerp.services.money import round_money, to_decimal


def _moved_reports_notice() -> FT:
    """Points at the reports that used to be tabs here.

    Naming each one and linking it directly means someone who came looking for the
    trial balance reaches it from where it used to be, rather than being sent to an
    index to hunt. Remove once the move is a release or two old; leaving it becomes
    the duplicate navigation this move existed to end.
    """
    return Div(
        Span(t("acct.reports_moved_notice")),
        Span(" "),
        *[
            A(t(REPORTS[key][3]), href=REPORTS[key][0], cls="drilldown-link")
            for key in MOVED_TABS.values()
        ],
        A(t("nav.reports"), href="/reports", cls="drilldown-link"),
        cls="info-banner mb-md",
    )


def _moved_to(path: str):
    """A handler that hands an old report URL on to its new home under /reports.

    Every query parameter comes across untouched: the pages, print views and CSV
    exports read the same parameter names on both sides of the move, so a saved
    link lands on the figures it was saved for rather than on an unfiltered
    report. Values are re-encoded on the way out, keeping raw request text out of
    the Location header.
    """
    async def moved(request: Request):
        return RedirectResponse(_href(path, request.query_params.multi_items()),
                                status_code=302)
    return moved


def setup_routes(app):

    # The reports left this section, so their print views and CSV exports left
    # with them. Bookmarks, emailed links and saved downloads predate the move,
    # so every old URL still answers and carries its filters over. Destinations
    # are read from REPORTS, keyed by the tab name the old URL used, so a
    # report's new home is written down once and these follow it.
    for _tab, _key in MOVED_TABS.items():
        _page, _print_path, _csv_path, _title = REPORTS[_key]
        app.get(f"/accounting/print/{_tab}",
                name=f"moved_print_{_key}")(_moved_to(_print_path))
        app.get(f"/accounting/export/{_tab}/csv",
                name=f"moved_csv_{_key}")(_moved_to(_csv_path))

    # Two shortcut URLs that predate even the tabbed layout, and are still linked
    # from outside the app.
    app.get("/accounting/pnl", name="moved_page_pnl")(_moved_to(REPORTS["pnl"][0]))
    app.get("/accounting/balance-sheet",
            name="moved_page_balance_sheet")(_moved_to(REPORTS["balance-sheet"][0]))

    @app.get("/accounting/ledger/{account_code}")
    async def account_ledger_redirect(request: Request, account_code: str):
        """The per-account drilldown moved to /reports with the reports it hangs off."""
        return RedirectResponse(
            _href(ledger_path(account_code), request.query_params.multi_items()),
            status_code=302)

    @app.get("/accounting")
    async def accounting_page(request: Request):
        """The journal. Report tabs that used to live here now redirect to /reports."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        tab = request.query_params.get("tab", "")
        if tab in MOVED_TABS:
            # Bookmarks and emailed links predate the move; carry their filters
            # over rather than dropping the reader on an unfiltered report. Only
            # "tab" is left behind: it named this page, not the one being opened.
            # Everything else comes across, so a preset link keeps its preset and
            # a statement link keeps the party it was written for.
            carried = [(k, v) for k, v in request.query_params.multi_items()
                       if k != "tab"]
            return RedirectResponse(
                _href(REPORTS[MOVED_TABS[tab]][0], carried), status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            fy = await _get_fiscal(token)
            d_from, d_to, preset = _parse_dates(request, fy)
            params = _date_params(d_from, d_to)
            data = await api.get_journal(token, params)
            content = Div(
                _moved_reports_notice(),
                _date_filter_bar("/accounting", d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company"),
                _journal_totals(data, currency),
                # Toolbar over the table, following the document-list pattern:
                # creative actions left, export/print right.
                Div(
                    A(t("btn.new_entry"),
                      href=_href("/accounting/journal/new",
                                 _date_params(d_from or "", d_to or "")),
                      cls="btn btn--primary"),
                    action_bar("/accounting/print/journal", "/accounting/export/journal/csv",
                               params),
                    cls="flex-row flex-between mt-md mb-md",
                ),
                _journal_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 403:
                content = Div(t("acct.not_authorized"), cls="error-banner")
            else:
                content = Div(f"{t('acct.error_loading_data')}: {e.detail}", cls="error-banner")

        return await base_shell(
            page_header(t("page.accounting", get_lang(request))),
            content,
            title="Accounting - Celerp",
            nav_active="accounting",
            request=request,
        )

    async def _render_journal_form(request: Request, token: str, ts: str, memo: str,
                                   lines: list[dict], idem_token: str, error: str | None,
                                   date_from: str = "", date_to: str = "",
                                   currency: str = "", rate: str = ""):
        try:
            _settings = (await api.get_company(token)).get("settings") or {}
        except APIError:
            _settings = {}
        if not role_has_permission(_settings, _get_role(request), "manage_accounting"):
            return await base_shell(
                page_header(t("acct.new_journal_entry", get_lang(request))),
                Div(t("acct.not_authorized"), cls="error-banner"),
                title="Accounting - Celerp",
                nav_active="accounting",
                request=request,
            )
        base_currency = ""
        try:
            accounts = (await api.get_chart(token)).get("items", [])
            base_currency = (await api.get_company(token)).get("currency") or ""
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            accounts = []
            if error is None:
                error = (t("acct.not_authorized") if e.status == 403
                         else f"{t('acct.error_loading_data')}: {e.detail}")
        return await base_shell(
            page_header(t("acct.new_journal_entry", get_lang(request))),
            _journal_entry_form(accounts, ts, memo, lines, idem_token, error,
                                date_from=date_from, date_to=date_to,
                                currency=currency, rate=rate,
                                base_currency=base_currency),
            title="Accounting - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.get("/accounting/journal/new")
    async def journal_new_form(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await _render_journal_form(
            request, token,
            ts=_date.today().isoformat(), memo="", lines=[],
            idem_token=str(uuid.uuid4()), error=None,
            date_from=request.query_params.get("date_from", ""),
            date_to=request.query_params.get("date_to", ""),
        )

    @app.post("/accounting/journal/new")
    async def journal_new_submit(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        ts = str(form.get("ts", "")).strip()
        memo = str(form.get("memo", "")).strip()
        idem_token = str(form.get("idempotency_token", "")).strip() or str(uuid.uuid4())
        d_from = str(form.get("date_from", "")).strip()
        d_to = str(form.get("date_to", "")).strip()

        # Collect the line grid: inputs are named account_N / debit_N / credit_N per row.
        idxs = sorted(
            int(k[len("account_"):]) for k in form.keys()
            if k.startswith("account_") and k[len("account_"):].isdigit()
        )
        lines: list[dict] = []
        for i in idxs:
            account = str(form.get(f"account_{i}") or "").strip()
            debit = str(form.get(f"debit_{i}") or "").strip()
            credit = str(form.get(f"credit_{i}") or "").strip()
            if not (account or debit or credit):
                continue  # an untouched blank row carries no intent
            lines.append({"account": account, "debit": debit, "credit": credit})

        error: str | None = None
        entries: list[dict] = []
        try:
            entries = [
                {"account": l["account"], "debit": float(l["debit"] or 0), "credit": float(l["credit"] or 0)}
                for l in lines
            ]
            if any(not (_math.isfinite(e["debit"]) and _math.isfinite(e["credit"])) for e in entries):
                error = t("acct.err_amounts_numeric")
        except ValueError:
            error = t("acct.err_amounts_numeric")

        # An untouched reveal posts an ordinary entry: no currency chosen means
        # no fx key at all, so the request is byte-identical to today's.
        fx_payload: dict | None = None
        currency = str(form.get("currency") or "").strip().upper()
        if error is None and currency:
            valid_codes = {c for c, _ in CURRENCIES}
            if currency not in valid_codes:
                error = t("acct.err_currency_unknown")
            else:
                try:
                    rate = float(str(form.get("rate") or "").strip() or 0)
                except ValueError:
                    rate = 0.0
                if not _math.isfinite(rate) or rate <= 0:
                    error = t("acct.err_rate_positive")
                else:
                    fx_payload = {"currency": currency, "rate": rate}

        if error is None:
            try:
                await api.create_journal_entry(token, {
                    "ts": ts,
                    "memo": memo,
                    "entries": entries,
                    "idempotency_token": idem_token,
                    **({"fx": fx_payload} if fx_payload else {}),
                })
                # Land on the journal with the entry guaranteed visible: keep the
                # filter the user came from, widened to include the posted date.
                if ts:
                    if d_from and ts < d_from:
                        d_from = ts
                    if d_to and ts > d_to:
                        d_to = ts
                return RedirectResponse(
                    _href("/accounting", {"tab": "journal", "from": d_from, "to": d_to}),
                    status_code=303)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                if e.status == 409:
                    # Same token, different payload: the original already posted.
                    # A fresh token lets a deliberately corrected resubmit succeed.
                    idem_token = str(uuid.uuid4())
                    error = t("acct.err_already_posted")
                else:
                    error = t("acct.not_authorized") if e.status == 403 else str(e.detail)

        # Validation failed: re-render the form with the message and the values intact.
        return await _render_journal_form(request, token, ts=ts, memo=memo, lines=lines,
                                          idem_token=idem_token, error=error,
                                          date_from=d_from, date_to=d_to,
                                          currency=currency,
                                          rate=str(form.get("rate") or "").strip())

    @app.post("/accounting/journal/{je_id}/void")
    async def journal_void(request: Request, je_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.void_journal_entry(token, je_id, reason)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _action_error(t("acct.not_authorized") if e.status == 403 else str(e.detail))
        target = _href("/accounting", {"tab": "journal", "from": str(form.get("date_from") or ""),
                                       "to": str(form.get("date_to") or "")})
        return _R("", status_code=204, headers={"HX-Redirect": target})

    # ── Print (PDF) routes ─────────────────────────────────────────────────

    @app.get("/accounting/print/journal")
    async def journal_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_journal(token, params)
        except APIError as e:
            return _plain_error_response(e)

        body = Div(
            _journal_totals(data, currency),
            _journal_view(data, currency, date_from=d_from, date_to=d_to, show_actions=False),
        )
        return _print_shell(company, t("acct.tab_journal"), _period_subtitle(d_from, d_to), body)

    @app.get("/accounting/export/journal/csv")
    async def journal_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            company, data = await asyncio.gather(api.get_company(token), api.get_journal(token, params))
        except APIError as e:
            return _plain_error_response(e)

        base_currency = company.get("currency") or ""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "entry_id", "source_ref", "memo", "account_code", "account_name",
                    "debit", "credit", "currency", "fx_currency", "fx_debit", "fx_credit",
                    "exchange_rate", "status"])
        # Rows stay ascending by date (the order the API returns): auditors and pivot
        # tables read a journal chronologically, unlike the on-screen newest-first view.
        for entry in data.get("entries", []):
            source = entry.get("source_doc") or {}
            source_ref = source.get("doc_ref") or source.get("doc_id") or _je_source_label(entry, csv_export=True)
            fx = entry.get("fx") or {}
            fx_currency = fx.get("currency") or ""
            rate = fx.get("rate") or 0
            for line in entry.get("lines", []):
                debit = float(line.get("debit") or 0)
                credit = float(line.get("credit") or 0)
                _fx_d, _fx_c = _fx_line_amounts(
                    debit, credit, fx, line.get("fx_debit"), line.get("fx_credit"))
                # A zero foreign amount is not a figure the author typed: the
                # rounding plug carries 0.0 on both sides and belongs in the
                # export as blank cells, with no currency claimed for it.
                fx_debit = _fx_d if _fx_d else ""
                fx_credit = _fx_c if _fx_c else ""
                line_fx_currency = fx_currency if (fx_debit != "" or fx_credit != "") else ""
                line_rate = rate if (fx_debit != "" or fx_credit != "") else ""
                csv_row(w, [
                    str(entry.get("ts") or "")[:10],
                    entry.get("je_id", ""),
                    source_ref,
                    entry.get("memo", ""),
                    line.get("account", ""),
                    line.get("name", ""),
                    debit,
                    credit,
                    base_currency,
                    line_fx_currency,
                    fx_debit,
                    fx_credit,
                    line_rate or "",
                    entry.get("status", ""),
                ])

        buf.seek(0)
        fname = f"journal_{_fname_date(d_from)}_{_fname_date(d_to)}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )



# ── Journal ─────────────────────────────────────────────────────────────────

def _je_source_label(entry: dict, csv_export: bool = False) -> str:
    """Source label for a journal entry without a source document, keyed off je_type.

    CSV exports always use the English label so the exported data is locale-stable.
    """
    je_type = str(entry.get("je_type") or "")
    if je_type == "manual":
        key = "acct.source_manual"
    elif je_type == "transfer":
        key = "acct.source_transfer"
    elif je_type.startswith("recon"):
        key = "acct.source_reconciliation"
    else:
        key = "acct.source_system"
    return t(key, "en") if csv_export else t(key)


def _journal_totals(data: dict, currency: str | None = None) -> FT:
    total_debit = float(data.get("total_debit", 0) or 0)
    total_credit = float(data.get("total_credit", 0) or 0)
    return _totals_chips(total_debit, total_credit, abs(total_debit - total_credit) < 0.01, currency)


def _fx_line_amounts(debit: float, credit: float, fx: dict | None,
                     fx_debit: float | None = None,
                     fx_credit: float | None = None) -> tuple[float | None, float | None]:
    """A journal line's foreign-currency amounts, or (None, None) when the entry
    carries no rate.

    A manually entered foreign line stores the figures the author actually
    typed, and those win: they are what the source document says, and dividing
    the local amount back out can land a cent away from it. Only a
    document-linked line, which stores no foreign amounts of its own, is
    derived from the recorded rate. Without a rate nothing is shown: a guessed
    figure on an audited journal is worse than a blank.
    """
    if fx_debit is not None or fx_credit is not None:
        return (fx_debit, fx_credit)
    rate = (fx or {}).get("rate") or 0
    fx_currency = (fx or {}).get("currency")
    # A rate with no currency cannot be formatted: the amount would be rounded
    # at a defaulted precision and shown under a currency nobody recorded.
    if not rate or not fx_currency:
        return (None, None)
    rate_d = to_decimal(rate)
    fx_debit = float(round_money(to_decimal(debit) / rate_d, fx_currency)) if debit else None
    fx_credit = float(round_money(to_decimal(credit) / rate_d, fx_currency)) if credit else None
    return (fx_debit, fx_credit)


def _fmt_exchange_rate(rate) -> str:
    """An exchange rate is a ratio, not an amount: no currency symbol, and
    trailing zeros trimmed so 35.0 reads as 35 and 36.55 keeps its precision."""
    try:
        s = f"{float(rate):,.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""
    return s or "0"


def _journal_view(data: dict, currency: str | None = None, date_from: str = "",
                  date_to: str = "", show_actions: bool = True) -> FT:
    entries = list(data.get("entries", []))
    if not entries:
        return P(t("acct.no_journal_entries"), cls="empty-state")
    entries.reverse()  # the API returns ascending; the screen shows newest first
    # Foreign-currency columns appear only when this period actually holds a
    # foreign-currency transaction, so a single-currency journal stays as narrow
    # as it has always been.
    has_fx = any(e.get("fx") for e in entries)

    def _fmt_nonzero(val: float) -> str:
        return fmt_money(val, currency) if val else ""

    def _void_action(entry: dict) -> FT:
        if entry.get("status") != "posted":
            return Td("")
        # Auto-posted entries keep the control and are refused by the server with
        # an explanation naming the source document. Removing the button instead
        # would leave the user guessing why an entry cannot be voided (GDR 2e:
        # validate at the function level, never restrict the interface).
        return Td(Details(
            Summary(t("btn.void"), cls="btn btn--danger btn--sm"),
            Form(
                Input(type="text", name="reason", placeholder=t("acct.void_reason_optional"),
                      cls="form-input form-input--sm",
                      onkeydown="if(event.key==='Escape'){this.closest('details').removeAttribute('open');event.preventDefault();}"),
                Input(type="hidden", name="date_from", value=date_from),
                Input(type="hidden", name="date_to", value=date_to),
                Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger btn--sm",
                       style="margin-top:0.5rem;"),
                hx_post=f"/accounting/journal/{entry.get('je_id', '')}/void",
                hx_swap="none", cls="inline-form",
            ),
            cls="void-section",
        ))

    rows = []
    for entry in entries:
        voided = entry.get("status") == "void"
        row_cls = "payment-voided" if voided else ""
        source = entry.get("source_doc") or {}
        if source.get("doc_id"):
            source_cell = A(source.get("doc_ref") or source["doc_id"],
                            href=f"/docs/{source['doc_id']}", cls="drilldown-link")
        else:
            source_cell = _je_source_label(entry)
        memo_bits: list = [Span(entry.get("memo", ""))] if entry.get("memo") else []
        if voided:
            reason = str(entry.get("void_reason") or entry.get("reason") or "").strip()
            memo_bits.append(Span(f"{t('doc.voided')}: {reason}" if reason else t("doc.voided"),
                                  cls="badge badge--void"))
        cells = [
            Td(str(entry.get("ts") or "")[:10], cls="cell--mono"),
            Td(source_cell),
            Td(*memo_bits, cls="cell--muted"),
            Td("", cls="cell--number"),
            Td("", cls="cell--number"),
        ]
        fx = entry.get("fx") or {}
        if has_fx:
            # The rate belongs to the transaction, so it is stated once on the
            # entry, next to the lines it converted.
            cells += [
                Td("", cls="cell--number"),
                Td("", cls="cell--number"),
                Td(_fmt_exchange_rate(fx.get("rate")) if fx.get("rate") else "",
                   cls="cell--number cell--mono"),
            ]
        if show_actions:
            cells.append(_void_action(entry))
        rows.append(Tr(*cells, cls=row_cls) if row_cls else Tr(*cells))
        for line in entry.get("lines", []):
            debit = float(line.get("debit") or 0)
            credit = float(line.get("credit") or 0)
            line_cells = [
                Td(""),
                Td(""),
                Td(f"{line.get('account', '')} {line.get('name', '')}".strip(),
                   style="padding-left:2rem"),
                Td(_fmt_nonzero(debit), cls="cell--number"),
                Td(_fmt_nonzero(credit), cls="cell--number"),
            ]
            if has_fx:
                fx_debit, fx_credit = _fx_line_amounts(
                    debit, credit, fx, line.get("fx_debit"), line.get("fx_credit"))
                fx_currency = fx.get("currency")
                line_cells += [
                    Td(fmt_money(fx_debit, fx_currency) if fx_debit else "", cls="cell--number"),
                    Td(fmt_money(fx_credit, fx_currency) if fx_credit else "", cls="cell--number"),
                    Td("", cls="cell--number"),
                ]
            if show_actions:
                line_cells.append(Td(""))
            rows.append(Tr(*line_cells, cls=row_cls) if row_cls else Tr(*line_cells))

    headers = [
        Th(t("th.date")),
        Th(t("th.source")),
        Th(t("th.description")),
        Th(t("th.debit"), cls="cell--number"),
        Th(t("th.credit"), cls="cell--number"),
    ]
    if has_fx:
        headers += [
            Th(t("th.fx_debit"), cls="cell--number"),
            Th(t("th.fx_credit"), cls="cell--number"),
            Th(t("th.rate"), cls="cell--number"),
        ]
    if show_actions:
        headers.append(Th(""))
    return Table(Thead(Tr(*headers)), Tbody(*rows), cls="data-table")


# ── Manual journal entry form ───────────────────────────────────────────────

def _je_line_row(idx: str, line: dict, acct_opts: list[tuple[str, str]]) -> FT:
    return Tr(
        Td(searchable_select(f"account_{idx}", acct_opts, value=line.get("account", ""),
                             placeholder=t("th.account"), cls_extra="cell-input")),
        Td(Input(type="number", name=f"debit_{idx}", value=line.get("debit", ""), step="any",
                 min="0", cls="cell-input", oninput="celerpJeTotals()",
                 onkeydown="if(event.key==='Escape'){this.blur();event.preventDefault();}"), cls="cell--number"),
        Td(Input(type="number", name=f"credit_{idx}", value=line.get("credit", ""), step="any",
                 min="0", cls="cell-input", oninput="celerpJeTotals()",
                 onkeydown="if(event.key==='Escape'){this.blur();event.preventDefault();}"), cls="cell--number"),
        # Read-only: the book figures are server-computed and must never be
        # request fields, or a client could post amounts unrelated to the rate.
        Td("", cls="cell--number cell--muted je-local-debit"),
        Td("", cls="cell--number cell--muted je-local-credit"),
        Td(Button("✕", type="button", cls="btn btn--ghost btn--sm", title=t("btn.remove"),
                  onclick="celerpJeRemoveLine(this)")),
    )


def _journal_entry_form(accounts: list[dict], ts: str, memo: str, lines: list[dict],
                        idem_token: str, error: str | None = None,
                        date_from: str = "", date_to: str = "",
                        currency: str = "", rate: str = "",
                        base_currency: str = "") -> FT:
    acct_opts = [
        (a.get("code", ""), f"{a.get('code', '')} {a.get('name', '')}".strip())
        for a in accounts
        if a.get("code") and a.get("is_active", True)
    ]
    if not lines:
        lines = [{}, {}]  # double-entry needs two sides, so start with two rows
    rows = [_je_line_row(str(i), line, acct_opts) for i, line in enumerate(lines)]

    js = f"""
var celerpJeIdx = {len(lines)};
var celerpJeBase = {_json.dumps(base_currency)};
function celerpJeAddLine() {{
    var tpl = document.getElementById('je-line-tpl').content.cloneNode(true);
    tpl.querySelectorAll('[name]').forEach(function(el) {{
        el.name = el.name.replace('__IDX__', celerpJeIdx);
        if (el.dataset && el.dataset.name) el.dataset.name = el.name;
    }});
    celerpJeIdx++;
    var tbody = document.getElementById('je-lines');
    tbody.appendChild(tpl);
    // cloneNode fires neither DOMContentLoaded nor htmx:afterSettle, so the new
    // row's combobox must be initialised here.
    tbody.lastElementChild.querySelectorAll('.combobox-wrap').forEach(initCombobox);
    celerpJeTotals();
}}
function celerpJeRemoveLine(btn) {{
    btn.closest('tr').remove();
    celerpJeTotals();
}}
function celerpJeFx() {{
    // The picker writes through a hidden input, same as every combobox here.
    var cur = document.querySelector('[name="currency"]');
    var rateEl = document.querySelector('[name="rate"]');
    var rate = parseFloat(rateEl ? rateEl.value : '') || 0;
    return {{ currency: cur ? (cur.value || '') : '', rate: rate }};
}}
function celerpJeTotals() {{
    var d = 0, c = 0;
    var fx = celerpJeFx();
    var on = fx.currency !== '' && fx.rate > 0;
    document.querySelectorAll('#je-lines [name^="debit_"]').forEach(function(el) {{ d += Math.round((parseFloat(el.value) || 0) * 100); }});
    document.querySelectorAll('#je-lines [name^="credit_"]').forEach(function(el) {{ c += Math.round((parseFloat(el.value) || 0) * 100); }});
    // Typed columns are foreign under a rate; say so in the header and totals.
    var fxTag = on ? ' (' + fx.currency + ')' : '';
    document.getElementById('je-debit-head').textContent = {_json.dumps(t("th.debit"))} + fxTag;
    document.getElementById('je-credit-head').textContent = {_json.dumps(t("th.credit"))} + fxTag;
    document.getElementById('je-total-debit').textContent = {_json.dumps(t("acct.total_debit"))} + fxTag + ': ' + (d / 100).toFixed(2);
    document.getElementById('je-total-credit').textContent = {_json.dumps(t("acct.total_credit"))} + fxTag + ': ' + (c / 100).toFixed(2);
    var chip = document.getElementById('je-balance-chip');
    // Balance is judged on the amounts the user typed, which under a rate are
    // the foreign ones. The server applies the same rule.
    var balanced = d === c && d > 0;
    chip.textContent = balanced ? {_json.dumps(t("acct.balanced"))} : {_json.dumps(t("acct.out_of_balance"))};
    chip.className = balanced ? 'val-chip' : 'val-chip val-chip--alert';

    // The book side reads like a journal: computed debit and credit columns in
    // the company currency, labelled with its code when known.
    var baseTag = celerpJeBase ? ' (' + celerpJeBase + ')' : '';
    document.getElementById('je-local-debit-head').textContent = on ? {_json.dumps(t("th.debit"))} + baseTag : '';
    document.getElementById('je-local-credit-head').textContent = on ? {_json.dumps(t("th.credit"))} + baseTag : '';
    var localDebit = 0, localCredit = 0;
    document.querySelectorAll('#je-lines tr').forEach(function(row) {{
        var dCell = row.querySelector('.je-local-debit');
        var cCell = row.querySelector('.je-local-credit');
        if (!dCell || !cCell) return;
        if (!on) {{ dCell.textContent = ''; cCell.textContent = ''; return; }}
        var dv = parseFloat((row.querySelector('[name^="debit_"]') || {{}}).value) || 0;
        var cv = parseFloat((row.querySelector('[name^="credit_"]') || {{}}).value) || 0;
        // Each line converts and rounds on its own, exactly as the server does,
        // so the preview cannot disagree with what gets posted.
        var ld = Math.round(dv * fx.rate * 100), lc = Math.round(cv * fx.rate * 100);
        localDebit += ld; localCredit += lc;
        dCell.textContent = ld ? (ld / 100).toFixed(2) : '';
        cCell.textContent = lc ? (lc / 100).toFixed(2) : '';
    }});
    // Book totals are the column sums; the rounding preview names the 6960
    // line that reconciles any residual between them.
    var tld = document.getElementById('je-total-local-debit');
    var tlc = document.getElementById('je-total-local-credit');
    tld.hidden = !on; tlc.hidden = !on;
    if (on) {{
        tld.textContent = {_json.dumps(t("acct.total_debit"))} + baseTag + ': ' + (localDebit / 100).toFixed(2);
        tlc.textContent = {_json.dumps(t("acct.total_credit"))} + baseTag + ': ' + (localCredit / 100).toFixed(2);
    }}

    var preview = document.getElementById('je-rounding-preview');
    var residual = localCredit - localDebit;
    if (on && balanced && residual !== 0) {{
        // GDR 2d: nothing is added to the books without the user seeing it first.
        var side = residual > 0 ? {_json.dumps(t("th.debit"))} : {_json.dumps(t("th.credit"))};
        preview.textContent = {_json.dumps(t("acct.rounding_line_preview"))}
            .replace('{{account}}', '6960')
            .replace('{{side}}', side)
            .replace('{{amount}}', (Math.abs(residual) / 100).toFixed(2));
    }} else {{
        preview.textContent = '';
    }}
}}
if (document.readyState === 'loading') {{ document.addEventListener('DOMContentLoaded', celerpJeTotals); }} else {{ celerpJeTotals(); }}
"""

    return Div(
        Div(error, cls="error-banner") if error else None,
        Form(
            Div(
                Div(
                    Label(t("th.date"), cls="form-label"),
                    Input(type="date", name="ts", value=ts, cls="date-input", required=True,
                  onkeydown="if(event.key==='Escape'){this.blur();event.preventDefault();}"),
                ),
                Div(
                    Label(t("th.memo"), cls="form-label"),
                    Input(type="text", name="memo", value=memo, cls="form-input",
                          placeholder=t("acct.memo_hint"),
                          onkeydown="if(event.key==='Escape'){this.blur();event.preventDefault();}"),
                ),
                cls="flex-row gap-sm",
            ),
            # Collapsed by default: an accountant who never posts in a foreign
            # currency sees the same form as before and no extra decision.
            Details(
                Summary(t("acct.record_foreign_currency"), cls="btn btn--ghost btn--sm"),
                Div(
                    Div(
                        Label(t("th.currency"), cls="form-label"),
                        searchable_select("currency", CURRENCIES, value=currency,
                                          placeholder=t("acct.currency_base_hint"),
                                          cls_extra="cell-input"),
                    ),
                    Div(
                        Label(t("th.rate"), cls="form-label"),
                        Input(type="number", name="rate", value=rate or "1.0000", step="0.0001",
                              min="0", cls="form-input", oninput="celerpJeTotals()",
                              onkeydown="if(event.key==='Escape'){this.closest('details').removeAttribute('open');event.preventDefault();}"),
                    ),
                    cls="flex-row gap-sm",
                ),
                cls="je-fx-reveal",
                # Reopened when a currency survives a failed submission, so the
                # user sees the values they typed instead of a collapsed control
                # that looks like it was never touched.
                open=bool(currency),
            ),
            Div("", id="je-rounding-preview", cls="report-section"),
            Template(_je_line_row("__IDX__", {}, acct_opts), id="je-line-tpl"),
            Table(
                Thead(Tr(
                    Th(t("th.account")),
                    Th(t("th.debit"), id="je-debit-head", cls="cell--number"),
                    Th(t("th.credit"), id="je-credit-head", cls="cell--number"),
                    Th("", id="je-local-debit-head", cls="cell--number"),
                    Th("", id="je-local-credit-head", cls="cell--number"),
                    Th(""),
                )),
                Tbody(*rows, id="je-lines"),
                cls="data-table",
            ),
            Div(
                Button(t("btn.add_line"), type="button", cls="btn btn--secondary btn--sm",
                       onclick="celerpJeAddLine()"),
                Div(
                    Span("", id="je-total-debit", cls="val-chip"),
                    Span("", id="je-total-credit", cls="val-chip"),
                    Span("", id="je-total-local-debit", cls="val-chip", hidden=True),
                    Span("", id="je-total-local-credit", cls="val-chip", hidden=True),
                    Span("", id="je-balance-chip", cls="val-chip"),
                    cls="valuation-bar",
                ),
                cls="flex-row flex-between mt-md",
            ),
            Input(type="hidden", name="idempotency_token", value=idem_token),
            Input(type="hidden", name="date_from", value=date_from),
            Input(type="hidden", name="date_to", value=date_to),
            Div(
                Button(t("btn.post"), type="submit", cls="btn btn--primary"),
                A(t("btn.cancel"),
                  href=_href("/accounting", {"tab": "journal", "from": date_from, "to": date_to}),
                  cls="btn btn--secondary"),
                cls="flex-row gap-sm",
            ),
            method="post", action="/accounting/journal/new",
        ),
        Script(js),
    )

