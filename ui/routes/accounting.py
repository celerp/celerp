# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The accounting workspace: the journal, and posting entries into it.

Reading the books happens under /reports; this module is the side you act on.
"""

from __future__ import annotations

import math as _math
import re
import json as _json
import uuid
import asyncio
from datetime import date as _date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, PlainTextResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.currency import CURRENCIES
from ui.components.report_kit import (
    action_bar, csv_response as _csv_response, date_params as _date_params,
    href as _href, journal_totals as _journal_totals,
    plain_error_response as _plain_error_response, print_shell as _print_shell,
)
from ui.components.journal import (
    journal_bulk_toolbar, journal_csv_rows, journal_export_name, journal_filter_bar,
    journal_filter_qs, journal_filter_words, journal_filters, journal_print_subtitle,
    journal_rows, journal_table, journal_void_toast,
)
from ui.components.table import (
    EMPTY, PARTY_PRELOAD, PARTY_SEARCH_URL, empty_state_cta, fmt_money, party_options,
    searchable_select,
)
from ui.config import get_token as _token, get_role as _get_role
from celerp.services.permissions import role_has_permission
from ui.i18n import t, get_lang
from ui.routes.documents import _action_error
from ui.routes.financial_reports import MOVED_TABS, REPORTS, ledger_path, report_nav
from ui.routes.reports import (
    _date_filter_bar, _get_fiscal, _page_or_fragment, _parse_dates,
)
from celerp.services.money import CURRENCY_DP, DEFAULT_DP, currency_dp


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
            filters = journal_filters(request)
            params = {**_date_params(d_from, d_to), **filters}
            data = await api.get_journal(token, params)
            # What the period is, in the terms the reader set it: a preset stays a
            # preset through a filter change, so the button they picked stays lit.
            carried = ({"preset": preset} if preset and preset != "custom"
                       else {"from": d_from, "to": d_to})
            content = Div(
                report_nav("journal", d_from or "", d_to or ""),
                _date_filter_bar("/accounting", d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company",
                                 extra_params=journal_filter_qs(filters)),
                journal_filter_bar("/accounting", filters, carried,
                                   get_lang(request)),
                _journal_totals(data, currency,
                                filter_words=journal_filter_words(filters, get_lang(request)),
                                clear_href=_href("/accounting", carried)),
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
                journal_bulk_toolbar(params, carried),
                journal_table(journal_rows(data, items=False), items=False, currency=currency,
                              params=params, select=True,
                              filtered=bool(data.get("filtered"))),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 403:
                content = Div(t("acct.not_authorized"), cls="error-banner")
            else:
                content = Div(f"{t('acct.error_loading_data')}: {e.detail}", cls="error-banner")

        # The journal answers HX-Request with the page body alone, like every other
        # report page, so a caller can swap it into #main-content.
        return await _page_or_fragment(
            request,
            page_header(t("page.accounting", get_lang(request))),
            content,
            title="Accounting - Celerp",
            nav_active="accounting",
        )

    async def _render_journal_form(request: Request, token: str, ts: str, memo: str,
                                   lines: list[dict], idem_token: str, error: str | None,
                                   date_from: str = "", date_to: str = ""):
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
        contacts: list[dict] = []
        contacts_total = 0
        try:
            accounts = (await api.get_chart(token)).get("items", [])
            base_currency = (await api.get_company(token)).get("currency") or ""
            # One page of parties to open on; typing searches the whole list
            # server-side, the same way the statement picker works.
            resp = await api.list_contacts(token, {"limit": PARTY_PRELOAD})
            contacts = resp.get("items") or []
            contacts_total = int(resp.get("total") or len(contacts))
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
                                base_currency=base_currency,
                                contacts=contacts, contacts_total=contacts_total),
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
            contact = str(form.get(f"contact_{i}") or "").strip()
            currency = str(form.get(f"currency_{i}") or "").strip().upper()
            rate = str(form.get(f"rate_{i}") or "").strip()
            if not (account or debit or credit or contact):
                continue  # an untouched blank row carries no intent
            lines.append({"account": account, "debit": debit, "credit": credit,
                          "contact": contact, "currency": currency, "rate": rate})

        error: str | None = None
        entries: list[dict] = []
        valid_codes = {c for c, _ in CURRENCIES}
        try:
            # A line with no currency posts exactly the request it posted before
            # the columns existed: no currency key, no rate key. Same for a line
            # nobody named and the contact key.
            entries = [
                {"account": l["account"], "debit": float(l["debit"] or 0), "credit": float(l["credit"] or 0),
                 **({"contact": l["contact"]} if l["contact"] else {}),
                 **({"currency": l["currency"]} if l["currency"] else {}),
                 **({"rate": float(l["rate"])} if l["rate"] else {})}
                for l in lines
            ]
            if any(not (_math.isfinite(e["debit"]) and _math.isfinite(e["credit"])) for e in entries):
                error = t("acct.err_amounts_numeric")
        except ValueError:
            error = t("acct.err_amounts_numeric")

        if error is None:
            for e in entries:
                if e.get("currency") and e["currency"] not in valid_codes:
                    error = t("acct.err_currency_unknown")
                    break
                r = e.get("rate")
                if r is not None and (not _math.isfinite(r) or r <= 0):
                    error = t("acct.err_rate_positive")
                    break

        if error is None:
            try:
                await api.create_journal_entry(token, {
                    "ts": ts,
                    "memo": memo,
                    "entries": entries,
                    "idempotency_token": idem_token,
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
                                          date_from=d_from, date_to=d_to)

    async def _journal_void_answer(request: Request, token: str, result: dict):
        """The answer to a void: the table it changed, its totals, and what happened.

        The figures are re-read from the API rather than adjusted here, so the totals
        over the rows cannot drift from the rows under them. They travel out of band
        because they sit above the table with the page's own furniture in between,
        and only these two regions changed: nothing else on the page has to move for
        a void, and the reader keeps the view they narrowed to.

        Which book asked is what `items` says, so a void fired from the extended
        journal comes back as the extended journal.
        """
        items = request.query_params.get("items") == "1"
        d_from = request.query_params.get("date_from", "")
        d_to = request.query_params.get("date_to", "")
        preset = request.query_params.get("preset", "")
        filters = journal_filters(request)
        params = {**_date_params(d_from, d_to), **filters}
        company = await api.get_company(token)
        currency = company.get("currency")
        data = (await api.get_extended_journal(token, params) if items
                else await api.get_journal(token, params))
        carried = ({"preset": preset} if preset and preset != "custom"
                   else {"from": d_from, "to": d_to})
        base = REPORTS["extended-journal"][0] if items else "/accounting"
        # Each book comes back the shape it went out in, so a void fired from the
        # extended journal returns the extended journal.
        table = journal_table(journal_rows(data, items=items), items=items,
                              currency=currency, params=params,
                              select=True, filtered=bool(data.get("filtered")))
        totals = _journal_totals(
            data, currency, oob=True,
            filter_words=journal_filter_words(filters, get_lang(request)),
            clear_href=_href(base, carried))
        return HTMLResponse(
            to_xml(table) + to_xml(totals),
            headers={"HX-Trigger": _json.dumps({"celerpToast": journal_void_toast(result)})},
        )

    @app.post("/accounting/journal/bulk-void")
    async def journal_bulk_void(request: Request):
        """Void every entry the reader ticked, and report what happened to each.

        The API decides what may be voided and answers per entry, so a refusal in
        the middle of a selection stops nothing: the entries around it are voided
        and the reader is told, in the words of the rule that refused it, which were
        not. The refreshed table shows them still standing unstruck.
        """
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            # Blanks, duplicates and an oversized selection are the API's rules to
            # apply; it refuses in its own words, which the toast then states.
            result = await api.bulk_void_journal_entries(
                token, form.getlist("selected"), reason)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _action_error(t("acct.not_authorized") if e.status == 403 else str(e.detail))
        return await _journal_void_answer(request, token, result)

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
            filters = journal_filters(request)
            params = {**_date_params(d_from, d_to), **filters}
            data = await api.get_journal(token, params)
        except APIError as e:
            return _plain_error_response(e)

        body = Div(
            _journal_totals(data, currency),
            journal_table(journal_rows(data, items=False), items=False, currency=currency,
                          filtered=bool(data.get("filtered"))),
        )
        # The filter is stated in the sheet's own header, next to the period, so a
        # page found on a desk later cannot be read as the whole book.
        return _print_shell(company, t("acct.tab_journal"),
                            journal_print_subtitle(d_from, d_to, filters), body)

    @app.get("/accounting/export/journal/csv")
    async def journal_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            filters = journal_filters(request)
            params = {**_date_params(d_from, d_to), **filters}
            company, data = await asyncio.gather(api.get_company(token), api.get_journal(token, params))
        except APIError as e:
            return _plain_error_response(e)

        fname = journal_export_name("journal", d_from, d_to, filters)
        return _csv_response(journal_csv_rows(data, items=False,
                                              currency=company.get("currency") or ""), fname)



# ── Manual journal entry form ───────────────────────────────────────────────

def _base_col(label: str, base_currency: str) -> str:
    """Column header for a book figure, naming the company currency when known.

    The typed debit and credit columns carry whatever each line names, so the
    converted columns have to say which currency they are the total in.
    """
    return f"{label} ({base_currency})" if base_currency else label


def _je_line_row(idx: str, line: dict, acct_opts: list[tuple[str, str]],
                 contact_opts: list[tuple[str, str]]) -> FT:
    esc = "if(event.key==='Escape'){this.blur();event.preventDefault();}"
    return Tr(
        Td(searchable_select(f"account_{idx}", acct_opts, value=line.get("account", ""),
                             placeholder=t("th.account"), cls_extra="cell-input")),
        Td(searchable_select(f"contact_{idx}", contact_opts, value=line.get("contact", ""),
                             placeholder=t("acct.je_line_party"), cls_extra="cell-input",
                             search_url=PARTY_SEARCH_URL)),
        Td(Input(type="number", name=f"debit_{idx}", value=line.get("debit", ""), step="any",
                 min="0", cls="cell-input", oninput="celerpJeTotals()",
                 onkeydown=esc), cls="cell--number"),
        Td(Input(type="number", name=f"credit_{idx}", value=line.get("credit", ""), step="any",
                 min="0", cls="cell-input", oninput="celerpJeTotals()",
                 onkeydown=esc), cls="cell--number"),
        Td(searchable_select(f"currency_{idx}", CURRENCIES, value=line.get("currency", ""),
                             placeholder=t("acct.currency_base_hint"), cls_extra="cell-input",
                             onchange="celerpJeFxFill(this)"),
           cls="je-fx-col"),
        Td(Input(type="number", name=f"rate_{idx}", value=line.get("rate", ""), step="any",
                 min="0", placeholder="1", cls="cell-input", oninput="celerpJeFxFill(this)",
                 onkeydown=esc), cls="cell--number je-fx-col"),
        # Read-only: the book figures are server-computed and must never be
        # request fields, or a client could post amounts unrelated to the rate.
        Td("", cls="cell--number cell--muted je-base-debit je-fx-col"),
        Td("", cls="cell--number cell--muted je-base-credit je-fx-col"),
        Td(Button("✕", type="button", cls="btn btn--ghost btn--sm", title=t("btn.remove"),
                  onclick="celerpJeRemoveLine(this)")),
    )


def _journal_entry_form(accounts: list[dict], ts: str, memo: str, lines: list[dict],
                        idem_token: str, error: str | None = None,
                        date_from: str = "", date_to: str = "",
                        base_currency: str = "",
                        contacts: list[dict] | None = None,
                        contacts_total: int = 0) -> FT:
    acct_opts = [
        (a.get("code", ""), f"{a.get('code', '')} {a.get('name', '')}".strip())
        for a in accounts
        if a.get("code") and a.get("is_active", True)
    ]
    contact_opts = party_options(contacts or [])
    if not lines:
        lines = [{}, {}]  # double-entry needs two sides, so start with two rows
    rows = [_je_line_row(str(i), line, acct_opts, contact_opts) for i, line in enumerate(lines)]

    any_fx = any(l.get("currency") for l in lines)
    js = f"""
var celerpJeIdx = {len(lines)};
var celerpJeBase = {_json.dumps(base_currency)};
// The server's ISO 4217 decimal places, so the preview rounds each amount at the
// same place the posting will and the two can never disagree.
var celerpJeDp = {_json.dumps(CURRENCY_DP)};
var celerpJeDefaultDp = {DEFAULT_DP};
function celerpJeDpOf(cur) {{
    var d = celerpJeDp[(cur || '').toUpperCase()];
    return d === undefined ? celerpJeDefaultDp : d;
}}
function celerpJeRound(v, cur) {{
    var p = Math.pow(10, celerpJeDpOf(cur));
    // toPrecision first: 1.005 * 100 is 100.49999999999999 in binary floating
    // point, and rounding that gives a different cent than the server's HALF_UP.
    return Math.round(Number((v * p).toPrecision(12))) / p;
}}
function celerpJeFmt(v, cur) {{
    return v.toLocaleString(undefined, {{minimumFractionDigits: celerpJeDpOf(cur),
                                        maximumFractionDigits: celerpJeDpOf(cur)}});
}}
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
    // The party picker searches server-side, and a cloned node carries its hx-
    // attributes as inert markup until htmx is told to bind them.
    if (window.htmx) htmx.process(tbody.lastElementChild);
    // An entry in one foreign currency should not be retyped once per line, so a
    // new row picks up the currency and rate already in use. It lands in the
    // row's own visible cells and can be changed there like any other line.
    var lead = celerpJeLeadFx();
    if (lead) celerpJeSpreadFx(lead.currency, lead.rate);
    celerpJeTotals();
}}
function celerpJeRemoveLine(btn) {{
    btn.closest('tr').remove();
    celerpJeTotals();
}}
function celerpJeRows() {{
    return Array.prototype.slice.call(document.querySelectorAll('#je-lines tr'));
}}
function celerpJeLineFx(row) {{
    // The currency picker writes through a hidden input, same as every combobox here.
    var curEl = row.querySelector('[name^="currency_"]');
    var rateEl = row.querySelector('[name^="rate_"]');
    return {{
        el: curEl, rateEl: rateEl,
        currency: curEl ? (curEl.value || '').toUpperCase() : '',
        rate: parseFloat(rateEl ? rateEl.value : '') || 0,
    }};
}}
function celerpJeLeadFx() {{
    var rows = celerpJeRows();
    for (var i = 0; i < rows.length; i++) {{
        var fx = celerpJeLineFx(rows[i]);
        if (fx.currency && fx.rate > 0) return fx;
    }}
    return null;
}}
function celerpJeSpreadFx(currency, rate) {{
    celerpJeRows().forEach(function(row) {{
        var fx = celerpJeLineFx(row);
        if (!fx.el || fx.currency) return;  // a line that names its own currency is left alone
        fx.el.value = currency;
        var text = fx.el.closest('.combobox-wrap').querySelector('.combobox-input');
        var opt = fx.el.closest('.combobox-wrap').querySelector('.combobox-option[data-value="' + currency + '"]');
        if (text) text.value = opt ? opt.textContent : currency;
        if (fx.rateEl && !fx.rateEl.value) fx.rateEl.value = rate;
    }});
}}
function celerpJeFxFill(el) {{
    var fx = celerpJeLineFx(el.closest('tr'));
    if (fx.currency && fx.rate > 0) celerpJeSpreadFx(fx.currency, fx.rate);
    celerpJeTotals();
}}
function celerpJeFxToggle(details) {{
    // Closing the reveal clears the foreign columns rather than hiding values
    // that would still post from cells the user can no longer see.
    if (!details.open) {{
        celerpJeRows().forEach(function(row) {{
            var fx = celerpJeLineFx(row);
            if (fx.el) {{
                fx.el.value = '';
                var text = fx.el.closest('.combobox-wrap').querySelector('.combobox-input');
                if (text) text.value = '';
            }}
            if (fx.rateEl) fx.rateEl.value = '';
        }});
    }}
    document.querySelectorAll('.je-fx-col').forEach(function(cell) {{ cell.hidden = !details.open; }});
    celerpJeTotals();
}}
function celerpJeTotals() {{
    var baseDp = celerpJeDpOf(celerpJeBase);
    var basePow = Math.pow(10, baseDp);
    // Totals accumulate in whole base units of account so that summing many lines
    // cannot drift the way repeated float addition does.
    var totalDebit = 0, totalCredit = 0;
    celerpJeRows().forEach(function(row) {{
        var dCell = row.querySelector('.je-base-debit');
        var cCell = row.querySelector('.je-base-credit');
        if (!dCell || !cCell) return;
        var fx = celerpJeLineFx(row);
        var cur = fx.currency || celerpJeBase;
        var rate = (fx.currency && fx.currency !== celerpJeBase) ? fx.rate : 1;
        var dv = celerpJeRound(parseFloat((row.querySelector('[name^="debit_"]') || {{}}).value) || 0, cur);
        var cv = celerpJeRound(parseFloat((row.querySelector('[name^="credit_"]') || {{}}).value) || 0, cur);
        // Each line converts and rounds on its own, exactly as the server does,
        // so the preview cannot disagree with what gets posted.
        var bd = rate > 0 ? celerpJeRound(dv * rate, celerpJeBase) : 0;
        var bc = rate > 0 ? celerpJeRound(cv * rate, celerpJeBase) : 0;
        totalDebit += Math.round(bd * basePow);
        totalCredit += Math.round(bc * basePow);
        dCell.textContent = bd ? celerpJeFmt(bd, celerpJeBase) : {_json.dumps(EMPTY)};
        cCell.textContent = bc ? celerpJeFmt(bc, celerpJeBase) : {_json.dumps(EMPTY)};
    }});

    var baseTag = celerpJeBase ? ' (' + celerpJeBase + ')' : '';
    var d = totalDebit / basePow, c = totalCredit / basePow;
    document.getElementById('je-total-debit').textContent =
        {_json.dumps(t("acct.total_debit"))} + baseTag + ': ' + celerpJeFmt(d, celerpJeBase);
    document.getElementById('je-total-credit').textContent =
        {_json.dumps(t("acct.total_credit"))} + baseTag + ': ' + celerpJeFmt(c, celerpJeBase);

    // Balance is judged on the converted figures, because those are what the
    // books hold. The server applies the same rule to the same numbers.
    var chip = document.getElementById('je-balance-chip');
    var balanced = totalDebit === totalCredit && totalDebit > 0;
    chip.textContent = balanced ? {_json.dumps(t("acct.balanced"))} : {_json.dumps(t("acct.out_of_balance"))};
    chip.className = balanced ? 'val-chip' : 'val-chip val-chip--alert';

    // Name the gap while it can still be fixed, so a rate that does not convert
    // to an even amount is the user's own line to write, not a silent posting.
    var note = document.getElementById('je-imbalance-note');
    var gap = Math.abs(totalDebit - totalCredit);
    if (gap && (totalDebit > 0 || totalCredit > 0)) {{
        var short = totalDebit > totalCredit ? {_json.dumps(t("th.credit"))} : {_json.dumps(t("th.debit"))};
        note.textContent = {_json.dumps(t("acct.imbalance_note"))}
            .replace('{{currency}}', celerpJeBase)
            .replace('{{side}}', short)
            .replace('{{amount}}', celerpJeFmt(gap / basePow, celerpJeBase));
    }} else {{
        note.textContent = '';
    }}
}}
function celerpJeInit() {{
    // One delegated listener rather than a handler per control, so anything
    // typed anywhere in the lines recomputes the book columns. Clearing a
    // line's currency back to blank is the case that needs it: the combobox
    // empties its hidden input without firing change, and a stale conversion
    // left on screen is worse than no conversion at all.
    document.getElementById('je-lines').addEventListener('input', celerpJeTotals);
    celerpJeFxToggle(document.getElementById('je-fx-reveal'));
}}
if (document.readyState === 'loading') {{ document.addEventListener('DOMContentLoaded', celerpJeInit); }} else {{ celerpJeInit(); }}
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
            # currency sees the same form as before and no extra decision. Open
            # it and each line gains its own currency and rate, so one entry can
            # carry more than one currency.
            Details(
                Summary(t("acct.record_foreign_currency"), cls="btn btn--ghost btn--sm"),
                P(t("acct.fx_per_line_hint"), cls="cell--muted"),
                id="je-fx-reveal",
                cls="je-fx-reveal",
                ontoggle="celerpJeFxToggle(this)",
                # Reopened when a currency survives a failed submission, so the
                # user sees the values they typed instead of a collapsed control
                # that looks like it was never touched.
                open=any_fx,
            ),
            Div("", id="je-imbalance-note", cls="report-section"),
            Template(_je_line_row("__IDX__", {}, acct_opts, contact_opts), id="je-line-tpl"),
            Table(
                Thead(Tr(
                    Th(t("th.account")),
                    Th(t("acct.je_line_party")),
                    Th(t("th.debit"), cls="cell--number"),
                    Th(t("th.credit"), cls="cell--number"),
                    Th(t("th.currency"), cls="je-fx-col"),
                    Th(t("th.rate"), cls="cell--number je-fx-col"),
                    Th(_base_col(t("th.debit"), base_currency), cls="cell--number je-fx-col"),
                    Th(_base_col(t("th.credit"), base_currency), cls="cell--number je-fx-col"),
                    Th(""),
                )),
                Tbody(*rows, id="je-lines"),
                cls="data-table",
            ),
            P(t("acct.je_party_hint"), cls="cell--muted"),
            # Say so when the picker opens on part of the contact list, so a short
            # list is never read as the whole one.
            (P(t("acct.picker_more_contacts", shown=len(contact_opts), total=contacts_total),
               cls="cell--muted")
             if contacts_total > len(contact_opts) else None),
            Div(
                Button(t("btn.add_line"), type="button", cls="btn btn--secondary btn--sm",
                       onclick="celerpJeAddLine()"),
                Div(
                    Span("", id="je-total-debit", cls="val-chip"),
                    Span("", id="je-total-credit", cls="val-chip"),
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

