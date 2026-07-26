# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Financial reports: P&L, balance sheet, trial balance, general ledger, cash flow
and statements of account.

These are the reports an accountant reads, so they live under /reports beside the
operational ones rather than inside the Accounting section, which is where entries
are posted. The accounting module registers them because it owns the data and the
figures; only the URL sits under /reports.
"""

from __future__ import annotations

import re
from datetime import date as _date

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import empty_state_cta, fmt_money, searchable_select
from ui.components.journal import journal_csv_rows, journal_rows, journal_table
from ui.components.report_kit import (
    action_bar, csv_response, date_params, fname_date, href, journal_totals,
    period_subtitle, plain_error_response, print_shell, report_header, totals_chips,
)
from ui.config import get_token as _token
from ui.i18n import t, get_lang
from celerp.services.money import to_decimal
from ui.routes.reports import (
    OPERATIONAL_REPORTS, _date_filter_bar, _get_fiscal, _parse_dates, _resolve_preset,
)

# Financial report cards on the /reports index, in reading order: the summary
# statements first, then the detail an accountant works down into.
_FINANCIAL_CARDS: list[tuple[str, str]] = [
    ("pnl", "rpt.pnl_desc"),
    ("balance-sheet", "rpt.balance_sheet_desc"),
    ("cash-flow", "rpt.cash_flow_desc"),
    ("trial-balance", "rpt.trial_balance_desc"),
    ("general-ledger", "rpt.general_ledger_desc"),
    ("journal", "rpt.journal_desc"),
    ("extended-journal", "rpt.extended_journal_desc"),
    ("statement", "rpt.statement_desc"),
]

# Report key -> (page path, print path, csv path, title key). One table so a report's
# three URLs and its name are declared once and every caller reads them from here.
# The journal is in the table though its page lives under /accounting: it is a
# financial report like the rest, an accountant looks for it beside them, and every
# sweep that reads this table (the index cards, the print and CSV checks, the
# goldens) has to cover it.
REPORTS: dict[str, tuple[str, str, str, str]] = {
    "pnl": ("/reports/pnl", "/reports/print/pnl", "/reports/export/pnl/csv", "acct.tab_pnl"),
    "balance-sheet": ("/reports/balance-sheet", "/reports/print/balance-sheet",
                      "/reports/export/balance-sheet/csv", "acct.tab_balance_sheet"),
    "trial-balance": ("/reports/trial-balance", "/reports/print/trial-balance",
                      "/reports/export/trial-balance/csv", "acct.tab_trial_balance"),
    "general-ledger": ("/reports/general-ledger", "/reports/print/general-ledger",
                       "/reports/export/general-ledger/csv", "acct.tab_general_ledger"),
    "cash-flow": ("/reports/cash-flow", "/reports/print/cash-flow",
                  "/reports/export/cash-flow/csv", "acct.tab_cash_flow"),
    "journal": ("/accounting", "/accounting/print/journal",
                "/accounting/export/journal/csv", "acct.tab_journal"),
    "extended-journal": ("/reports/extended-journal", "/reports/print/extended-journal",
                         "/reports/export/extended-journal/csv", "acct.tab_extended_journal"),
    "statement": ("/reports/statement", "/reports/print/statement",
                  "/reports/export/statement/csv", "acct.soa_title"),
}

# Old /accounting?tab=<name> -> the report key it became. Bookmarks and emailed
# links predate the move, so they are redirected rather than broken. Mapping to the
# key rather than the path keeps one source for each report's URL and its name;
# note the tab name and the report key differ for the statement.
MOVED_TABS: dict[str, str] = {
    "pnl": "pnl",
    "balance-sheet": "balance-sheet",
    "trial-balance": "trial-balance",
    "general-ledger": "general-ledger",
    "soa": "statement",
}


def ledger_path(account_code: str) -> str:
    """One account's ledger drilldown. Kept out of REPORTS because it takes the
    account in its path and has no print or CSV view of its own, but declared once
    here all the same so callers and redirects cannot disagree about where it is."""
    return f"/reports/ledger/{account_code}"


def _report_path(key: str) -> str:
    return REPORTS[key][0]


def _bars(key: str, params) -> FT:
    _page, print_path, csv_path, _title = REPORTS[key]
    return action_bar(print_path, csv_path, params)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _page(request: Request, *content, title: str) -> FT:
    """Full shell, or a bare fragment when HTMX asked for one.

    The operational reports already swap fragments, so the financial ones do too;
    a mix would reload the page for half the section and not the other half.
    """
    if _is_htmx(request):
        return Div(*content, id="main-content", cls="main-content")
    return await base_shell(*content, title=title, nav_active="reports", request=request)


def _error_content(e: APIError):
    if e.status == 403:
        return Div(t("acct.not_authorized"), cls="error-banner")
    return Div(f"{t('acct.error_loading_data')}: {e.detail}", cls="error-banner")


# ── Statement subjects ──────────────────────────────────────────────────────

_SUBJECT_KINDS = frozenset({"c", "a"})


def subject_token(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


def parse_subject(raw: str) -> tuple[str, str] | None:
    """"c:contact:9" -> ("c", "contact:9"); "a:1120" -> ("a", "1120").

    Split once and never again: contact ids contain their own colon, so any
    further splitting would truncate the id and look up the wrong party.
    Unknown kinds are dropped rather than guessed at.
    """
    kind, sep, ident = raw.partition(":")
    if not sep or kind not in _SUBJECT_KINDS or not ident:
        return None
    return kind, ident


def parse_subjects(request: Request) -> list[tuple[str, str]]:
    """Selected statement subjects, in the order given, without duplicates.

    Accepts the pre-move `contact_id` spelling so existing links keep working.
    """
    raw = list(request.query_params.getlist("account"))
    legacy = request.query_params.get("contact_id", "")
    if legacy and not raw:
        raw = [subject_token("c", legacy)]
    out: list[tuple[str, str]] = []
    for item in raw:
        parsed = parse_subject(item)
        if parsed and parsed not in out:
            out.append(parsed)
    return out


def _subject_params(subjects: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [("account", subject_token(k, i)) for k, i in subjects]


# Where the statement picker's search sends what the reader types.
SUBJECT_OPTIONS_PATH = "/reports/statement/subject-options"

# Contacts drawn into the picker before anything is typed. It is where the list
# opens, not the limit of what can be chosen: typing searches the whole contact
# list server-side. Loading every contact up front would put a company's entire
# contact list into every statement page load to save the first keystroke.
_PICKER_PRELOAD = 50

# Contacts per request when reading the list in full.
_CONTACT_PAGE = 200


def _contact_options(contacts: list[dict]) -> list[tuple[str, str]]:
    return [
        (subject_token("c", c["id"]),
         f"{c.get('name', '')} ({c.get('contact_type') or 'customer'})")
        for c in contacts if c.get("id")
    ]


def _account_options(accounts: list[dict]) -> list[tuple[str, str]]:
    return [
        (subject_token("a", a["code"]), f"{a.get('code', '')} {a.get('name', '')}".strip())
        for a in accounts if a.get("code")
    ]


async def _every_contact(token: str, params: dict) -> list[dict]:
    """Every contact matching params, read page by page to the end of the list.

    One capped request is what let the month-end run miss people: a cap does not
    show up in the response, so a company past it got a short list and nothing
    said so. Reading until a page comes back short has no cap to outgrow.
    """
    items: list[dict] = []
    while True:
        resp = await api.list_contacts(
            token, params | {"limit": _CONTACT_PAGE, "offset": len(items)})
        batch = resp.get("items") or []
        items += batch
        if len(batch) < _CONTACT_PAGE:
            return items


def _option_markup(opts: list[tuple[str, str]], selected: set[str]) -> str:
    """Combobox options in the shape the picker's JS reads.

    Search results replace the option list whole, so each option has to carry
    whether it is already picked. An option that arrives unmarked reads as a way
    to add a subject that is in fact already on the statement, and clicking it
    would take that subject off.
    """
    els = [
        Div(label,
            cls="combobox-option" + (" combobox-option--selected" if val in selected else ""),
            data_value=val)
        for val, label in opts
    ]
    els.append(Div(t("msg.no_results"), cls="combobox-option combobox-option--empty",
                   **({"style": "display:none"} if opts else {})))
    return "".join(to_xml(el) for el in els)


def _option_notice(message: str) -> str:
    """A line of text in place of options, which the picker cannot select.

    An empty list would read as "nobody matched", so a search that could not run
    says what happened instead of answering with a result it does not have.
    """
    return to_xml(Div(message, cls="combobox-option combobox-option--empty"))


# Reports a ledger drilldown can arrive from, via the "src" query param.
LEDGER_ORIGINS = frozenset({"pnl", "balance-sheet", "trial-balance", "general-ledger"})


def _ledger_context(request: Request, fy: str) -> tuple[str, str, str, str]:
    """Resolve (origin, date_from, date_to, preset) for the ledger drilldown.

    Drilldown links from the reports name their originating report in "src" and
    carry their dates as date_from/date_to. "src" stays outside the date bar's
    reserved from/to/preset fields, so the custom-range form's hidden-param
    carry-through preserves the origin across date changes on the drilldown page.
    """
    qp = request.query_params
    origin = qp.get("src", "")
    if origin in LEDGER_ORIGINS:
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


# ── Views ───────────────────────────────────────────────────────────────────

def _drill_ledger_href(code: str, origin: str, date_from: str = "", date_to: str = "") -> str:
    """Link to the per-account ledger, carrying the date range and the originating report."""
    return href(ledger_path(code),
                [("date_from", date_from), ("date_to", date_to), ("src", origin)])


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


def _trial_balance_summary(tb: dict, currency: str | None = None) -> FT:
    return totals_chips(tb.get("total_debit", 0), tb.get("total_credit", 0),
                        tb.get("balanced", True), currency)


def _ledger_rows(data: dict) -> list[dict]:
    """Ledger lines in the statement's row shape.

    A line and a statement row differ only in naming; the memo is what a manual
    entry carries where a document has a type.
    """
    return [
        {
            "date": l.get("date", ""),
            "doc_id": l.get("doc_id"),
            "doc_ref": l.get("doc_ref"),
            "kind": l.get("memo", ""),
            "debit": l.get("debit", 0),
            "credit": l.get("credit", 0),
            "balance": l.get("balance", 0),
        }
        for l in data.get("lines", [])
    ]


def _ledger_view(data: dict, currency: str | None = None,
                 date_from: str = "", date_to: str = "") -> FT:
    """One account's ledger, drawn by the same table as that account's statement.

    The running balance carries on from what the account already held, so the
    opening figure has to be on the page: without it the first row's balance
    appears from nowhere and cannot be checked against the general ledger.
    """
    if not data.get("lines"):
        return P(t("acct.no_entries_for_account"), cls="empty-state")
    return _soa_table({**data, "rows": _ledger_rows(data)}, currency,
                      date_from=date_from, date_to=date_to)


def _pnl_view(data: dict, currency: str | None = None, date_from: str = "", date_to: str = "") -> FT:
    rev_total = float(data.get("revenue", {}).get("total", 0) or 0)

    _dqs = href("", [("date_from", date_from), ("date_to", date_to)])

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
        return href(_report_path("pnl"), [("date_to", as_of)])

    def _tb_href() -> str:
        return href(_report_path("trial-balance"), [("as_of", as_of)])

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


def _general_ledger_view(data: dict, currency: str | None = None,
                         date_from: str = "", date_to: str = "") -> FT:
    rows = data.get("rows", [])
    if not rows:
        return P(t("acct.no_journal_entries"), cls="empty-state")
    totals = data.get("totals", {})

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
    body.append(Tr(
        Td(""),
        Td(Strong(t("label.total"))),
        _num_cell(totals.get("opening", 0), strong=True),
        _num_cell(totals.get("debit", 0), strong=True),
        _num_cell(totals.get("credit", 0), strong=True),
        _num_cell(totals.get("closing", 0), strong=True),
    ))
    return Table(
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
    )


# The statuses worth a sentence, and the sentence each one gets. `expanded` needs
# none, and `no_items` must not have one: an entry that never had items is not an
# entry whose items were withheld, and listing it sends a reader looking for
# detail that was never meant to exist.
_WITHHELD_KEYS = (("untied", "acct.items_untied"),
                  ("no_document", "acct.items_no_document"))


def _unexpanded_note(data: dict) -> FT | None:
    """Names the entries whose item detail could not be shown, and why.

    An accountant reading a report about items has to be able to tell an entry
    with no items from one whose items were withheld, so the ones that were
    withheld are listed rather than silently rendered like the rest, under the
    reason they were withheld: figures that do not tie is a judgement about the
    numbers, a document that is gone is a fact about the books, and they are acted
    on differently.
    """
    entries = data.get("entries", [])
    sentences = []
    for status, key in _WITHHELD_KEYS:
        ids = [e.get("je_id", "") for e in entries if e.get("items_status") == status]
        if ids:
            sentences.append(Div(f"{t(key)} {', '.join(ids)}"))
    if not sentences:
        return None
    return Div(*sentences, cls="report-section cell--muted")


# ── Statement of account ────────────────────────────────────────────────────

# Every kind a statement row can carry: the document types that post to a control
# account, a payment against one of them, and a journal entry that reached the
# account with no document behind it. A kind with no entry here would print its
# raw key, so the API's kinds and this map are changed together.
_SOA_KIND_KEYS = {
    "invoice": "label.invoice",
    "credit_note": "doc.credit_note",
    "bill": "label.vendor_bill",
    "purchase_order": "label.purchase_order",
    "payment": "acct.soa_kind_payment",
    "journal": "acct.soa_kind_journal",
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


def _statement_source_note() -> FT:
    """Says which books a statement was built from.

    Every statement, whether its subject is a party or an account, is a slice of
    the posted journal entries. The reader is told so on the page: it is what
    makes the figures on a statement the same figures the balance sheet reports,
    and it means an entry posted by hand against a control account is there too.
    """
    return P(t("acct.soa_source_ledger"), cls="cell--muted")


def _subject_heading(kind: str, data: dict) -> str:
    if kind == "c":
        contact = data.get("contact") or {}
        name = contact.get("name", "")
        ctype = contact.get("type", "")
        return f"{name} ({ctype})" if ctype else name
    return f"{data.get('account_code', '')} {data.get('account_name', '')}".strip()


def _soa_section(kind: str, data: dict, currency: str | None,
                 d_from: str, d_to: str, merged: bool = False) -> FT:
    """One subject's statement: heading, source note, table, closing balance."""
    return Div(
        H3(_subject_heading(kind, data), cls="report-section-title"),
        (Div(t("acct.soa_merged_notice"), cls="error-banner") if merged else None),
        _statement_source_note(),
        _soa_table(data, currency, date_from=d_from, date_to=d_to),
        P(Strong(f"{t('acct.closing_balance')}: {fmt_money(data.get('closing_balance', 0), currency)}"),
          cls="report-total"),
        cls="report-section",
    )


def _soa_summary(sections: list[dict], currency: str | None) -> FT | None:
    """Closing balance per selected subject, above the stack.

    No combined total: a running balance that mixes a customer with an expense
    account, or two unrelated customers, is a number with no meaning. The reader
    gets each figure and adds the ones that belong together.
    """
    if len(sections) < 2:
        return None
    return Div(
        Table(
            Thead(Tr(Th(t("label.account")), Th(t("acct.closing_balance"), cls="cell--number"))),
            Tbody(*[
                Tr(
                    Td(_subject_heading(s["kind"], s["data"])),
                    Td(fmt_money(s["data"].get("closing_balance", 0), currency), cls="cell--number"),
                )
                for s in sections
            ]),
            cls="data-table data-table--compact",
        ),
        cls="report-section",
    )


def _cash_flow_view(data: dict, currency: str | None = None) -> FT:
    """Both methods side by side, with the reconciliation that ties them."""
    def _amount_cell(val) -> FT:
        v = float(val or 0)
        cls = "cell--number cell--negative" if v < 0 else "cell--number"
        return Td(fmt_money(v, currency), cls=cls)

    def _head(first_col: str) -> FT:
        """The figures are money, so the header sits right-aligned over them."""
        return Thead(Tr(Th(first_col), Th(t("label.amount"), cls="cell--number")))

    def _section(title: str, section: dict) -> FT:
        lines = section.get("lines", [])
        rows = [
            Tr(Td(f"{l.get('code', '')} {l.get('name', '')}".strip()), _amount_cell(l.get("amount")))
            for l in lines
        ]
        return Div(
            H3(title, cls="report-section-title"),
            (Table(_head(t("th.account")), Tbody(*rows), cls="data-table data-table--compact")
             if rows else P(t("acct.no_entries"), cls="empty-state")),
            P(Strong(fmt_money(section.get("total", 0), currency)), cls="section-total"),
            cls="report-section",
        )

    direct = data.get("direct", {})
    indirect = data.get("indirect", {})
    adjustments = indirect.get("adjustments", [])
    balanced = data.get("balanced", True)
    unbacked = data.get("unbacked_bank_openings", [])
    return Div(
        Div(
            Span(f"{t('acct.cf_opening_cash')}: {fmt_money(data.get('opening_cash', 0), currency)}", cls="val-chip"),
            Span(f"{t('acct.cf_net_change')}: {fmt_money(data.get('net_change', 0), currency)}", cls="val-chip"),
            Span(f"{t('acct.cf_closing_cash')}: {fmt_money(data.get('closing_cash', 0), currency)}", cls="val-chip"),
            Span(t("acct.balanced") if balanced else t("acct.cf_methods_disagree"),
                 cls="val-chip" if balanced else "val-chip val-chip--alert"),
            cls="valuation-bar",
        ),
        # Named, not counted: the reader has to know which account to go and fix.
        *([Div(
            P(t("acct.cf_unbacked_opening")),
            Ul(*[Li(f"{u.get('bank_name', '')} ({u.get('chart_account_code', '')}): "
                    f"{fmt_money(u.get('opening_balance', 0), currency)}")
                 for u in unbacked]),
            cls="flash flash--warning",
        )] if unbacked else []),
        H2(t("acct.cf_direct"), cls="report-section-title"),
        _section(t("acct.cf_operating"), direct.get("operating", {})),
        _section(t("acct.cf_investing"), direct.get("investing", {})),
        _section(t("acct.cf_financing"), direct.get("financing", {})),
        H2(t("acct.cf_indirect"), cls="report-section-title"),
        Div(
            Table(
                # Net profit heads this table and the rest are adjustments to it,
                # so the first column is what the line is, not an account.
                _head(t("th.description")),
                Tbody(
                    Tr(Td(t("acct.net_profit")), _amount_cell(indirect.get("net_profit", 0))),
                    *[Tr(Td(f"{a.get('code', '')} {a.get('name', '')}".strip()),
                         _amount_cell(a.get("amount"))) for a in adjustments],
                ),
                cls="data-table data-table--compact",
            ),
            P(Strong(fmt_money(indirect.get("total", 0), currency)), cls="section-total"),
            cls="report-section",
        ),
        cls="report-view",
    )


# ── Routes ──────────────────────────────────────────────────────────────────

def setup_routes(app):

    async def _dates(request: Request, token: str):
        fy = await _get_fiscal(token)
        return _parse_dates(request, fy)

    @app.get("/reports")
    async def reports_index(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        cards = [(REPORTS[k][0], t(REPORTS[k][3], lang), t(desc, lang))
                 for k, desc in _FINANCIAL_CARDS]
        # Operational reports come from the reports module. It is optional, so the
        # index lists only the ones whose routes are actually registered instead of
        # advertising links that would 404.
        registered = {getattr(r, "path", "") for r in app.routes}
        cards += [(path, t(name, lang), t(desc, lang))
                  for path, name, desc in OPERATIONAL_REPORTS
                  if path.split("?")[0] in registered]
        content = Div(
            *[A(Strong(name), P(desc, cls="quick-link-desc"), href=path, cls="quick-link-card")
              for path, name, desc in cards],
            cls="quick-links-grid",
        )
        return await _page(request, page_header(t("page.reports", lang)), content,
                           title="Reports - Celerp")

    @app.get("/reports/pnl")
    async def pnl_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from, d_to, preset = await _dates(request, token)
            params = date_params(d_from, d_to)
            data = await api.get_pnl(token, params)
            content = Div(
                _date_filter_bar(_report_path("pnl"), d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company"),
                Div(_bars("pnl", params), cls="flex-row mt-md mb-md"),
                _pnl_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        return await _page(request, page_header(t("acct.tab_pnl", get_lang(request))), content,
                           title="Profit & Loss - Celerp")

    @app.get("/reports/balance-sheet")
    async def balance_sheet_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        as_of = request.query_params.get("as_of", "") or _date.today().isoformat()
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            data = await api.get_balance_sheet(token, {"as_of": as_of} if as_of else {})
            as_of_form = Form(
                Label(t("label.as_of_date"), cls="form-label"),
                Input(type="date", name="as_of", value=as_of, cls="date-input"),
                Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
                action=_report_path("balance-sheet"), method="get", cls="date-custom-form",
            )
            content = Div(
                Div(as_of_form, cls="date-filter-bar"),
                Div(_bars("balance-sheet", {"as_of": as_of}), cls="flex-row mt-md mb-md"),
                _balance_sheet_view(data, currency, as_of=as_of),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        return await _page(request, page_header(t("acct.tab_balance_sheet", get_lang(request))),
                           content, title="Balance Sheet - Celerp")

    @app.get("/reports/trial-balance")
    async def trial_balance_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from, d_to, preset = await _dates(request, token)
            params = date_params(d_from, d_to)
            data = await api.get_trial_balance(token, params)
            content = Div(
                _date_filter_bar(_report_path("trial-balance"), d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company"),
                _trial_balance_summary(data, currency),
                Div(_bars("trial-balance", params), cls="flex-row mt-md mb-md"),
                _trial_balance_table(data, currency, date_from=d_from or "", date_to=d_to or ""),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        return await _page(request, page_header(t("acct.tab_trial_balance", get_lang(request))),
                           content, title="Trial Balance - Celerp")

    @app.get("/reports/general-ledger")
    async def general_ledger_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from, d_to, preset = await _dates(request, token)
            params = date_params(d_from, d_to)
            data = await api.get_general_ledger(token, params)
            totals = data.get("totals") or {}
            content = Div(
                _date_filter_bar(_report_path("general-ledger"), d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company"),
                totals_chips(float(totals.get("debit", 0) or 0),
                             float(totals.get("credit", 0) or 0),
                             bool(data.get("balanced", True)), currency),
                Div(_bars("general-ledger", params), cls="flex-row mt-md mb-md"),
                _general_ledger_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        return await _page(request, page_header(t("acct.tab_general_ledger", get_lang(request))),
                           content, title="General Ledger - Celerp")

    @app.get("/reports/cash-flow")
    async def cash_flow_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from, d_to, preset = await _dates(request, token)
            params = date_params(d_from, d_to)
            data = await api.get_cash_flow(token, params)
            content = Div(
                _date_filter_bar(_report_path("cash-flow"), d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company"),
                Div(_bars("cash-flow", params), cls="flex-row mt-md mb-md"),
                _cash_flow_view(data, currency),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        return await _page(request, page_header(t("acct.tab_cash_flow", get_lang(request))),
                           content, title="Cash Flow - Celerp")

    @app.get("/reports/extended-journal")
    async def extended_journal_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from, d_to, preset = await _dates(request, token)
            params = date_params(d_from, d_to)
            data = await api.get_extended_journal(token, params)
            content = Div(
                _date_filter_bar(_report_path("extended-journal"), d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company"),
                journal_totals(data, currency),
                Div(_bars("extended-journal", params), cls="flex-row mt-md mb-md"),
                _unexpanded_note(data),
                journal_table(journal_rows(data, items=True), items=True, currency=currency),
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        return await _page(request, page_header(t("acct.tab_extended_journal", get_lang(request))),
                           content, title="Extended Journal - Celerp")

    @app.get("/reports/ledger/{account_code}")
    async def account_ledger_page(request: Request, account_code: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            fy = await _get_fiscal(token)
            origin, d_from, d_to, preset = _ledger_context(request, fy)
            data = await api.get_ledger(token, account_code, date_params(d_from, d_to))
            company = await api.get_company(token)
            currency = company.get("currency")
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _page(request, _error_content(e), title="Ledger - Celerp")

        back_path, _p, _c, back_key = REPORTS[origin]
        back_qs = ([("as_of", d_to)] if origin == "balance-sheet"
                   else [("from", d_from), ("to", d_to)])
        content = Div(
            Div(
                _date_filter_bar(ledger_path(account_code), d_from, d_to, preset,
                                 settings_link="/settings/general?tab=company",
                                 extra_params=f"&src={origin}"),
                A(f"← {t(back_key)}", href=href(back_path, back_qs), cls="btn btn--ghost btn--sm"),
                cls="flex-row flex-between",
            ),
            _ledger_view(data, currency, date_from=d_from or "", date_to=d_to or ""),
        )
        name = data.get("account_name", account_code)
        return await _page(request, page_header(f"Ledger: {account_code} {name}"), content,
                           title=f"Ledger {account_code} - Celerp")

    # ── Statement of account ───────────────────────────────────────────────

    async def _fetch_subject(token: str, kind: str, ident: str, params: dict) -> dict:
        """One subject's statement data, normalised so both kinds render alike."""
        if kind == "a":
            data = await api.get_ledger(token, ident, params)
            data["rows"] = _ledger_rows(data)
            return data
        return await api.get_soa(token, ident, params)

    async def _resolve_statements(token: str, subjects: list[tuple[str, str]], params: dict):
        """Fetch every selected subject, following contact merges per selection.

        A merged contact is substituted individually and the substitution is
        disclosed on that section alone. Redirecting the whole page, as the single
        subject view used to, would silently rewrite the other selections; and a
        winner already selected in its own right is rendered once, not twice.
        """
        sections: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for kind, ident in subjects:
            data = await _fetch_subject(token, kind, ident, params)
            merged = False
            if kind == "c":
                winner = data.get("merged_into")
                if winner:
                    merged = True
                    if ("c", winner) in seen or ("c", winner) in subjects:
                        continue
                    ident = winner
                    data = await _fetch_subject(token, "c", winner, params)
            if (kind, ident) in seen:
                continue
            seen.add((kind, ident))
            sections.append({"kind": kind, "ident": ident, "data": data, "merged": merged})
        return sections

    async def _subject_options(token: str) -> tuple[list[tuple[str, str]], int, int]:
        """The picker's opening list, with how much of the contact list it covers.

        Contacts come one page at a time and the rest are reached by typing; the
        chart of accounts comes whole, being a bounded list a company keeps in the
        dozens. The two counts are returned so the page can say when it is showing
        part of the contact list rather than leaving the reader to find out.
        """
        resp = await api.list_contacts(token, {"limit": _PICKER_PRELOAD})
        contacts = resp.get("items") or []
        accounts = (await api.get_chart(token)).get("items") or []
        opts = _contact_options(contacts) + _account_options(accounts)
        return opts, len(contacts), int(resp.get("total") or len(contacts))

    @app.get(SUBJECT_OPTIONS_PATH)
    async def statement_subject_options(request: Request):
        """The picker's search: contacts and chart accounts matching what was typed.

        The picker opens on one page of contacts, so this is what keeps a company
        past that page able to select any of its own customers. Every match is
        returned rather than a first page of them, because a search that stopped
        early would put the truncation back one step further in.

        An empty query returns an empty body, which is the picker's signal to put
        its own opening list back.
        """
        token = _token(request)
        if not token:
            return HTMLResponse(_option_notice(t("error.session_expired")))
        q = request.query_params.get("q", "").strip()
        if not q:
            return HTMLResponse("")
        selected = set(request.query_params.getlist("account"))
        try:
            contacts = await _every_contact(token, {"q": q})
            accounts = (await api.get_chart(token)).get("items") or []
        except APIError as e:
            return HTMLResponse(_option_notice(
                f"{t('acct.error_loading_data')}: {e.detail}"))
        needle = q.lower()
        opts = _contact_options(contacts) + [
            (val, label) for val, label in _account_options(accounts)
            if needle in label.lower()
        ]
        return HTMLResponse(_option_markup(opts, selected))

    async def _quick_pick(token: str, pick: str) -> list[tuple[str, str]]:
        """Expand a quick-pick into the subjects it stands for.

        The month-end run is "everyone who owes us", which is a set nobody wants to
        tick by hand. Kept as buttons beside the picker rather than entries inside
        it, so one control never mixes choosing with acting.

        "All customers" reads the customer list in full instead of reusing the
        picker's opening page. A run that quietly stopped at the first page would
        leave customers unbilled with nothing on the page saying so, and who counts
        as a customer is the contacts module's answer to give, not this page's.
        """
        if pick == "customers":
            contacts = await _every_contact(token, {"contact_type": "customer"})
            return [("c", c["id"]) for c in contacts if c.get("id")]
        if pick in ("ar_balance", "ap_balance"):
            aging = (await api.get_ar_aging(token) if pick == "ar_balance"
                     else await api.get_ap_aging(token))
            key = "customer_id" if pick == "ar_balance" else "supplier_id"
            return [("c", l[key]) for l in aging.get("lines", [])
                    if l.get(key) and float(l.get("total") or 0)]
        return []

    def _quick_pick_buttons(d_from: str, d_to: str) -> FT:
        picks = [
            ("ar_balance", t("acct.soa_pick_ar_balance")),
            ("ap_balance", t("acct.soa_pick_ap_balance")),
            ("customers", t("acct.soa_pick_all_customers")),
        ]
        return Div(
            *[A(label,
                href=href(_report_path("statement"),
                          [("pick", key), ("from", d_from), ("to", d_to)]),
                cls="btn btn--ghost btn--sm")
              for key, label in picks],
            cls="picker-quickpicks",
        )

    @app.get("/reports/statement")
    async def statement_report(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
            d_from, d_to, preset = await _dates(request, token)
            params = date_params(d_from, d_to)
            opts, shown, total_contacts = await _subject_options(token)
            pick = request.query_params.get("pick", "")
            subjects = (await _quick_pick(token, pick) if pick
                        else parse_subjects(request))
            sections = await _resolve_statements(token, subjects, params) if subjects else []
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _page(request, page_header(t("acct.soa_title", get_lang(request))),
                               _error_content(e), title="Statement of Account - Celerp")

        selected = [subject_token(k, i) for k, i in subjects]
        picker = Form(
            Label(t("label.account"), cls="form-label"),
            searchable_select("account", opts, multiple=True, values=selected,
                              placeholder=t("acct.soa_pick_placeholder"),
                              count_label=t("acct.soa_selected_count"),
                              search_url=href(SUBJECT_OPTIONS_PATH,
                                              [("account", s) for s in selected])),
            Input(type="hidden", name="from", value=d_from or ""),
            Input(type="hidden", name="to", value=d_to or ""),
            Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
            method="get", action=_report_path("statement"), cls="flex-row gap-sm",
        )
        # Say so when the list on screen is part of the contact list, so nobody
        # reads a short list as the whole one.
        more_note = (
            P(t("acct.picker_more_contacts", shown=shown, total=total_contacts),
              cls="cell--muted")
            if total_contacts > shown else None
        )
        selector = Div(picker, more_note, _quick_pick_buttons(d_from or "", d_to or ""))

        if not sections:
            body = empty_state_cta(t("acct.soa_pick_contact"))
        else:
            body = Div(
                _soa_summary(sections, currency),
                *[_soa_section(s["kind"], s["data"], currency, d_from or "", d_to or "",
                               merged=s["merged"]) for s in sections],
            )
        content = Div(
            _date_filter_bar(_report_path("statement"), d_from, d_to, preset,
                             settings_link="/settings/general?tab=company",
                             extra_params="".join(f"&account={subject_token(k, i)}"
                                                  for k, i in subjects)),
            selector,
            # Pairs, not a mapping: one key per subject is what a statement over
            # several subjects sends, and a mapping keeps only the last of them.
            (Div(_bars("statement", list(date_params(d_from, d_to).items())
                       + _subject_params(subjects)),
                 cls="flex-row mt-md mb-md") if sections else None),
            body,
        )
        return await _page(request, page_header(t("acct.soa_title", get_lang(request))),
                           content, title="Statement of Account - Celerp")

    # ── Print ──────────────────────────────────────────────────────────────

    async def _print_ctx(request: Request, token: str):
        company = await api.get_company(token)
        d_from = request.query_params.get("date_from", "")
        d_to = request.query_params.get("date_to", "")
        return company, company.get("currency"), d_from, d_to

    @app.get("/reports/print/pnl")
    async def pnl_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company, currency, d_from, d_to = await _print_ctx(request, token)
            data = await api.get_pnl(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        return print_shell(company, t("acct.tab_pnl"), period_subtitle(d_from, d_to),
                           _pnl_view(data, currency, date_from=d_from, date_to=d_to))

    @app.get("/reports/print/balance-sheet")
    async def balance_sheet_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        as_of = request.query_params.get("as_of", "")
        try:
            company = await api.get_company(token)
            data = await api.get_balance_sheet(token, {"as_of": as_of} if as_of else {})
        except APIError as e:
            return plain_error_response(e)
        return print_shell(company, t("acct.tab_balance_sheet"),
                           f"As of: {as_of}" if as_of else "",
                           _balance_sheet_view(data, company.get("currency"), as_of=as_of))

    @app.get("/reports/print/trial-balance")
    async def trial_balance_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company, currency, d_from, d_to = await _print_ctx(request, token)
            data = await api.get_trial_balance(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        body = Div(_trial_balance_summary(data, currency),
                   _trial_balance_table(data, currency, date_from=d_from, date_to=d_to))
        return print_shell(company, t("acct.tab_trial_balance"),
                           period_subtitle(d_from, d_to), body)

    @app.get("/reports/print/general-ledger")
    async def general_ledger_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company, currency, d_from, d_to = await _print_ctx(request, token)
            data = await api.get_general_ledger(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        return print_shell(company, t("acct.tab_general_ledger"), period_subtitle(d_from, d_to),
                           _general_ledger_view(data, currency, date_from=d_from, date_to=d_to))

    @app.get("/reports/print/cash-flow")
    async def cash_flow_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company, currency, d_from, d_to = await _print_ctx(request, token)
            data = await api.get_cash_flow(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        return print_shell(company, t("acct.tab_cash_flow"), period_subtitle(d_from, d_to),
                           _cash_flow_view(data, currency))

    @app.get("/reports/print/extended-journal")
    async def extended_journal_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company, currency, d_from, d_to = await _print_ctx(request, token)
            data = await api.get_extended_journal(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        body = Div(journal_totals(data, currency), _unexpanded_note(data),
                   journal_table(journal_rows(data, items=True), items=True,
                                 currency=currency))
        return print_shell(company, t("acct.tab_extended_journal"),
                           period_subtitle(d_from, d_to), body)

    @app.get("/reports/print/statement")
    async def statement_print(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        subjects = parse_subjects(request)
        if not subjects:
            return RedirectResponse(_report_path("statement"), status_code=302)
        try:
            company, currency, d_from, d_to = await _print_ctx(request, token)
            sections = await _resolve_statements(token, subjects, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        subtitle = period_subtitle(d_from, d_to)
        # One subject per printed page: these get handed out separately.
        pages = [
            Div(
                report_header(company, t("acct.soa_title"), subtitle),
                _soa_section(s["kind"], s["data"], currency, d_from, d_to, merged=s["merged"]),
                cls="soa-page",
            )
            for s in sections
        ]
        return print_shell(company, t("acct.soa_title"), subtitle, Div(*pages), header=False)

    # ── CSV ────────────────────────────────────────────────────────────────

    @app.get("/reports/export/trial-balance/csv")
    async def trial_balance_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            data = await api.get_trial_balance(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        rows = [["Code", "Account", "Type", "Debit", "Credit", "Net"]]
        rows += [[l.get("code", ""), l.get("name", ""), l.get("account_type", ""),
                  l.get("total_debit", 0), l.get("total_credit", 0), l.get("net", 0)]
                 for l in data.get("lines", [])]
        rows.append(["", "TOTAL", "", data.get("total_debit", 0), data.get("total_credit", 0), ""])
        return csv_response(rows, f"trial_balance_{fname_date(d_from)}_{fname_date(d_to)}.csv")

    @app.get("/reports/export/general-ledger/csv")
    async def general_ledger_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            # One call: the backend buckets summary and detail together, so the
            # export can never disagree with the on-screen general ledger.
            data = await api.get_general_ledger(
                token, {**date_params(d_from, d_to), "include_lines": "1"})
        except APIError as e:
            return plain_error_response(e)
        rows: list[list] = [["account_code", "account_name", "date", "source_ref",
                             "memo", "debit", "credit", "balance"]]
        for row in data.get("rows", []):
            code, name = row.get("code", ""), row.get("name", "")
            debit_normal = row.get("debit_normal")
            opening = row.get("opening", 0)
            rows.append([code, name, "", "", "Opening balance", "", "", opening])
            running = to_decimal(opening)
            for line in row.get("lines", []):
                debit = to_decimal(line.get("debit") or 0)
                credit = to_decimal(line.get("credit") or 0)
                running += (debit - credit) if debit_normal else (credit - debit)
                rows.append([code, name, line.get("date", ""), line.get("source_ref") or "",
                             line.get("memo", ""), float(debit), float(credit), float(running)])
            rows.append([code, name, "", "", "Closing balance", "", "", row.get("closing", 0)])
        return csv_response(rows, f"general_ledger_{fname_date(d_from)}_{fname_date(d_to)}.csv")

    @app.get("/reports/export/extended-journal/csv")
    async def extended_journal_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            data = await api.get_extended_journal(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        return csv_response(journal_csv_rows(data, items=True),
                            f"extended_journal_{fname_date(d_from)}_{fname_date(d_to)}.csv")

    @app.get("/reports/export/cash-flow/csv")
    async def cash_flow_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            data = await api.get_cash_flow(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        rows = [["Section", "Code", "Account", "Amount"]]
        for cat in ("operating", "investing", "financing"):
            section = data.get("direct", {}).get(cat, {})
            for l in section.get("lines", []):
                rows.append([cat, l.get("code", ""), l.get("name", ""), l.get("amount", 0)])
            rows.append([cat, "", "TOTAL", section.get("total", 0)])
        rows.append(["", "", "NET CHANGE IN CASH", data.get("net_change", 0)])
        rows.append(["indirect", "", "Net profit", data.get("indirect", {}).get("net_profit", 0)])
        for a in data.get("indirect", {}).get("adjustments", []):
            rows.append(["indirect", a.get("code", ""), a.get("name", ""), a.get("amount", 0)])
        rows.append(["indirect", "", "TOTAL", data.get("indirect", {}).get("total", 0)])
        return csv_response(rows, f"cash_flow_{fname_date(d_from)}_{fname_date(d_to)}.csv")

    @app.get("/reports/export/statement/csv")
    async def statement_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        subjects = parse_subjects(request)
        if not subjects:
            return RedirectResponse(_report_path("statement"), status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            sections = await _resolve_statements(token, subjects, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        rows: list[list] = []
        for s in sections:
            data = s["data"]
            rows.append([_subject_heading(s["kind"], data)])
            if s["merged"]:
                rows.append([t("acct.soa_merged_notice")])
            rows.append(["Date", "Ref", "Type", "Debit", "Credit", "Balance"])
            rows.append([d_from, "", "Opening Balance", "", "", data.get("opening_balance", 0)])
            rows += [[r.get("date", ""), r.get("doc_ref", ""), r.get("kind", ""),
                      r.get("debit", 0), r.get("credit", 0), r.get("balance", 0)]
                     for r in data.get("rows", [])]
            rows.append([d_to, "", "Closing Balance", "", "", data.get("closing_balance", 0)])
            rows.append([])
        stem = (re.sub(r"[^A-Za-z0-9_-]+", "_",
                       _subject_heading(sections[0]["kind"], sections[0]["data"])).strip("_")
                if len(sections) == 1 else f"{len(sections)}_accounts")
        return csv_response(rows, f"statement_{stem or 'account'}_"
                                   f"{fname_date(d_from)}_{fname_date(d_to)}.csv")

    @app.get("/reports/export/pnl/csv")
    async def pnl_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            d_from = request.query_params.get("date_from", "")
            d_to = request.query_params.get("date_to", "")
            data = await api.get_pnl(token, date_params(d_from, d_to))
        except APIError as e:
            return plain_error_response(e)
        rev_total = float(data.get("revenue", {}).get("total", 0) or 0)

        def _pct(amount: float) -> str:
            return f"{abs(amount) / rev_total * 100:.1f}%" if rev_total else ""

        rows: list[list] = [["Section", "Code", "Account", "Amount", "% of Revenue"]]
        for key, label in [("revenue", "Revenue"), ("cogs", "Cost of Goods Sold"),
                           ("expenses", "Operating Expenses")]:
            section = data.get(key, {})
            for line in section.get("lines", []):
                amt = float(line.get("amount", 0) or 0)
                rows.append([label, line.get("code", ""), line.get("name", ""), amt, _pct(amt)])
            rows.append([f"TOTAL {label}", "", "", section.get("total", 0), ""])
        rows += [[], ["Gross Profit", "", "", data.get("gross_profit", 0), ""],
                 ["Net Profit", "", "", data.get("net_profit", 0), ""]]
        return csv_response(rows, f"pnl_{fname_date(d_from)}_{fname_date(d_to)}.csv")

    @app.get("/reports/export/balance-sheet/csv")
    async def balance_sheet_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            as_of = request.query_params.get("as_of", "") or _date.today().isoformat()
            data = await api.get_balance_sheet(token, {"as_of": as_of})
        except APIError as e:
            return plain_error_response(e)
        rows: list[list] = [["Section", "Code", "Account", "Amount"]]
        for key, label in [("assets", "Assets"), ("liabilities", "Liabilities"),
                           ("equity", "Equity")]:
            section = data.get(key, {})
            for line in section.get("lines", []):
                rows.append([label, line.get("code", ""), line.get("name", ""), line.get("amount", 0)])
            rows += [[f"TOTAL {label}", "", "", section.get("total", 0)], []]
        return csv_response(rows, f"balance_sheet_{fname_date(as_of)}.csv")
