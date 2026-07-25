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
from ui.routes.financial_reports import MOVED_TABS, REPORTS, parse_subject, subject_token


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

_DEFAULTS = {
    "get_company": _COMPANY, "list_contacts": _CONTACTS, "get_chart": _CHART,
    "get_soa": _SOA, "get_ledger": _LEDGER, "get_pnl": _PNL, "get_balance_sheet": _BS,
    "get_trial_balance": _TB, "get_general_ledger": _GL, "get_cash_flow": _CASH_FLOW,
    "get_ar_aging": _AGING, "get_ap_aging": _AGING,
}


def _mocks(**overrides):
    """Patch every api_client call this surface makes, returning the mocks by name
    so a test can inspect what the page actually asked for."""
    values = {**_DEFAULTS, **overrides}
    made = {name: AsyncMock(return_value=val) for name, val in values.items()}
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

@pytest.mark.parametrize("tab,expected", sorted(MOVED_TABS.items()))
@pytest.mark.asyncio
async def test_moved_tab_redirects_to_its_new_home(ui_client, tab, expected):
    r = await _get(ui_client, f"/accounting?tab={tab}")
    assert r.status_code in (302, 303), tab
    assert r.headers["location"].split("?")[0] == expected


@pytest.mark.asyncio
async def test_moved_tab_redirect_preserves_dates(ui_client):
    """A bookmarked report keeps its period; dropping it would silently show a
    different set of figures than the link was saved for."""
    r = await _get(ui_client, "/accounting?tab=pnl&from=2026-01-01&to=2026-12-31")
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert "from=2026-01-01" in loc and "to=2026-12-31" in loc


@pytest.mark.asyncio
async def test_legacy_soa_contact_link_still_resolves(ui_client):
    r = await _get(ui_client, "/accounting?tab=soa&contact_id=contact:9")
    assert r.status_code in (302, 303)
    assert "contact_id=contact" in r.headers["location"]


@pytest.mark.parametrize("key", sorted(REPORTS))
@pytest.mark.asyncio
async def test_every_report_page_renders(ui_client, key):
    r = await _get(ui_client, REPORTS[key][0])
    assert r.status_code == 200, (key, r.status_code)


@pytest.mark.asyncio
async def test_reports_index_lists_the_financial_reports(ui_client):
    r = await _get(ui_client, "/reports")
    assert r.status_code == 200
    for key in ("pnl", "balance-sheet", "cash-flow", "trial-balance",
                "general-ledger", "statement"):
        assert REPORTS[key][0] in r.text, key


@pytest.mark.asyncio
async def test_accounting_page_signposts_every_moved_report(ui_client):
    """Someone who came looking for a report where it used to be gets a direct
    link, not a hunt through an index."""
    r = await _get(ui_client, "/accounting")
    assert r.status_code == 200
    for path in MOVED_TABS.values():
        assert path in r.text, path


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
async def test_ledger_drilldown_requests_its_account(ui_client):
    r = await _get(ui_client, "/reports/ledger/6200?date_from=2026-01-01&src=trial-balance")
    assert r.status_code == 200
    assert r.mocks["get_ledger"].await_args.args[1] == "6200"


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
    r = await _get(ui_client, "/reports/statement?account=c:contact:9&account=c:contact:9")
    assert r.status_code == 200
    assert r.text.count("Acme Ltd") == r.text.count("Acme Ltd")
    assert r.mocks["get_soa"].await_count == 1


@pytest.mark.asyncio
async def test_a_merged_contact_is_substituted_only_in_its_own_section(ui_client):
    """Substituting one selection must not disturb the others, and the swap is
    disclosed on the section it happened in."""
    async def _soa(_token, contact_id, _params=None):
        if contact_id == "contact:old":
            return {"merged_into": "contact:9"}
        return _SOA
    # get_soa needs call-dependent behaviour, so it is patched by hand here.
    made, _ = _mocks()
    made["get_soa"] = AsyncMock(side_effect=_soa)
    ps = [patch(f"ui.api_client.{n}", new=m) for n, m in made.items()]
    for p in ps:
        p.start()
    try:
        r = await ui_client.get(
            "/reports/statement?account=c:contact:old&account=a:6200",
            cookies=_cookies())
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert "merged into" in r.text
    assert "Rent" in r.text  # the other selection is untouched


@pytest.mark.asyncio
async def test_a_tombstone_and_its_winner_together_render_the_winner_once(ui_client):
    async def _soa(_token, contact_id, _params=None):
        if contact_id == "contact:old":
            return {"merged_into": "contact:9"}
        return _SOA
    made, _ = _mocks()
    made["get_soa"] = AsyncMock(side_effect=_soa)
    ps = [patch(f"ui.api_client.{n}", new=m) for n, m in made.items()]
    for p in ps:
        p.start()
    try:
        r = await ui_client.get(
            "/reports/statement?account=c:contact:old&account=c:contact:9",
            cookies=_cookies())
    finally:
        for p in ps:
            p.stop()
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
# Print and CSV
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_statement_print_paginates_one_subject_per_page(ui_client):
    r = await _get(ui_client, "/reports/print/statement?account=c:contact:9&account=a:6200")
    assert r.status_code == 200
    assert r.text.count("soa-page") == 2


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
# Locale keys
# ---------------------------------------------------------------------------

_NEW_KEYS = [
    "acct.tab_cash_flow", "acct.cf_direct", "acct.cf_indirect", "acct.cf_operating",
    "acct.cf_investing", "acct.cf_financing", "acct.cf_opening_cash",
    "acct.cf_closing_cash", "acct.cf_net_change", "acct.cf_methods_disagree",
    "acct.reports_moved_notice", "acct.soa_source_docs", "acct.soa_source_ledger",
    "acct.soa_pick_placeholder", "acct.soa_selected_count", "acct.soa_pick_ar_balance",
    "acct.soa_pick_ap_balance", "acct.soa_pick_all_customers",
    "rpt.cash_flow_desc", "rpt.trial_balance_desc", "rpt.general_ledger_desc",
    "rpt.statement_desc",
]

# Deleted with the batch print panel. Presence checks alone would not notice a key
# left behind in ten locales after being removed from one.
_REMOVED_KEYS = ["acct.soa_batch_title", "acct.soa_batch_none_selected"]


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
    """The GL export makes one general-ledger call with include_lines and
    never re-derives detail: no get_ledger, no get_journal, running balance
    signed by the backend's debit_normal flag."""
    gl = json.loads(json.dumps(_GL))
    gl["rows"][0]["lines"] = [{"date": "2026-01-15", "je_id": "je:manual:abc",
                              "memo": "Adjustment", "source_ref": None,
                              "debit": 40.0, "credit": 0.0}]
    gl["rows"][1]["lines"] = [{"date": "2026-01-15", "je_id": "je:manual:abc",
                              "memo": "Adjustment", "source_ref": None,
                              "debit": 0.0, "credit": 40.0}]
    ledger_mock = AsyncMock(return_value=_LEDGER)
    journal_mock = AsyncMock(return_value=_JOURNAL)
    gl_mock = AsyncMock(return_value=gl)
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.get_ledger", new=ledger_mock), \
             patch("ui.api_client.get_journal", new=journal_mock), \
             patch("ui.api_client.get_general_ledger", new=gl_mock):
            r = await ui_client.get("/reports/export/general-ledger/csv", cookies=_cookies())
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    ledger_mock.assert_not_called()
    journal_mock.assert_not_called()
    called_params = gl_mock.call_args.args[1] if len(gl_mock.call_args.args) > 1 else gl_mock.call_args.kwargs.get("params", {})
    assert called_params.get("include_lines") == "1"
    lines = r.text.strip().splitlines()
    bank = [l for l in lines if l.startswith("1111")]
    assert any("Opening balance" in l and "100.0" in l for l in bank)
    # Debit-normal: opening 100 + 40 debit = 140 running on the detail row
    assert any(l.endswith("140.0") and "Adjustment" in l for l in bank)
    # Credit-normal account: the 40 credit INCREASES the running balance
    sales = [l for l in lines if l.startswith("4100") and "Adjustment" in l]
    assert sales and any(l.endswith("140.0") for l in sales)
