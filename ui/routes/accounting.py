# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import csv
import io
import math as _math
import re
import json as _json
import uuid
import asyncio
from datetime import date as _date
from urllib.parse import quote_plus, urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, StreamingResponse, PlainTextResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.currency import CURRENCIES
from ui.components.table import empty_state_cta, fmt_money, searchable_select
from ui.config import get_token as _token, get_role as _get_role
from celerp.services.auth import ROLE_LEVELS as _ROLE_LEVELS
from ui.i18n import t, get_lang
from ui.routes.documents import _action_error
from ui.routes.reports import _date_filter_bar, _get_fiscal, _parse_dates, _resolve_preset
from celerp.services.money import round_money, to_decimal

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


def _csv_safe(cell):
    """Neutralize spreadsheet formula injection: user-authored strings (memos, account and
    contact names) must never execute when the export is opened in a spreadsheet, so string
    cells starting with a formula trigger character get a leading single quote."""
    if isinstance(cell, str) and cell and cell[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + cell
    return cell


def _csv_row(writer, cells: list) -> None:
    writer.writerow([_csv_safe(c) for c in cells])


def _plain_error_response(e: APIError):
    """Error mapping for print/export routes (non-shell responses)."""
    if e.status == 401:
        return RedirectResponse("/login", status_code=302)
    if e.status == 403:
        return PlainTextResponse(t("acct.not_authorized"), status_code=403)
    return PlainTextResponse(f"Error: {e.detail}", status_code=500)


def _date_params(d_from: str, d_to: str) -> dict:
    return {k: v for k, v in {"date_from": d_from, "date_to": d_to}.items() if v}


def _fname_date(v: str) -> str:
    """A date for a download filename: digits and dashes only, else 'all'.

    Filenames land in a Content-Disposition header, so raw query text must
    never reach them (header parameter injection, latin-1 encoding errors).
    """
    v = (v or "")[:10]
    return v if re.fullmatch(r"[0-9-]+", v) else "all"


def _href(path: str, params: dict) -> str:
    """path?k=v joining only non-empty params, values URL-encoded; bare path
    when none. Encoded values also keep header-borne redirects (HX-Redirect,
    Location) free of raw user text."""
    qs = urlencode({k: v for k, v in params.items() if v})
    return f"{path}?{qs}" if qs else path


def _soa_merge_redirect(request: Request, winner: str, base_path: str, requested: str = ""):
    """Re-point a print/export URL at the merge winner's statement.

    Carries the contact that was asked for so the printed page and the exported
    file both state whose statement this actually is: substituting one party's
    figures for another's without saying so would be a silent swap on an
    outward-facing document.
    """
    params = dict(request.query_params)
    params["contact_id"] = winner
    if requested and requested != winner:
        params["merged_from"] = requested
    return RedirectResponse(f"{base_path}?{urlencode(params)}", status_code=302)


def _period_subtitle(d_from: str, d_to: str) -> str:
    parts = []
    if d_from:
        parts.append(f"From: {d_from}")
    if d_to:
        parts.append(f"To: {d_to}")
    return "  |  ".join(parts) if parts else "All periods"


# Tabs a ledger drilldown can arrive from (via the "src" query param) -> tab label key.
_LEDGER_BACK_TABS = {
    "pnl": "acct.tab_pnl",
    "balance-sheet": "acct.tab_balance_sheet",
    "trial-balance": "acct.tab_trial_balance",
    "general-ledger": "acct.tab_general_ledger",
}


def _ledger_context(request: Request, fy: str) -> tuple[str, str, str, str]:
    """Resolve (origin_tab, date_from, date_to, preset) for the ledger drilldown.

    Drilldown links from the report tabs name their originating tab in "src" and
    carry their dates as date_from/date_to. "src" stays outside the date bar's
    reserved from/to/preset fields, so the custom-range form's hidden-param
    carry-through preserves the origin across date changes on the drilldown page.
    """
    qp = request.query_params
    origin = qp.get("src", "")
    if origin in _LEDGER_BACK_TABS:
        preset = qp.get("preset", "")
        if preset and preset != "custom":
            d_from, d_to = _resolve_preset(preset, fy)
            return origin, d_from, d_to, preset
        # Drilldown links carry date_from/date_to; the page's own custom-range
        # form submits from/to.
        d_from = qp.get("date_from", "") or qp.get("from", "")
        d_to = qp.get("date_to", "") or qp.get("to", "")
        if d_from or d_to:
            if d_from and d_to and d_from > d_to:
                d_from, d_to = d_to, d_from
            return origin, d_from, d_to, "custom"
        # Drilldown links always encode their view's dates; none means the
        # originating view was unfiltered, so match it with the full history.
        return origin, "", "", "all"
    d_from, d_to, preset = _parse_dates(request, fy)
    return "trial-balance", d_from, d_to, preset


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
            Div(NotStr('Powered by <a href="https://celerp.com">celerp.com</a>'), cls="report-footer"),
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
thead th { background: #f5f5f5; font-weight: 700; text-align: center; padding: 1.5mm 2mm; border-bottom: 1px solid #ccc; }
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

.report-footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 2mm 20mm; border-top: 1px solid #ddd; font-size: 8pt; color: #aaa; text-align: center; background: white; }
.report-footer a { color: #aaa; text-decoration: none; }
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
                    _pnl_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
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
                    _balance_sheet_view(data, currency, as_of=as_of),
                )
            elif tab == "trial-balance":
                fy = await _get_fiscal(token)
                d_from, d_to, preset = _parse_dates(request, fy)
                tb_params = {}
                if d_from:
                    tb_params["date_from"] = d_from
                if d_to:
                    tb_params["date_to"] = d_to
                trial_balance = await api.get_trial_balance(token, tb_params)
                content = Div(
                    Div(
                        _date_filter_bar("/accounting", d_from, d_to, preset,
                                         settings_link="/settings/general?tab=company",
                                         extra_params="&tab=trial-balance"),
                        _action_bar("trial-balance", {"date_from": d_from or "", "date_to": d_to or ""}),
                        cls="flex-row flex-between",
                    ),
                    _trial_balance_summary(trial_balance, currency),
                    _trial_balance_table(trial_balance, currency, date_from=d_from or "", date_to=d_to or ""),
                )
            elif tab == "journal":
                fy = await _get_fiscal(token)
                d_from, d_to, preset = _parse_dates(request, fy)
                params = _date_params(d_from, d_to)
                data = await api.get_journal(token, params)
                content = Div(
                    Div(
                        _date_filter_bar("/accounting", d_from, d_to, preset,
                                         settings_link="/settings/general?tab=company",
                                         extra_params="&tab=journal"),
                        Div(
                            A(t("btn.new_entry"),
                              href=_href("/accounting/journal/new",
                                         _date_params(d_from or "", d_to or "")),
                              cls="btn btn--primary"),
                            _action_bar("journal", {"date_from": d_from or "", "date_to": d_to or ""}),
                            cls="flex-row gap-sm ml-auto",
                        ),
                        cls="flex-row flex-between",
                    ),
                    _journal_totals(data, currency),
                    _journal_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
                )
            elif tab == "general-ledger":
                fy = await _get_fiscal(token)
                d_from, d_to, preset = _parse_dates(request, fy)
                params = _date_params(d_from, d_to)
                data = await api.get_general_ledger(token, params)
                content = Div(
                    Div(
                        _date_filter_bar("/accounting", d_from, d_to, preset,
                                         settings_link="/settings/general?tab=company",
                                         extra_params="&tab=general-ledger"),
                        _action_bar("general-ledger", {"date_from": d_from or "", "date_to": d_to or ""}),
                        cls="flex-row flex-between",
                    ),
                    _general_ledger_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
                )
            elif tab == "soa":
                fy = await _get_fiscal(token)
                d_from, d_to, preset = _parse_dates(request, fy)
                contact_id = request.query_params.get("contact_id", "")
                contacts = (await api.list_contacts(token, {"limit": 1000})).get("items", [])
                data = None
                if contact_id:
                    data = await api.get_soa(
                        token, contact_id,
                        {k: v for k, v in {"date_from": d_from, "date_to": d_to}.items() if v},
                    )
                    winner = data.get("merged_into") or (data.get("contact") or {}).get("id") or contact_id
                    if winner != contact_id:
                        # Merged contact: keep the URL on the merge winner so the
                        # bookmarkable state matches what is shown, and carry the
                        # contact that was asked for so the destination can say
                        # whose statement this actually is (GDR 2d: never
                        # substitute data silently).
                        qs = (f"?tab=soa&contact_id={quote_plus(winner)}"
                              f"&merged_from={quote_plus(contact_id)}")
                        if d_from:
                            qs += f"&from={d_from}"
                        if d_to:
                            qs += f"&to={d_to}"
                        return RedirectResponse(f"/accounting{qs}", status_code=302)
                extra = f"&tab=soa&contact_id={quote_plus(contact_id)}" if contact_id else "&tab=soa"
                content = Div(
                    Div(
                        _date_filter_bar("/accounting", d_from, d_to, preset,
                                         settings_link="/settings/general?tab=company",
                                         extra_params=extra),
                        _action_bar("soa", {"contact_id": contact_id,
                                            "date_from": d_from or "",
                                            "date_to": d_to or ""}) if contact_id else None,
                        cls="flex-row flex-between",
                    ),
                    _soa_view(data, contacts, contact_id, currency,
                              date_from=d_from or "", date_to=d_to or "",
                              merged_from=request.query_params.get("merged_from", "")),
                )
            else:
                return RedirectResponse("/accounting", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 403:
                content = Div(t("acct.not_authorized"), cls="error-banner")
            else:
                content = Div(f"{t('acct.error_loading_data')}: {e.detail}", cls="error-banner")

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

    # ── Manual journal entries ─────────────────────────────────────────────

    async def _render_journal_form(request: Request, token: str, ts: str, memo: str,
                                   lines: list[dict], idem_token: str, error: str | None,
                                   date_from: str = "", date_to: str = "",
                                   currency: str = "", rate: str = ""):
        if _ROLE_LEVELS.get(_get_role(request), 0) < _ROLE_LEVELS["manager"]:
            return base_shell(
                page_header(t("acct.new_journal_entry", get_lang(request))),
                _accounting_tabs("journal"),
                Div(t("acct.not_authorized"), cls="error-banner"),
                title="Accounting - Celerp",
                nav_active="accounting",
                request=request,
            )
        try:
            accounts = (await api.get_chart(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            accounts = []
            if error is None:
                error = (t("acct.not_authorized") if e.status == 403
                         else f"{t('acct.error_loading_data')}: {e.detail}")
        return base_shell(
            page_header(t("acct.new_journal_entry", get_lang(request))),
            _accounting_tabs("journal"),
            _journal_entry_form(accounts, ts, memo, lines, idem_token, error,
                                date_from=date_from, date_to=date_to,
                                currency=currency, rate=rate),
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
            params = _date_params(d_from, d_to)
            data = await api.get_pnl(token, params)
        except APIError as e:
            return _plain_error_response(e)

        body = _pnl_view(data, currency, date_from=d_from, date_to=d_to)
        return _print_shell(company, "Profit & Loss Statement", _period_subtitle(d_from, d_to), body)

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
            return _plain_error_response(e)

        body = _balance_sheet_view(data, currency, as_of=as_of)
        return _print_shell(company, "Balance Sheet", f"As of: {as_of}", body)

    @app.get("/accounting/print/trial-balance")
    async def trial_balance_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_trial_balance(token, params)
        except APIError as e:
            return _plain_error_response(e)

        body = Div(
            _trial_balance_summary(data, currency),
            _trial_balance_table(data, currency, date_from=d_from, date_to=d_to),
        )
        return _print_shell(company, "Trial Balance", _period_subtitle(d_from, d_to), body)

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

    @app.get("/accounting/print/general-ledger")
    async def general_ledger_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_general_ledger(token, params)
        except APIError as e:
            return _plain_error_response(e)

        body = _general_ledger_view(data, currency, date_from=d_from, date_to=d_to)
        return _print_shell(company, t("acct.tab_general_ledger"), _period_subtitle(d_from, d_to), body)

    @app.get("/accounting/print/soa")
    async def soa_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        contact_id = request.query_params.get("contact_id", "")
        if not contact_id:
            return RedirectResponse("/accounting?tab=soa", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_soa(token, contact_id, params)
        except APIError as e:
            return _plain_error_response(e)
        if data.get("merged_into"):
            return _soa_merge_redirect(request, data["merged_into"], "/accounting/print/soa",
                                       requested=contact_id)

        contact = data.get("contact") or {}
        # An outward document: company block (report header), contact block, period, closing.
        body = Div(
            (P(t("acct.soa_merged_notice"), cls="report-section")
             if request.query_params.get("merged_from") else None),
            Div(
                H3(t("label.contact"), cls="report-section-title"),
                P(contact.get("name", "")),
                P(contact.get("type", ""), cls="report-company-address") if contact.get("type") else None,
                cls="report-section",
            ),
            _soa_table(data, currency, date_from=d_from, date_to=d_to),
            P(Strong(f"{t('acct.closing_balance')}: {fmt_money(data.get('closing_balance', 0), currency)}"),
              cls="report-total"),
        )
        return _print_shell(company, t("acct.soa_title"), _period_subtitle(d_from, d_to), body)

    # ── CSV export routes ──────────────────────────────────────────────────

    @app.get("/accounting/export/pnl/csv")
    async def pnl_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_pnl(token, params)
        except APIError as e:
            return _plain_error_response(e)

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
                _csv_row(w, [section_label, line.get("code", ""), line.get("name", ""), amt, _pct(amt)])
            w.writerow([f"TOTAL {section_label}", "", "", section.get("total", 0), ""])

        w.writerow([])
        w.writerow(["Gross Profit", "", "", data.get("gross_profit", 0), ""])
        w.writerow(["Net Profit", "", "", data.get("net_profit", 0), ""])

        buf.seek(0)
        fname = f"pnl_{_fname_date(d_from)}_{_fname_date(d_to)}.csv"
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
            return _plain_error_response(e)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Section", "Code", "Account", "Amount"])
        for section_key, section_label in [("assets", "Assets"), ("liabilities", "Liabilities"), ("equity", "Equity")]:
            section = data.get(section_key, {})
            for line in section.get("lines", []):
                _csv_row(w, [section_label, line.get("code", ""), line.get("name", ""), line.get("amount", 0)])
            w.writerow([f"TOTAL {section_label}", "", "", section.get("total", 0)])
            w.writerow([])

        buf.seek(0)
        fname = f"balance_sheet_{_fname_date(as_of)}.csv"
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
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_trial_balance(token, params)
        except APIError as e:
            return _plain_error_response(e)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Code", "Account", "Debit", "Credit"])
        for line in data.get("lines", []):
            _csv_row(w, [line.get("code", ""), line.get("name", ""), line.get("total_debit", 0), line.get("total_credit", 0)])
        w.writerow([])
        w.writerow(["TOTAL", "", data.get("total_debit", 0), data.get("total_credit", 0)])

        buf.seek(0)
        fname = f"trial_balance_{_fname_date(d_from)}_{_fname_date(d_to)}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

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
                _csv_row(w, [
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

    @app.get("/accounting/export/general-ledger/csv")
    async def general_ledger_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            # One call: the backend buckets summary and detail together, so the
            # export can never disagree with the on-screen general ledger.
            data = await api.get_general_ledger(
                token, {**_date_params(d_from, d_to), "include_lines": "1"})

            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["account_code", "account_name", "date", "source_ref", "memo", "debit", "credit", "balance"])
            for row in data.get("rows", []):
                code = row.get("code", "")
                name = row.get("name", "")
                debit_normal = row.get("debit_normal")
                opening = row.get("opening", 0)
                _csv_row(w, [code, name, "", "", "Opening balance", "", "", opening])
                running = to_decimal(opening)
                for line in row.get("lines", []):
                    debit = to_decimal(line.get("debit") or 0)
                    credit = to_decimal(line.get("credit") or 0)
                    running += (debit - credit) if debit_normal else (credit - debit)
                    _csv_row(w, [code, name, line.get("date", ""), line.get("source_ref") or "",
                                 line.get("memo", ""), float(debit), float(credit), float(running)])
                _csv_row(w, [code, name, "", "", "Closing balance", "", "", row.get("closing", 0)])
        except APIError as e:
            return _plain_error_response(e)

        buf.seek(0)
        fname = f"general_ledger_{_fname_date(d_from)}_{_fname_date(d_to)}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/accounting/export/soa/csv")
    async def soa_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        contact_id = request.query_params.get("contact_id", "")
        if not contact_id:
            return RedirectResponse("/accounting?tab=soa", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            params = _date_params(d_from, d_to)
            data = await api.get_soa(token, contact_id, params)
        except APIError as e:
            return _plain_error_response(e)
        if data.get("merged_into"):
            return _soa_merge_redirect(request, data["merged_into"], "/accounting/export/soa/csv",
                                       requested=contact_id)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "ref", "kind", "debit", "credit", "balance"])
        if request.query_params.get("merged_from"):
            # The exported file states the substitution too: a spreadsheet
            # outlives the redirect that produced it.
            _csv_row(w, ["", "", t("acct.soa_merged_notice"), "", "", ""])
        _csv_row(w, [d_from, "", "Opening balance", "", "", data.get("opening_balance", 0)])
        for row in data.get("rows", []):
            _csv_row(w, [row.get("date", ""), row.get("doc_ref") or row.get("doc_id") or "",
                         row.get("kind", ""), row.get("debit", 0), row.get("credit", 0),
                         row.get("balance", 0)])
        _csv_row(w, [d_to, "", "Closing balance", "", "", data.get("closing_balance", 0)])

        buf.seek(0)
        contact_name = (data.get("contact") or {}).get("name") or contact_id
        contact_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", contact_name).strip("_") or "contact"
        fname = f"soa_{contact_slug}_{_fname_date(d_from)}_{_fname_date(d_to)}.csv"
        return StreamingResponse(
            iter([buf.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # ── Account ledger drilldown ───────────────────────────────────────────

    @app.get("/accounting/ledger/{account_code}")
    async def account_ledger_page(request: Request, account_code: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            fy = await _get_fiscal(token)
            origin, d_from, d_to, preset = _ledger_context(request, fy)
            params = {}
            if d_from:
                params["date_from"] = d_from
            if d_to:
                params["date_to"] = d_to
            data = await api.get_ledger(token, account_code, params)
            company = await api.get_company(token)
            currency = company.get("currency")
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 403:
                content = Div(t("acct.not_authorized"), cls="error-banner")
            else:
                content = Div(f"{t('acct.error_loading_data')}: {e.detail}", cls="error-banner")
            return base_shell(content, title="Ledger - Celerp", nav_active="accounting", request=request)

        # Back link follows the tab the user drilled down from.
        if origin == "balance-sheet":
            back_qs = f"?tab={origin}" + (f"&as_of={d_to}" if d_to else "")
        else:
            back_qs = f"?tab={origin}"
            if d_from:
                back_qs += f"&from={d_from}"
            if d_to:
                back_qs += f"&to={d_to}"

        content = Div(
            Div(
                _date_filter_bar(
                    f"/accounting/ledger/{account_code}", d_from, d_to, preset,
                    settings_link="/settings/general?tab=company",
                    extra_params=f"&src={origin}",
                ),
                A(f"← {t(_LEDGER_BACK_TABS[origin])}", href=f"/accounting{back_qs}", cls="btn btn--ghost btn--sm"),
                cls="flex-row flex-between",
            ),
            _ledger_view(data, currency),
        )

        account_name = data.get("account_name", account_code)
        return base_shell(
            page_header(f"Ledger: {account_code} {account_name}"),
            content,
            title=f"Ledger {account_code} - Celerp",
            nav_active="accounting",
            request=request,
        )


def _accounting_tabs(active: str) -> FT:
    tabs = [
        ("pnl", t("acct.tab_pnl")),
        ("balance-sheet", t("acct.tab_balance_sheet")),
        ("trial-balance", t("acct.tab_trial_balance")),
        ("journal", t("acct.tab_journal")),
        ("general-ledger", t("acct.tab_general_ledger")),
        ("soa", t("acct.tab_soa")),
    ]
    return Div(
        *[
            A(label, href=f"/accounting?tab={key}",
              cls=f"tab-link {'tab-link--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="tab-bar",
    )


def _drill_ledger_href(code: str, origin: str, date_from: str = "", date_to: str = "") -> str:
    """Link to the per-account ledger, carrying the date range and the originating tab."""
    parts = [f"date_from={date_from}"] if date_from else []
    if date_to:
        parts.append(f"date_to={date_to}")
    parts.append(f"src={origin}")
    return f"/accounting/ledger/{code}?" + "&".join(parts)


def _trial_balance_table(tb: dict, currency: str | None = None, date_from: str = "", date_to: str = "") -> FT:
    lines = tb.get("lines", [])
    if not lines:
        return P(t("acct.no_trial_balance_entries"), cls="empty-state")

    def _ledger_href(code: str) -> str:
        return _drill_ledger_href(code, "trial-balance", date_from, date_to)

    rows = [
        Tr(
            Td(l.get("code", ""), cls="cell--mono"),
            Td(A(l.get("name", ""), href=_ledger_href(l.get("code", "")), cls="drilldown-link")),
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


def _totals_chips(total_debit, total_credit, balanced: bool, currency: str | None) -> FT:
    return Div(
        Span(f"{t('acct.total_debit')}: {fmt_money(total_debit, currency)}", cls="val-chip"),
        Span(f"{t('acct.total_credit')}: {fmt_money(total_credit, currency)}", cls="val-chip"),
        Span(t("acct.balanced") if balanced else t("acct.out_of_balance"),
             cls="val-chip" if balanced else "val-chip val-chip--alert"),
        cls="valuation-bar",
    )


def _trial_balance_summary(tb: dict, currency: str | None = None) -> FT:
    return _totals_chips(tb.get("total_debit", 0), tb.get("total_credit", 0),
                         tb.get("balanced", True), currency)


def _ledger_view(data: dict, currency: str | None = None) -> FT:
    lines = data.get("lines", [])
    if not lines:
        return P(t("acct.no_entries_for_account"), cls="empty-state")

    def _fmt_nonzero(val: float) -> str:
        return fmt_money(val, currency) if val else ""

    def _balance_cls(val: float) -> str:
        if val < 0:
            return "cell--number cell--negative"
        return "cell--number"

    rows = [
        Tr(
            Td(line["date"], cls="cell--mono"),
            Td(
                A(line.get("doc_ref") or line["doc_id"], href=f"/docs/{line['doc_id']}", cls="drilldown-link")
                if line.get("doc_id") else "",
            ),
            Td(line.get("memo", ""), cls="cell--muted"),
            Td(_fmt_nonzero(line["debit"]), cls="cell--number"),
            Td(_fmt_nonzero(line["credit"]), cls="cell--number"),
            Td(fmt_money(line["balance"], currency), cls=_balance_cls(line["balance"])),
        )
        for line in lines
    ]
    return Table(
        Thead(Tr(
            Th(t("th.date")),
            Th(t("th.source")),
            Th(t("th.description")),
            Th(t("th.debit"), cls="cell--number"),
            Th(t("th.credit"), cls="cell--number"),
            Th(t("th.balance"), cls="cell--number"),
        )),
        Tbody(*rows),
        cls="data-table",
    )


def _pnl_view(data: dict, currency: str | None = None, date_from: str = "", date_to: str = "") -> FT:
    rev_total = float(data.get("revenue", {}).get("total", 0) or 0)

    # Build date query string for drilldown links
    _qs_parts = [f"date_from={date_from}"] if date_from else []
    if date_to:
        _qs_parts.append(f"date_to={date_to}")
    _dqs = ("?" + "&".join(_qs_parts)) if _qs_parts else ""

    def _ledger_href(code: str) -> str:
        return _drill_ledger_href(code, "pnl", date_from, date_to)

    def _pct_of_rev(amount: float) -> str:
        if not rev_total:
            return "--"
        return f"{abs(amount) / rev_total * 100:.1f}%"

    def _section(title, section_data, show_pct: bool = False, cls="", total_href: str = ""):
        lines = section_data.get("lines", [])
        rows = []
        for l in lines:
            amt = float(l.get("amount", 0) or 0)
            code = l.get("code", "")
            label = f"{code} {l.get('name', '')}".strip()
            name_cell = Td(A(label, href=_ledger_href(code), cls="drilldown-link") if code else label)
            cells = [
                name_cell,
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
        total_el = fmt_money(section_data.get('total', 0), currency)
        total_content = (
            A(Strong(total_el), href=total_href, cls="drilldown-link", target="_blank")
            if total_href else Strong(total_el)
        )
        return Div(
            H3(title, cls="report-section-title"),
            Table(Thead(header_row), Tbody(*rows), cls="data-table data-table--compact") if rows else P(t("acct.no_entries"), cls="empty-state"),
            P(total_content, cls="section-total"),
            cls=f"report-section {cls}",
        )

    net = float(data.get("net_profit", 0))
    return Div(
        _section("Revenue", data.get("revenue", {}), show_pct=True,
                 total_href=f"/reports/sales{_dqs}"),
        _section("Cost of Goods Sold", data.get("cogs", {}), show_pct=True,
                 total_href=f"/reports/purchases{_dqs}"),
        Div(P(Strong(f"Gross Profit: {fmt_money(data.get('gross_profit', 0), currency)}")), cls="report-subtotal"),
        _section("Operating Expenses", data.get("expenses", {}), show_pct=True,
                 total_href=f"/reports/purchases{_dqs}"),
        Div(
            P(Strong(f"Net Profit: {fmt_money(net, currency)}"),
              cls=f"net-profit {'net-profit--positive' if net >= 0 else 'net-profit--negative'}"),
            cls="report-total",
        ),
        cls="report-view",
    )


def _balance_sheet_view(data: dict, currency: str | None = None, as_of: str = "") -> FT:
    def _ledger_href(code: str) -> str:
        # Ledger up to as_of date for balance sheet context
        return _drill_ledger_href(code, "balance-sheet", "", as_of)

    def _pnl_href() -> str:
        return f"/accounting?tab=pnl&date_to={as_of}" if as_of else "/accounting?tab=pnl"

    def _tb_href() -> str:
        return f"/accounting?tab=trial-balance&as_of={as_of}" if as_of else "/accounting?tab=trial-balance"

    def _section(title, section_data):
        lines = section_data.get("lines", [])
        rows = []
        for l in lines:
            code = l.get("code", "")
            synthetic = l.get("synthetic", False)
            is_parent = l.get("is_parent", False)
            is_child = l.get("is_child", False)

            if synthetic and l.get("href_pnl"):
                label = l.get("name", "")
                name_cell = Td(
                    A(label, href=_pnl_href(), cls="drilldown-link"),
                    title="Net income accumulated from P&L. Click to see the calculation.",
                )
            elif is_parent:
                label = f"{code} {l.get('name', '')}".strip()
                name_cell = Td(Strong(label))
            elif is_child:
                label = f"{code} {l.get('name', '')}".strip()
                name_cell = Td(A(label, href=_ledger_href(code), cls="drilldown-link"), style="padding-left:2rem")
            elif code and not synthetic:
                label = f"{code} {l.get('name', '')}".strip()
                name_cell = Td(A(label, href=_ledger_href(code), cls="drilldown-link"))
            else:
                name_cell = Td(l.get("name", ""))

            amount_style = "padding-left:2rem" if is_child else ""
            rows.append(Tr(
                name_cell,
                Td(fmt_money(l.get('amount', 0), currency) if not is_parent else "", cls="cell--number", style=amount_style),
                Td(fmt_money(l.get('amount', 0), currency) if is_parent else "", cls="cell--number"),
            ) if is_parent else Tr(
                name_cell,
                Td(fmt_money(l.get('amount', 0), currency), cls="cell--number"),
            ))
        total_el = fmt_money(section_data.get('total', 0), currency)
        return Div(
            H3(title, cls="report-section-title"),
            Table(Tbody(*rows), cls="data-table data-table--compact") if rows else P(t("acct.no_entries"), cls="empty-state"),
            P(A(Strong(total_el), href=_tb_href(), cls="drilldown-link"), cls="section-total"),
            cls="report-section",
        )

    balanced = data.get("balanced", True)
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
        # Read-only: the local figure is server-computed and must never be a
        # request field, or a client could post amounts unrelated to the rate.
        Td("", cls="cell--number cell--muted je-local", data_idx=idx),
        Td(Button("✕", type="button", cls="btn btn--ghost btn--sm", title=t("btn.remove"),
                  onclick="celerpJeRemoveLine(this)")),
    )


def _journal_entry_form(accounts: list[dict], ts: str, memo: str, lines: list[dict],
                        idem_token: str, error: str | None = None,
                        date_from: str = "", date_to: str = "",
                        currency: str = "", rate: str = "") -> FT:
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
    document.getElementById('je-total-debit').textContent = {_json.dumps(t("acct.total_debit"))} + ': ' + (d / 100).toFixed(2);
    document.getElementById('je-total-credit').textContent = {_json.dumps(t("acct.total_credit"))} + ': ' + (c / 100).toFixed(2);
    var chip = document.getElementById('je-balance-chip');
    // Balance is judged on the amounts the user typed, which under a rate are
    // the foreign ones. The server applies the same rule.
    var balanced = d === c && d > 0;
    chip.textContent = balanced ? {_json.dumps(t("acct.balanced"))} : {_json.dumps(t("acct.out_of_balance"))};
    chip.className = balanced ? 'val-chip' : 'val-chip val-chip--alert';

    var head = document.getElementById('je-local-head');
    head.textContent = on ? {_json.dumps(t("acct.local_amount"))} : '';
    var localDebit = 0, localCredit = 0;
    document.querySelectorAll('#je-lines tr').forEach(function(row) {{
        var cell = row.querySelector('.je-local');
        if (!cell) return;
        if (!on) {{ cell.textContent = ''; return; }}
        var dv = parseFloat((row.querySelector('[name^="debit_"]') || {{}}).value) || 0;
        var cv = parseFloat((row.querySelector('[name^="credit_"]') || {{}}).value) || 0;
        // Each line converts and rounds on its own, exactly as the server does,
        // so the preview cannot disagree with what gets posted.
        var ld = Math.round(dv * fx.rate * 100), lc = Math.round(cv * fx.rate * 100);
        localDebit += ld; localCredit += lc;
        cell.textContent = ((ld || lc) / 100).toFixed(2);
    }});

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
                    Th(t("th.debit"), cls="cell--number"),
                    Th(t("th.credit"), cls="cell--number"),
                    Th("", id="je-local-head", cls="cell--number"),
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
                    Span("", id="je-balance-chip", cls="val-chip"),
                    cls="valuation-bar",
                ),
                cls="flex-row flex-between",
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


# ── General ledger ──────────────────────────────────────────────────────────

def _general_ledger_view(data: dict, currency: str | None = None,
                         date_from: str = "", date_to: str = "") -> FT:
    rows = data.get("rows", [])
    if not rows:
        return P(t("acct.no_entries"), cls="empty-state")

    def _num_cell(val, strong: bool = False) -> FT:
        v = float(val or 0)
        cls = "cell--number cell--negative" if v < 0 else "cell--number"
        formatted = fmt_money(v, currency)
        return Td(Strong(formatted) if strong else formatted, cls=cls)

    body = [
        Tr(
            Td(r.get("code", ""), cls="cell--mono"),
            Td(A(r.get("name", ""),
                 href=_drill_ledger_href(r.get("code", ""), "general-ledger", date_from, date_to),
                 cls="drilldown-link")),
            _num_cell(r.get("opening", 0)),
            _num_cell(r.get("debit", 0)),
            _num_cell(r.get("credit", 0)),
            _num_cell(r.get("closing", 0)),
        )
        for r in rows
    ]
    totals = data.get("totals") or {}
    body.append(Tr(
        Td(""),
        Td(Strong(t("th.total"))),
        _num_cell(totals.get("opening", 0), strong=True),
        _num_cell(totals.get("debit", 0), strong=True),
        _num_cell(totals.get("credit", 0), strong=True),
        _num_cell(totals.get("closing", 0), strong=True),
    ))
    # Summary chips above the table, in the same position as the other tabs.
    return Div(
        _totals_chips(float(totals.get("debit", 0) or 0), float(totals.get("credit", 0) or 0),
                      bool(data.get("balanced", True)), currency),
        Table(
            Thead(Tr(
                Th(t("th.code")),
                Th(t("th.account")),
                Th(t("th.opening"), cls="cell--number"),
                Th(t("th.debit"), cls="cell--number"),
                Th(t("th.credit"), cls="cell--number"),
                Th(t("th.closing"), cls="cell--number"),
            )),
            Tbody(*body),
            cls="data-table",
        ),
    )


# ── Statement of account ────────────────────────────────────────────────────

_SOA_KIND_KEYS = {
    "invoice": "label.invoice",
    "credit_note": "doc.credit_note",
    "payment": "acct.soa_kind_payment",
}


def _soa_kind_label(kind: str) -> str:
    key = _SOA_KIND_KEYS.get(kind)
    return t(key) if key else kind


def _soa_table(data: dict, currency: str | None = None,
               date_from: str = "", date_to: str = "") -> FT:
    def _fmt_nonzero(val: float) -> str:
        return fmt_money(val, currency) if val else ""

    def _bal_cell(val, strong: bool = False) -> FT:
        v = float(val or 0)
        cls = "cell--number cell--negative" if v < 0 else "cell--number"
        formatted = fmt_money(v, currency)
        return Td(Strong(formatted) if strong else formatted, cls=cls)

    rows = [Tr(
        Td(date_from, cls="cell--mono"),
        Td(""),
        Td(t("acct.opening_balance"), cls="cell--muted"),
        Td("", cls="cell--number"),
        Td("", cls="cell--number"),
        _bal_cell(data.get("opening_balance", 0)),
    )]
    for row in data.get("rows", []):
        ref = row.get("doc_ref") or row.get("doc_id") or ""
        ref_cell = (A(ref, href=f"/docs/{row['doc_id']}", cls="drilldown-link")
                    if row.get("doc_id") else ref)
        rows.append(Tr(
            Td(row.get("date", ""), cls="cell--mono"),
            Td(ref_cell),
            Td(_soa_kind_label(str(row.get("kind") or ""))),
            Td(_fmt_nonzero(float(row.get("debit") or 0)), cls="cell--number"),
            Td(_fmt_nonzero(float(row.get("credit") or 0)), cls="cell--number"),
            _bal_cell(row.get("balance", 0)),
        ))
    rows.append(Tr(
        Td(date_to, cls="cell--mono"),
        Td(""),
        Td(Strong(t("acct.closing_balance"))),
        Td("", cls="cell--number"),
        Td("", cls="cell--number"),
        _bal_cell(data.get("closing_balance", 0), strong=True),
    ))
    return Table(
        Thead(Tr(
            Th(t("th.date")),
            Th(t("th.ref")),
            Th(t("th.type")),
            Th(t("th.debit"), cls="cell--number"),
            Th(t("th.credit"), cls="cell--number"),
            Th(t("th.balance"), cls="cell--number"),
        )),
        Tbody(*rows),
        cls="data-table",
    )


def _soa_view(data: dict | None, contacts: list[dict], contact_id: str,
              currency: str | None = None, date_from: str = "", date_to: str = "",
              merged_from: str = "") -> FT:
    opts = [
        (c.get("id", ""), f"{c.get('name', '')} ({c.get('contact_type') or 'customer'})")
        for c in contacts if c.get("id")
    ]
    selector = Form(
        Label(t("label.contact"), cls="form-label"),
        searchable_select("contact_id", opts, value=contact_id,
                          placeholder=t("label.contact"), onchange="this.form.submit()"),
        Input(type="hidden", name="tab", value="soa"),
        Input(type="hidden", name="from", value=date_from),
        Input(type="hidden", name="to", value=date_to),
        method="get", action="/accounting",
        cls="flex-row gap-sm",
    )
    if not contact_id or data is None:
        return Div(selector, empty_state_cta(t("acct.soa_pick_contact")))
    # The requested contact was merged into this one, so the figures below are
    # not the contact that was asked for. Say so, on screen and on anything
    # printed from it (GDR 2d).
    notice = (Div(t("acct.soa_merged_notice"), cls="error-banner") if merged_from else None)
    return Div(selector, notice, _soa_table(data, currency, date_from, date_to))
