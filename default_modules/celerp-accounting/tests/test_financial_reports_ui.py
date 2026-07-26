# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The financial reports at /reports: routing, selection, and what gets asked for.

API responses are mocked, so rendering assertions alone would pass even if a page
asked the API for the wrong thing. Several tests here therefore assert on the
outbound call arguments rather than the returned HTML: after moving these pages
between modules, "requested the wrong date range" is the failure mode that looks
identical to success from the outside.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.api_client import APIError
from ui.routes.financial_reports import (
    MOVED_TABS, REPORTS, ledger_path, parse_subject, subject_token,
)


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _cookies(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(role=role)}


_COMPANY = {"id": "c1", "name": "TestCo", "currency": "THB", "settings": {"currency": "THB"}}
_CONTACTS = {"items": [
    {"id": "contact:9", "name": "Acme Ltd", "contact_type": "customer"},
    {"id": "contact:7", "name": "Supplier Co", "contact_type": "supplier"},
]}
_CHART = {"items": [
    {"code": "1120", "name": "Accounts Receivable", "account_type": "asset"},
    {"code": "6200", "name": "Rent", "account_type": "expense"},
]}
_SOA = {
    "contact": {"id": "contact:9", "name": "Acme Ltd", "type": "customer"},
    "opening_balance": 0.0, "closing_balance": 60.0,
    "rows": [{"date": "2026-01-20", "doc_id": "doc1", "doc_ref": "INV-1",
              "kind": "invoice", "debit": 60.0, "credit": 0.0, "balance": 60.0}],
}
_LEDGER = {
    "account_code": "6200", "account_name": "Rent", "account_type": "expense",
    "opening_balance": 10.0, "closing_balance": 35.0,
    "lines": [{"date": "2026-02-01", "je_id": "je:manual:1", "memo": "February rent",
               "doc_id": None, "doc_ref": None, "contact_id": "",
               "debit": 25.0, "credit": 0.0, "balance": 35.0}],
}
_PNL = {"revenue": {"lines": [], "total": 0}, "cogs": {"lines": [], "total": 0},
        "expenses": {"lines": [], "total": 0}, "gross_profit": 0, "net_profit": 0}
_BS = {"assets": {"lines": [], "total": 0}, "liabilities": {"lines": [], "total": 0},
       "equity": {"lines": [], "total": 0}, "balanced": True}
_TB = {"lines": [{"code": "1120", "name": "Accounts Receivable", "account_type": "asset",
                  "total_debit": 60.0, "total_credit": 0.0, "net": 60.0}],
       "total_debit": 60.0, "total_credit": 60.0, "balanced": True}
_GL = {"rows": [{"code": "1120", "name": "Accounts Receivable", "account_type": "asset",
                 "debit_normal": True, "opening": 0, "debit": 60.0, "credit": 0, "closing": 60.0}],
       "totals": {"opening": 0, "debit": 60.0, "credit": 60.0, "closing": 60.0}, "balanced": True}
_CASH_FLOW = {
    "cash_accounts": ["1111"], "opening_cash": 0.0, "closing_cash": 60.0, "net_change": 60.0,
    "direct": {"operating": {"lines": [{"code": "4100", "name": "Sales", "amount": 60.0}],
                             "total": 60.0},
               "investing": {"lines": [], "total": 0.0},
               "financing": {"lines": [], "total": 0.0},
               "total": 60.0},
    "indirect": {"net_profit": 60.0, "adjustments": [], "total": 60.0},
    "balanced": True,
}
_AGING = {"lines": [{"customer_id": "contact:9", "supplier_id": "contact:9", "total": 60.0}]}
# /accounting is the journal now, and the signpost test loads it.
_JOURNAL = {
    "date_from": "", "date_to": "", "total_debit": 60.0, "total_credit": 60.0,
    "entries": [{"je_id": "je:manual:abc", "ts": "2026-01-15", "memo": "Adjustment",
                 "status": "posted", "je_type": "manual", "void_reason": None,
                 "source_doc": None, "fx": None,
                 "lines": [{"account": "1111", "name": "Bank", "debit": 60.0, "credit": 0.0},
                           {"account": "4100", "name": "Sales", "debit": 0.0, "credit": 60.0}]}],
}

# The same period as the journal above, with the item detail the extended view
# adds, and one entry of every status the expansion can end in: split into its
# items, left whole because its figures did not tie, left whole because the
# document behind it is gone, and a payment that never had items of its own. The
# page renders the item rows, both notes, and nothing about the payment.
_EXTENDED_JOURNAL = {
    "date_from": "", "date_to": "", "total_debit": 60.0, "total_credit": 60.0,
    "entries": [
        {"je_id": "je:doc:1", "ts": "2026-01-15", "memo": "Auto JE for doc:1 finalized",
         "status": "posted", "je_type": "auto", "void_reason": None,
         "source_doc": {"doc_id": "doc:1", "doc_ref": "INV-0001", "doc_type": "invoice"},
         "fx": None, "items_status": "expanded",
         "lines": [{"account": "1120", "name": "Accounts Receivable", "debit": 60.0, "credit": 0.0},
                   {"account": "4100", "name": "Sales", "debit": 0.0, "credit": 60.0,
                    "item": "WIDGET-1", "quantity": 2, "unit_price": 30.0}]},
        {"je_id": "je:doc:2", "ts": "2026-01-16", "memo": "Auto JE for doc:2 finalized",
         "status": "posted", "je_type": "auto", "void_reason": None,
         "source_doc": {"doc_id": "doc:2", "doc_ref": "INV-0002", "doc_type": "invoice"},
         "fx": None, "items_status": "untied",
         "lines": [{"account": "1120", "name": "Accounts Receivable", "debit": 10.0, "credit": 0.0},
                   {"account": "4100", "name": "Sales", "debit": 0.0, "credit": 10.0}]},
        {"je_id": "je:doc:3", "ts": "2026-01-17", "memo": "Auto JE for doc:3 finalized",
         "status": "posted", "je_type": "auto", "void_reason": None,
         # A document that is no longer in the books: the reference the report can
         # show is the raw id the entry carries, because there is no document left
         # to read a number from.
         "source_doc": {"doc_id": "doc:3", "doc_ref": "doc:3"},
         "fx": None, "items_status": "no_document",
         "lines": [{"account": "1120", "name": "Accounts Receivable", "debit": 25.0, "credit": 0.0},
                   {"account": "4100", "name": "Sales", "debit": 0.0, "credit": 25.0}]},
        {"je_id": "je:doc:1:pay:0", "ts": "2026-01-18", "memo": "Payment received for doc:1",
         "status": "posted", "je_type": "auto", "void_reason": None,
         "source_doc": {"doc_id": "doc:1", "doc_ref": "INV-0001", "doc_type": "invoice"},
         "fx": None, "items_status": "no_items",
         "lines": [{"account": "1010", "name": "Cash", "debit": 60.0, "credit": 0.0},
                   {"account": "1120", "name": "Accounts Receivable", "debit": 0.0, "credit": 60.0}]},
    ],
}

_DEFAULTS = {
    "get_company": _COMPANY, "list_contacts": _CONTACTS, "get_chart": _CHART,
    "get_soa": _SOA, "get_ledger": _LEDGER, "get_pnl": _PNL, "get_balance_sheet": _BS,
    "get_trial_balance": _TB, "get_general_ledger": _GL, "get_cash_flow": _CASH_FLOW,
    "get_ar_aging": _AGING, "get_ap_aging": _AGING, "get_journal": _JOURNAL,
    "get_extended_journal": _EXTENDED_JOURNAL,
}


def _mocks(**overrides):
    """Patch every api_client call this surface makes, returning the mocks by name
    so a test can inspect what the page actually asked for.

    An override is a return value, or a mock of its own where the answer depends
    on what was asked: a stand-in that pages, or one that answers differently per
    contact.
    """
    values = {**_DEFAULTS, **overrides}
    made = {name: val if isinstance(val, AsyncMock) else AsyncMock(return_value=val)
            for name, val in values.items()}
    patchers = [patch(f"ui.api_client.{name}", new=mock) for name, mock in made.items()]
    return made, patchers


async def _get(ui_client, url, role: str = "owner", **overrides):
    made, ps = _mocks(**overrides)
    for p in ps:
        p.start()
    try:
        resp = await ui_client.get(url, cookies=_cookies(role))
    finally:
        for p in ps:
            p.stop()
    resp.mocks = made
    return resp


# ---------------------------------------------------------------------------
# The move: old URLs keep working, new ones render
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tab,key", sorted(MOVED_TABS.items()))
@pytest.mark.asyncio
async def test_moved_tab_redirects_to_its_new_home(ui_client, tab, key):
    r = await _get(ui_client, f"/accounting?tab={tab}")
    assert r.status_code in (302, 303), tab
    assert r.headers["location"].split("?")[0] == REPORTS[key][0]


@pytest.mark.asyncio
async def test_moved_tab_redirect_preserves_dates(ui_client):
    """A bookmarked report keeps its period; dropping it would silently show a
    different set of figures than the link was saved for. The date bar wrote
    preset links as often as explicit ranges, so a preset is a period too."""
    r = await _get(ui_client,
                   "/accounting?tab=pnl&from=2026-01-01&to=2026-12-31&preset=last_month")
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert "from=2026-01-01" in loc and "to=2026-12-31" in loc
    assert "preset=last_month" in loc


@pytest.mark.asyncio
async def test_legacy_soa_contact_link_still_resolves(ui_client):
    r = await _get(ui_client, "/accounting?tab=soa&contact_id=contact:9")
    assert r.status_code in (302, 303)
    assert "contact_id=contact" in r.headers["location"]


# Every URL these reports answered on before they moved out of /accounting, with
# the report key and the slot in REPORTS that now holds the replacement (0 page,
# 1 print, 2 CSV). The old side is written out because it is a record of what was
# published; the new side is read from REPORTS so it follows a report that moves
# again.
_OLD_REPORT_URLS: list[tuple[str, str, int]] = [
    ("/accounting/pnl", "pnl", 0),
    ("/accounting/balance-sheet", "balance-sheet", 0),
    ("/accounting/print/pnl", "pnl", 1),
    ("/accounting/print/balance-sheet", "balance-sheet", 1),
    ("/accounting/print/trial-balance", "trial-balance", 1),
    ("/accounting/print/general-ledger", "general-ledger", 1),
    ("/accounting/print/soa", "statement", 1),
    ("/accounting/export/pnl/csv", "pnl", 2),
    ("/accounting/export/balance-sheet/csv", "balance-sheet", 2),
    ("/accounting/export/trial-balance/csv", "trial-balance", 2),
    ("/accounting/export/general-ledger/csv", "general-ledger", 2),
    ("/accounting/export/soa/csv", "statement", 2),
]


@pytest.mark.parametrize("old,key,slot", _OLD_REPORT_URLS)
@pytest.mark.asyncio
async def test_every_old_report_url_redirects_carrying_its_parameters(
        ui_client, old, key, slot):
    """Saved links, emailed reports and scheduled downloads all predate the move.

    A 404 loses the reader outright; a redirect that drops the filters is worse,
    because it answers with a different set of figures than the link was saved for
    and says nothing about it.
    """
    r = await _get(ui_client, f"{old}?date_from=2026-01-01&date_to=2026-12-31"
                              "&as_of=2026-12-31&preset=last_month&contact_id=contact:9")
    assert r.status_code in (302, 303), old
    loc = r.headers["location"]
    assert loc.split("?")[0] == REPORTS[key][slot], old
    for carried in ("date_from=2026-01-01", "date_to=2026-12-31", "as_of=2026-12-31",
                    "preset=last_month", "contact_id=contact"):
        assert carried in loc, (old, carried)


@pytest.mark.asyncio
async def test_the_old_account_ledger_url_redirects_carrying_its_parameters(ui_client):
    """The drilldown moved with the reports it hangs off. Its links carry the
    originating report in "src" as well as the dates, and losing that strands the
    reader with no way back to where they came from."""
    r = await _get(ui_client, "/accounting/ledger/1130-FRT?date_from=2026-01-01"
                              "&date_to=2026-12-31&src=trial-balance")
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert loc.split("?")[0] == ledger_path("1130-FRT")
    for carried in ("date_from=2026-01-01", "date_to=2026-12-31", "src=trial-balance"):
        assert carried in loc, carried


@pytest.mark.parametrize("key", sorted(REPORTS))
@pytest.mark.asyncio
async def test_every_report_page_renders(ui_client, key):
    r = await _get(ui_client, REPORTS[key][0])
    assert r.status_code == 200, (key, r.status_code)


@pytest.mark.asyncio
async def test_reports_index_lists_the_financial_reports(ui_client):
    r = await _get(ui_client, "/reports")
    assert r.status_code == 200
    for key in ("journal", "pnl", "balance-sheet", "cash-flow", "trial-balance",
                "general-ledger", "extended-journal", "statement"):
        assert REPORTS[key][0] in r.text, key


@pytest.mark.asyncio
async def test_extended_journal_page_shows_what_each_posting_was_for(ui_client):
    """The whole point of the report: the item, how many, and at what price,
    on the posting line itself."""
    r = await _get(ui_client, "/reports/extended-journal")
    assert r.status_code == 200
    assert "WIDGET-1" in r.text
    assert "฿30.00" in r.text


# The two reasons item detail can be withheld, as the page words them.
_UNTIED = "because their amounts do not tie to the document lines:"
_NO_DOCUMENT = "because the document behind them is no longer in the books:"


def _note(text: str, reason: str) -> str:
    """What the note giving `reason` says, or "" when the page gives no such note."""
    if reason not in text:
        return ""
    return text.split(reason, 1)[1].split("</div>", 1)[0]


@pytest.mark.asyncio
async def test_extended_journal_page_names_the_entries_it_could_not_expand(ui_client):
    """An entry with no item detail shown is called out by id, under the reason it
    was withheld. Reading past it as though the document had no items would be
    worse than saying nothing, so the report says which entries it could not
    account for, and figures that did not tie are not confused with a document
    that is no longer there: one is a judgement about the numbers, the other is a
    fact about the books, and an accountant does something different about each."""
    r = await _get(ui_client, "/reports/extended-journal")
    untied, orphaned = _note(r.text, _UNTIED), _note(r.text, _NO_DOCUMENT)
    assert untied, f"no note about figures that did not tie: {r.text[:2000]}"
    assert orphaned, f"no note about a missing document: {r.text[:2000]}"
    assert "je:doc:2" in untied and "je:doc:3" not in untied
    assert "je:doc:3" in orphaned and "je:doc:2" not in orphaned
    assert "je:doc:1" not in untied and "je:doc:1" not in orphaned


@pytest.mark.asyncio
async def test_extended_journal_page_says_nothing_about_entries_with_no_items(ui_client):
    """A payment moves cash against a control account and never had items of its
    own, so naming it would send a reader hunting for detail that was never meant
    to exist. It appears in the report like any other entry and in neither note."""
    r = await _get(ui_client, "/reports/extended-journal")
    # The rows carry the memo, not the entry id: an id on the page at all means a
    # note named it.
    assert "Payment received for doc:1" in r.text
    assert "je:doc:1:pay:0" not in r.text
    assert "je:doc:1:pay:0" not in _note(r.text, _UNTIED)
    assert "je:doc:1:pay:0" not in _note(r.text, _NO_DOCUMENT)

    payments = {**_EXTENDED_JOURNAL,
                "entries": [e for e in _EXTENDED_JOURNAL["entries"]
                            if e["items_status"] == "no_items"]}
    r = await _get(ui_client, "/reports/extended-journal", get_extended_journal=payments)
    assert r.status_code == 200
    assert "Item detail is not shown" not in r.text


@pytest.mark.asyncio
async def test_accounting_page_signposts_every_moved_report(ui_client):
    """Someone who came looking for a report where it used to be gets a direct
    link, not a hunt through an index."""
    r = await _get(ui_client, "/accounting")
    assert r.status_code == 200
    for key in MOVED_TABS.values():
        assert REPORTS[key][0] in r.text, key


@pytest.mark.asyncio
async def test_report_pages_return_a_fragment_to_htmx(ui_client):
    made, ps = _mocks()
    for p in ps:
        p.start()
    try:
        r = await ui_client.get("/reports/pnl", cookies=_cookies(),
                                headers={"HX-Request": "true"})
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert "main-content" in r.text


@pytest.mark.asyncio
async def test_report_pages_mark_reports_active_in_the_sidebar(ui_client):
    r = await _get(ui_client, "/reports/trial-balance")
    assert r.status_code == 200
    assert 'href="/reports"' in r.text


# ---------------------------------------------------------------------------
# What the pages ask the API for
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pnl_page_requests_the_range_it_was_given(ui_client):
    r = await _get(ui_client, "/reports/pnl?from=2026-03-01&to=2026-03-31")
    assert r.status_code == 200
    _token, params = r.mocks["get_pnl"].await_args.args
    assert params == {"date_from": "2026-03-01", "date_to": "2026-03-31"}


@pytest.mark.asyncio
async def test_balance_sheet_page_requests_the_as_of_date(ui_client):
    r = await _get(ui_client, "/reports/balance-sheet?as_of=2026-06-30")
    assert r.status_code == 200
    _token, params = r.mocks["get_balance_sheet"].await_args.args
    assert params == {"as_of": "2026-06-30"}


@pytest.mark.asyncio
async def test_statement_requests_each_selected_subject(ui_client):
    r = await _get(ui_client,
                   "/reports/statement?account=c:contact:9&account=a:6200"
                   "&from=2026-01-01&to=2026-12-31")
    assert r.status_code == 200
    soa_args = r.mocks["get_soa"].await_args.args
    assert soa_args[1] == "contact:9"
    assert soa_args[2] == {"date_from": "2026-01-01", "date_to": "2026-12-31"}
    led_args = r.mocks["get_ledger"].await_args.args
    assert led_args[1] == "6200"
    assert led_args[2] == {"date_from": "2026-01-01", "date_to": "2026-12-31"}


@pytest.mark.asyncio
async def test_trial_balance_export_passes_dates(ui_client):
    """A download taken from a filtered report has to cover that filter's period.

    The mocked API answers with the same figures whatever it is asked for, so the
    outbound call is the only place the dropped date shows up: a CSV built from the
    whole history renders exactly like one built from January.
    """
    r = await _get(ui_client, "/reports/export/trial-balance/csv"
                              "?date_from=2026-01-01&date_to=2026-01-31")
    assert r.status_code == 200
    _token, params = r.mocks["get_trial_balance"].await_args.args
    assert params == {"date_from": "2026-01-01", "date_to": "2026-01-31"}


@pytest.mark.asyncio
async def test_ledger_drilldown_requests_its_account(ui_client):
    """The drilldown asks for the account it names, over the range it was given,
    and offers a way back to the report the reader came from with that same range
    still applied. A back link that drops the dates returns them to a different set
    of figures than the one they clicked out of."""
    r = await _get(ui_client, "/reports/ledger/6200"
                              "?src=general-ledger&from=2026-01-01&to=2026-01-31")
    assert r.status_code == 200
    _token, code, params = r.mocks["get_ledger"].await_args.args
    assert code == "6200"
    assert params == {"date_from": "2026-01-01", "date_to": "2026-01-31"}
    back = f"{REPORTS['general-ledger'][0]}?from=2026-01-01&amp;to=2026-01-31"
    assert back in r.text


@pytest.mark.asyncio
async def test_ledger_shows_its_opening_balance_on_every_surface(ui_client):
    """A date-filtered ledger carries its running balance on from the prior period,
    so the opening figure has to be on the page. Without it the first row's balance
    is a number the reader cannot account for or tie back to the general ledger.
    The account's statement is built from the same call, so all three surfaces are
    checked together: page, print, and CSV.
    """
    page = await _get(ui_client,
                      "/reports/ledger/6200?date_from=2026-03-01&src=general-ledger")
    assert page.status_code == 200
    assert "Opening Balance" in page.text
    assert "฿10.00" in page.text

    printed = await _get(ui_client, "/reports/print/statement?account=a:6200"
                                    "&date_from=2026-03-01&date_to=2026-03-31")
    assert printed.status_code == 200
    assert "Opening Balance" in printed.text
    assert "฿10.00" in printed.text

    exported = await _get(ui_client, "/reports/export/statement/csv?account=a:6200"
                                     "&date_from=2026-03-01&date_to=2026-03-31")
    assert exported.status_code == 200
    assert any("Opening Balance" in line and "10.0" in line
               for line in exported.text.splitlines())


# ---------------------------------------------------------------------------
# Statement subjects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("c:contact:9", ("c", "contact:9")),   # a contact id carries its own colon
    ("a:1120", ("a", "1120")),
    ("a:1130-P", ("a", "1130-P")),
    ("x:1120", None),                      # unknown kind is dropped, not guessed
    ("1120", None),
    ("c:", None),
    ("", None),
])
def test_subject_parsing(raw, expected):
    assert parse_subject(raw) == expected


def test_a_chart_code_never_parses_as_a_contact():
    kind, ident = parse_subject(subject_token("a", "1120"))
    assert kind == "a" and ident == "1120"


def test_single_select_comboboxes_are_untouched_by_the_multiple_option():
    """searchable_select is used across the app; gaining a multi-select mode must
    not change a single byte of what every existing caller renders."""
    from fasthtml.common import to_xml
    from ui.components.table import searchable_select

    markup = to_xml(searchable_select("acct", [("a", "Alpha"), ("b", "Beta")], value="b"))
    assert "data-multiple" not in markup
    assert "combobox-selected" not in markup
    assert "combobox-option--selected" not in markup


def test_multi_select_submits_one_field_per_selection():
    """A repeated query key is what the server reads back, and one input cannot
    carry two values."""
    from fasthtml.common import to_xml
    from ui.components.table import searchable_select

    opts = [("a", "Alpha"), ("b", "Beta")]
    # Leading space, so the data-name attribute is not counted as a submitted field.
    two = to_xml(searchable_select("account", opts, multiple=True, values=["a", "b"],
                                   count_label="{n} selected"))
    assert two.count(' name="account"') == 2
    assert "2 selected" in two

    one = to_xml(searchable_select("account", opts, multiple=True, values=["b"]))
    assert one.count(' name="account"') == 1
    assert 'value="Beta"' in one, "a single selection reads as its own name, not a count"

    none = to_xml(searchable_select("account", opts, multiple=True, values=[]))
    assert none.count(' name="account"') == 0


@pytest.mark.asyncio
async def test_single_subject_renders_one_statement(ui_client):
    r = await _get(ui_client, "/reports/statement?account=c:contact:9")
    assert r.status_code == 200
    assert "Acme Ltd" in r.text
    assert r.text.count("report-section-title") >= 1


@pytest.mark.asyncio
async def test_several_subjects_each_get_their_own_section(ui_client):
    r = await _get(ui_client, "/reports/statement?account=c:contact:9&account=a:6200")
    assert r.status_code == 200
    assert "Acme Ltd" in r.text
    assert "Rent" in r.text


@pytest.mark.asyncio
async def test_each_section_says_which_books_it_came_from(ui_client):
    """The two kinds are assembled from different records and can differ, so a
    reader is told which one they are looking at."""
    r = await _get(ui_client, "/reports/statement?account=c:contact:9&account=a:6200")
    assert r.status_code == 200
    assert "documents" in r.text
    assert "journal entries" in r.text


@pytest.mark.asyncio
async def test_no_selection_prompts_instead_of_rendering_nothing(ui_client):
    r = await _get(ui_client, "/reports/statement")
    assert r.status_code == 200
    assert "empty-state" in r.text


@pytest.mark.asyncio
async def test_duplicate_selection_renders_once(ui_client):
    """Picking the same subject twice is one statement, not two identical ones.

    One closing-balance block per rendered section, so counting them counts the
    sections. The subject's name is no use for this: the picker lists it as well.
    """
    r = await _get(ui_client, "/reports/statement?account=c:contact:9&account=c:contact:9")
    assert r.status_code == 200
    assert r.text.count("report-total") == 1
    assert r.mocks["get_soa"].await_count == 1


@pytest.mark.asyncio
async def test_a_merged_contact_is_substituted_only_in_its_own_section(ui_client):
    """Substituting one selection must not disturb the others, and the swap is
    disclosed on the section it happened in."""
    async def _soa(_token, contact_id, _params=None):
        if contact_id == "contact:old":
            return {"merged_into": "contact:9"}
        return _SOA
    r = await _get(ui_client, "/reports/statement?account=c:contact:old&account=a:6200",
                   get_soa=AsyncMock(side_effect=_soa))
    assert r.status_code == 200
    assert "merged into" in r.text
    assert "Rent" in r.text  # the other selection is untouched


@pytest.mark.asyncio
async def test_a_tombstone_and_its_winner_together_render_the_winner_once(ui_client):
    async def _soa(_token, contact_id, _params=None):
        if contact_id == "contact:old":
            return {"merged_into": "contact:9"}
        return _SOA
    r = await _get(ui_client, "/reports/statement?account=c:contact:old&account=c:contact:9",
                   get_soa=AsyncMock(side_effect=_soa))
    assert r.status_code == 200
    # One closing-balance block for the winner, not two.
    assert r.text.count("report-total") == 1


@pytest.mark.asyncio
async def test_quick_pick_expands_to_everyone_with_a_balance(ui_client):
    """The month-end run is a set nobody wants to tick by hand."""
    r = await _get(ui_client, "/reports/statement?pick=ar_balance")
    assert r.status_code == 200
    assert r.mocks["get_ar_aging"].await_count == 1
    assert r.mocks["get_soa"].await_count == 1


@pytest.mark.asyncio
async def test_the_picker_offers_contacts_and_chart_accounts_together(ui_client):
    r = await _get(ui_client, "/reports/statement")
    assert r.status_code == 200
    assert subject_token("c", "contact:9") in r.text
    assert subject_token("a", "1120") in r.text


# ---------------------------------------------------------------------------
# Every contact is reachable, and the page says what it is showing
# ---------------------------------------------------------------------------

def _many_contacts(n: int) -> list[dict]:
    """n customers, zero-padded so a plain name sort keeps them in seeded order."""
    return [{"id": f"contact:{i:04d}", "name": f"Contact {i:04d}", "contact_type": "customer"}
            for i in range(n)]


def _contacts_api(contacts: list[dict]):
    """Stand-in for the contacts endpoint: filters, sorts and pages as it does.

    A mock that returns the same list whatever it is asked for cannot show a
    truncation, because truncation is invisible in the response body. This one
    honours q, contact_type, limit and offset, so a page that asks for less than
    the whole set gets less than the whole set.
    """
    async def _call(_token, params=None):
        params = params or {}
        rows = list(contacts)
        q = (params.get("q") or "").lower()
        if q:
            rows = [c for c in rows if q in (c.get("name") or "").lower()]
        ctype = params.get("contact_type")
        if ctype:
            allowed = {"customer": ("customer", "both"), "vendor": ("vendor", "both")}[ctype]
            rows = [c for c in rows if (c.get("contact_type") or "customer") in allowed]
        rows.sort(key=lambda c: ((c.get("name") or "").lower(), c.get("id") or ""))
        offset, limit = int(params.get("offset") or 0), int(params.get("limit") or 50)
        return {"items": rows[offset:offset + limit], "total": len(rows)}
    return AsyncMock(side_effect=_call)


async def _get_with_contacts(ui_client, url, contacts):
    return await _get(ui_client, url, list_contacts=_contacts_api(contacts))


@pytest.mark.asyncio
async def test_a_contact_past_the_first_page_is_reachable_by_typing(ui_client):
    """The picker opens on a page of contacts, so the rest have to be searchable.

    A company past the page size otherwise gets a control that cannot select some
    of its own customers and says nothing about it.
    """
    r = await _get_with_contacts(ui_client, "/reports/statement/subject-options?q=Contact+1000",
                                 _many_contacts(1001))
    assert r.status_code == 200
    assert subject_token("c", "contact:1000") in r.text


@pytest.mark.asyncio
async def test_the_picker_search_offers_contacts_and_chart_accounts_together(ui_client):
    """Searching narrows the same pool the picker opens with, not a smaller one."""
    r = await _get_with_contacts(ui_client, "/reports/statement/subject-options?q=Contact+0007",
                                 _many_contacts(1001))
    assert subject_token("c", "contact:0007") in r.text
    accounts = await _get_with_contacts(ui_client, "/reports/statement/subject-options?q=Rent",
                                        _many_contacts(1001))
    assert subject_token("a", "6200") in accounts.text


@pytest.mark.asyncio
async def test_a_selection_stays_marked_when_the_options_come_from_a_search(ui_client):
    """Search results replace the option list, so they have to carry the selection
    with them; an option that is selected but drawn unselected reads as a way to
    add it, while clicking it takes it off the statement."""
    page = await _get_with_contacts(ui_client, "/reports/statement?account=c:contact:0007",
                                    _many_contacts(1001))
    assert "/reports/statement/subject-options?account=c%3Acontact%3A0007" in page.text
    r = await _get_with_contacts(
        ui_client,
        "/reports/statement/subject-options?account=c:contact:0007&q=Contact+0007",
        _many_contacts(1001))
    assert "combobox-option--selected" in r.text


@pytest.mark.asyncio
async def test_the_all_customers_quick_pick_covers_the_customers_past_the_first_page(ui_client):
    """The month-end run is the one place a missing customer costs money, so the
    button stands for every customer rather than for the ones already loaded."""
    r = await _get_with_contacts(ui_client, "/reports/statement?pick=customers",
                                 _many_contacts(1001))
    assert r.status_code == 200
    asked = [call.args[1] for call in r.mocks["get_soa"].await_args_list]
    assert "contact:1000" in asked
    assert len(asked) == 1001


@pytest.mark.asyncio
async def test_the_picker_says_when_it_is_showing_only_the_first_contacts(ui_client):
    """Showing part of a list without saying so is the truncation this replaces."""
    many = await _get_with_contacts(ui_client, "/reports/statement", _many_contacts(1001))
    assert "1001" in many.text
    assert "Type to search" in many.text
    few = await _get_with_contacts(ui_client, "/reports/statement", _many_contacts(3))
    assert "Type to search" not in few.text


@pytest.mark.asyncio
async def test_the_picker_search_reports_a_failure_instead_of_an_empty_list(ui_client):
    """An API failure and no matches look identical in an empty option list, and
    the reader would act on the wrong one of the two."""
    r = await _get(ui_client, "/reports/statement/subject-options?q=Acme",
                   list_contacts=AsyncMock(side_effect=APIError(500, "boom")))
    assert r.status_code == 200
    assert "combobox-option--empty" in r.text
    assert "No results" not in r.text


@pytest.mark.asyncio
async def test_the_print_and_csv_buttons_carry_every_selected_subject(ui_client):
    """The two buttons beside a statement export what is on screen. Carrying one
    subject out of several hands over a month-end run with the rest missing and
    nothing saying so."""
    r = await _get(ui_client, "/reports/statement?account=c:contact:9&account=a:6200")
    assert r.status_code == 200
    for path in (REPORTS["statement"][1], REPORTS["statement"][2]):
        links = [part for part in r.text.split('"') if part.startswith(f"{path}?")]
        assert links, f"no link to {path} on the page"
        assert all(link.count("account=") == 2 for link in links), links


def test_the_cash_flow_statement_labels_the_column_its_figures_sit_in():
    """Every other report names its numeric column; an unlabelled one leaves the
    reader to infer whether the figures are the movement or the balance."""
    from fasthtml.common import to_xml
    from ui.routes.financial_reports import _cash_flow_view

    markup = to_xml(_cash_flow_view(_CASH_FLOW, "THB"))
    assert markup.count("<thead>") >= 2, "the direct section and the indirect table"
    assert markup.count("<table") == markup.count("<thead>")
    # Numeric headers sit right-aligned over their figures, as elsewhere.
    assert 'class="cell--number">Amount' in markup


# ---------------------------------------------------------------------------
# Print and CSV
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_statement_print_paginates_one_subject_per_page(ui_client):
    r = await _get(ui_client, "/reports/print/statement?account=c:contact:9&account=a:6200")
    assert r.status_code == 200
    # The class name also appears in the print stylesheet, so count the elements.
    assert r.text.count('class="soa-page"') == 2


@pytest.mark.asyncio
async def test_trial_balance_print_shows_period(ui_client):
    """A printed trial balance is handed to someone who cannot see the filter that
    produced it, so the sheet has to say which period it covers. "As of:" is the
    balance sheet's heading and describes a single instant; on a report that spans
    a range it would misdescribe every figure below it.
    """
    r = await _get(ui_client, "/reports/print/trial-balance"
                              "?date_from=2026-01-01&date_to=2026-01-31")
    assert r.status_code == 200
    assert "From: 2026-01-01" in r.text
    assert "To: 2026-01-31" in r.text
    assert "As of:" not in r.text


@pytest.mark.asyncio
async def test_statement_csv_covers_every_selection(ui_client):
    r = await _get(ui_client, "/reports/export/statement/csv?account=c:contact:9&account=a:6200")
    assert r.status_code == 200
    assert "Acme Ltd" in r.text and "Rent" in r.text
    assert "2_accounts" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_statement_csv_filename_is_header_safe(ui_client):
    r = await _get(ui_client, "/reports/export/statement/csv"
                              "?account=c:contact:9&date_from=../../etc")
    assert r.status_code == 200
    assert ".." not in r.headers["content-disposition"]


@pytest.mark.parametrize("key", sorted(REPORTS))
@pytest.mark.asyncio
async def test_every_report_prints_and_exports(ui_client, key):
    _page, print_path, csv_path, _title = REPORTS[key]
    suffix = "?account=c:contact:9" if key == "statement" else ""
    assert (await _get(ui_client, print_path + suffix)).status_code == 200
    assert (await _get(ui_client, csv_path + suffix)).status_code == 200


@pytest.mark.asyncio
async def test_action_bar_urls_are_encoded(ui_client):
    """The action bar builds its own query string; an unencoded value would
    produce a link that loses everything after the stray character."""
    r = await _get(ui_client, "/reports/statement?account=c:contact:9")
    assert r.status_code == 200
    assert "account=c%3Acontact%3A9" in r.text


# ---------------------------------------------------------------------------
# Module matrix: the reports module is not what makes the reports reachable
# ---------------------------------------------------------------------------

# A company running the books without the reports module installed. The financial
# reports belong to accounting, which owns the figures; only their URL sits under
# /reports, and that is the whole point of this matrix.
_ACCOUNTING_ONLY = ["celerp-accounting", "celerp-inventory", "celerp-sales"]


async def _get_without_the_reports_module(ui_client, url):
    _made, ps = _mocks()
    for p in ps:
        p.start()
    try:
        return await ui_client.get(
            url, cookies={"celerp_token": make_test_token(modules=_ACCOUNTING_ONLY)})
    finally:
        for p in ps:
            p.stop()


@pytest.mark.parametrize("key", sorted(REPORTS))
@pytest.mark.asyncio
async def test_every_financial_report_survives_the_reports_module_being_off(ui_client, key):
    """Turning off celerp-reports must not take the books away with it.

    The two modules both offer a Reports section and both would answer for the
    same URL, so it is easy for the financial reports to end up depending on the
    operational module by accident. Each page is checked on its own here rather
    than through the index, since the index rendering does not prove the page
    behind the card answers.
    """
    r = await _get_without_the_reports_module(ui_client, REPORTS[key][0])
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_the_reports_section_stays_in_the_nav_without_the_reports_module(ui_client):
    """Reachable by URL is not reachable by a user. Both modules declare the same
    nav key, so whichever survives the deduplication has to be the enabled one."""
    r = await _get_without_the_reports_module(ui_client, "/reports")
    assert r.status_code == 200, r.text
    # The sidebar only, not the page: the index links to itself from its own cards.
    sidebar = r.text.split("</nav>")[0]
    assert 'href="/reports"' in sidebar


def test_a_disabled_module_cannot_take_a_section_the_enabled_one_also_offers():
    """The nav drops what the company cannot see before it deduplicates keys.

    Two modules declare the section "reports": accounting, because the financial
    reports ship with the figures, and reports, because it adds the operational
    ones. Deduplicating first lets the disabled module's entry claim the key and
    then be filtered out, taking the surviving module's entry with it. Whether
    that happens used to depend on which module happened to be loaded first, so
    the order is pinned here with the disabled one deliberately in front.
    """
    from unittest.mock import patch

    from fasthtml.common import to_xml
    from starlette.requests import Request

    from ui.components.shell import _sidebar

    slots = [
        {"group": "Finance", "key": "reports", "href": "/reports", "label": "Reports",
         "order": 51, "_module": "celerp-reports"},
        {"group": "Finance", "key": "reports", "href": "/reports", "label": "Reports",
         "order": 51, "_module": "celerp-accounting"},
    ]
    request = Request({"type": "http", "method": "GET", "path": "/reports",
                       "query_string": b"", "headers": []})

    with patch("celerp.modules.slots.get", return_value=slots), \
         patch("ui.config.get_enabled_modules", return_value={"celerp-accounting"}):
        markup = to_xml(_sidebar("reports", request=request))

    assert 'href="/reports"' in markup


@pytest.mark.asyncio
async def test_the_reports_index_still_lists_the_financial_reports_without_that_module(ui_client):
    r = await _get_without_the_reports_module(ui_client, "/reports")
    assert r.status_code == 200, r.text
    for key, (path, _print, _csv, _title) in REPORTS.items():
        assert f'href="{path}"' in r.text, f"{key} card missing from the index"


# ---------------------------------------------------------------------------
# Locale keys
# ---------------------------------------------------------------------------

_NEW_KEYS = [
    "acct.tab_cash_flow", "acct.cf_direct", "acct.cf_indirect", "acct.cf_operating",
    "acct.cf_investing", "acct.cf_financing", "acct.cf_opening_cash",
    "acct.cf_closing_cash", "acct.cf_net_change", "acct.cf_methods_disagree",
    "acct.reports_moved_notice", "acct.soa_source_ledger",
    "acct.soa_pick_placeholder", "acct.soa_selected_count", "acct.soa_pick_ar_balance",
    "acct.soa_pick_ap_balance", "acct.soa_pick_all_customers",
    "acct.picker_more_contacts",
    "label.account", "label.vendor_bill", "acct.soa_kind_journal",
    "acct.je_line_party", "acct.je_party_hint",
    "acct.cf_unbacked_opening", "acct.opening_balance_unbacked",
    "rpt.cash_flow_desc", "rpt.trial_balance_desc", "rpt.general_ledger_desc",
    "rpt.statement_desc", "rpt.journal_desc",
    "acct.items_untied", "acct.items_no_document",
    "acct.filter_clear", "acct.filtered_totals", "acct.no_matches",
]

# Deleted with the batch print panel. Presence checks alone would not notice a key
# left behind in ten locales after being removed from one.
_REMOVED_KEYS = [
    "acct.soa_batch_title", "acct.soa_batch_none_selected",
    # Every statement is a slice of the ledger now, so there is no second source
    # to name and no wording that says a party statement came from documents.
    "acct.soa_source_docs",
    # Renamed: the same note now appears on the journal entry form too.
    "acct.soa_picker_more_contacts",
    # Renamed to acct.items_untied, now that it is one of two reasons item detail
    # can be withheld rather than the only one.
    "acct.items_not_expanded",
]


def _locales():
    d = os.path.join(os.path.dirname(__file__), "../../../ui/locales")
    for fname in os.listdir(d):
        if fname.endswith(".json"):
            with open(os.path.join(d, fname), encoding="utf-8") as fh:
                yield fname, json.load(fh)


def test_new_locale_keys_present_everywhere():
    for fname, data in _locales():
        for key in _NEW_KEYS:
            assert key in data, f"{key} missing from {fname}"


def test_removed_locale_keys_are_gone_everywhere():
    for fname, data in _locales():
        for key in _REMOVED_KEYS:
            assert key not in data, f"{key} still in {fname}"


@pytest.mark.asyncio
async def test_gl_csv_single_call_with_backend_lines(ui_client):
    """The GL export makes one general-ledger call with include_lines and never
    re-derives detail: no get_ledger, no get_journal, running balance signed by
    the backend's debit_normal flag."""
    gl = json.loads(json.dumps(_GL))
    gl["rows"] = [
        {"code": "1111", "name": "Bank", "account_type": "asset", "debit_normal": True,
         "opening": 100.0, "debit": 40.0, "credit": 0.0, "closing": 140.0,
         "lines": [{"date": "2026-01-15", "je_id": "je:manual:abc", "memo": "Adjustment",
                    "source_ref": None, "debit": 40.0, "credit": 0.0}]},
        {"code": "4100", "name": "Sales Revenue", "account_type": "revenue",
         "debit_normal": False, "opening": 100.0, "debit": 0.0, "credit": 40.0,
         "closing": 140.0,
         "lines": [{"date": "2026-01-15", "je_id": "je:manual:abc", "memo": "Adjustment",
                    "source_ref": None, "debit": 0.0, "credit": 40.0}]},
    ]
    made, ps = _mocks(get_general_ledger=gl)
    for p in ps:
        p.start()
    try:
        r = await ui_client.get("/reports/export/general-ledger/csv", cookies=_cookies())
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    made["get_ledger"].assert_not_called()
    made["get_journal"].assert_not_called()
    params = made["get_general_ledger"].await_args.args[1]
    assert params.get("include_lines") == "1"

    lines = r.text.strip().splitlines()
    bank = [l for l in lines if l.startswith("1111")]
    assert any("Opening balance" in l and "100.0" in l for l in bank)
    # Debit-normal: opening 100 + 40 debit = 140 running on the detail row
    assert any(l.endswith("140.0") and "Adjustment" in l for l in bank)
    # Credit-normal account: the 40 credit INCREASES the running balance
    sales = [l for l in lines if l.startswith("4100") and "Adjustment" in l]
    assert sales and any(l.endswith("140.0") for l in sales)
