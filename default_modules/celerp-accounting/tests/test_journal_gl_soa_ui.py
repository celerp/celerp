# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""UI surfaces for the journal and manual journal entries.

API responses are mocked (the API contract itself is covered in
test_journal_gl_soa.py); these tests pin the rendering contract: routes
resolve, tables render, CSV exports are flat/safe/dated, error states are
clean, and every locale key exists in every locale file.

The reports that used to be tabs here now live under /reports and are covered
in test_financial_reports_ui.py.
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
from ui.i18n import t


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

_JOURNAL = {
    "date_from": "", "date_to": "",
    "entries": [
        {
            "je_id": "je:manual:abc", "ts": "2026-01-15", "memo": "Adjustment",
            "status": "posted", "je_type": "manual", "void_reason": None,
            "source_doc": None,
            "lines": [
                {"account": "1111", "name": "Bank", "debit": 100.0, "credit": 0.0},
                {"account": "4100", "name": "Sales Revenue", "debit": 0.0, "credit": 100.0},
            ],
            "fx": None,
        },
        {
            "je_id": "je:auto:doc1:fin", "ts": "2026-01-20", "memo": "Invoice INV-1",
            "status": "posted", "je_type": None, "void_reason": None,
            "source_doc": {"doc_id": "doc1", "doc_ref": "INV-1"},
            "lines": [
                {"account": "1120", "name": "Accounts Receivable", "debit": 350.0, "credit": 0.0,
                 "fx_currency": "USD", "fx_rate": 35.0},
                {"account": "4100", "name": "Sales Revenue", "debit": 0.0, "credit": 350.0,
                 "fx_currency": "USD", "fx_rate": 35.0},
            ],
            # A document-linked entry is one currency for the whole document, so
            # every line carries the same pair.
            "fx": {"currency": "USD", "rate": 35.0},
        },
    ],
    "total_debit": 450.0, "total_credit": 450.0,
}

_GL = {
    "date_from": "", "date_to": "",
    "rows": [
        {"code": "1111", "name": "Bank", "account_type": "asset", "debit_normal": True,
         "opening": 100.0, "debit": 40.0, "credit": 0.0, "closing": 140.0},
        {"code": "4100", "name": "Sales Revenue", "account_type": "revenue", "debit_normal": False,
         "opening": 100.0, "debit": 0.0, "credit": 40.0, "closing": 140.0},
    ],
    "totals": {"opening": 0.0, "debit": 40.0, "credit": 40.0, "closing": 0.0},
    "balanced": True,
}

_SOA = {
    "contact": {"id": "contact:9", "name": "Acme Ltd", "type": "customer"},
    "date_from": "", "date_to": "",
    "opening_balance": 0.0,
    "rows": [
        {"date": "2026-01-05", "doc_id": "doc1", "doc_ref": "INV-1", "kind": "invoice",
         "debit": 100.0, "credit": 0.0, "balance": 100.0},
        {"date": "2026-01-20", "doc_id": "doc1", "doc_ref": "INV-1", "kind": "payment",
         "debit": 0.0, "credit": 40.0, "balance": 60.0},
    ],
    "closing_balance": 60.0,
}

_LEDGER = {
    "account_code": "1111", "account_name": "Bank", "account_type": "asset",
    "date_from": "", "date_to": "",
    "lines": [{"date": "2026-01-15", "je_id": "je:manual:abc", "memo": "Adjustment",
               "doc_id": None, "doc_ref": None, "debit": 100.0, "credit": 0.0,
               "balance": 100.0}],
}

_CHART = [{"code": "1111", "name": "Bank", "account_type": "asset", "is_active": True},
          {"code": "4100", "name": "Sales Revenue", "account_type": "revenue", "is_active": True}]


def _patches(**overrides):
    mocks = {
        "get_company": _COMPANY,
        "get_journal": overrides.get("journal", _JOURNAL),
        # The two journal pages read the same shape from two endpoints, so the
        # default stub covers both; a test that cares patches the one it exercises.
        "get_extended_journal": _JOURNAL,
        "get_general_ledger": _GL,
        "get_soa": _SOA,
        "get_ledger": _LEDGER,
        "get_trial_balance": {"lines": [], "total_debit": 0, "total_credit": 0, "balanced": True},
        "get_chart": {"items": _CHART, "total": len(_CHART)},
        "list_contacts": {"items": [{"id": "contact:9", "name": "Acme Ltd",
                                     "contact_type": "customer"}], "total": 1},
        "get_ar_aging": {"as_of": "2026-07-22", "lines": [
            {"customer_id": "contact:9", "customer_name": "Acme Ltd",
             "current": 60.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0,
             "total": 60.0}], "buckets": {}},
        "get_ap_aging": {"as_of": "2026-07-22", "lines": []},
    }
    mocks.update(overrides)
    patchers = [patch(f"ui.api_client.{name}", new=AsyncMock(return_value=val))
                for name, val in mocks.items() if name != "journal"]
    return patchers


async def _get(ui_client, url, **overrides):
    ps = _patches(**overrides)
    for p in ps:
        p.start()
    try:
        return await ui_client.get(url, cookies=_cookies())
    finally:
        for p in ps:
            p.stop()


async def _post(ui_client, url, data, **overrides):
    ps = _patches(**overrides)
    for p in ps:
        p.start()
    try:
        return await ui_client.post(url, cookies=_cookies(), data=data)
    finally:
        for p in ps:
            p.stop()


# ---------------------------------------------------------------------------
# Tab pages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_journal_page_renders(ui_client):
    r = await _get(ui_client, "/accounting")
    assert r.status_code == 200
    html = r.text
    # No tab bar: the journal is the only page under Accounting now, and the
    # reports it used to sit beside live under /reports.
    assert "tab-bar" not in html
    assert "Adjustment" in html
    assert "INV-1" in html
    # Newest entry (2026-01-20) renders before the older one
    assert html.index("2026-01-20") < html.index("2026-01-15")
    # Every posted entry offers void; the server explains any refusal
    assert "void" in html.lower()


@pytest.mark.asyncio
async def test_journal_tab_shows_fx_columns(ui_client):
    """Auditors need the foreign amounts and the rate used per transaction on
    the journal itself, not only in the export."""
    r = await _get(ui_client, "/accounting?tab=journal")
    assert r.status_code == 200
    html = r.text
    assert "FX Debit" in html and "FX Credit" in html and "Rate" in html
    assert "35" in html  # the transaction's exchange rate
    # 350.00 base at 35.0 is 10.00 in the foreign currency
    assert "10.00" in html


@pytest.mark.asyncio
async def test_journal_tab_hides_fx_columns_without_fx(ui_client):
    """A single-currency journal keeps its original narrow shape: no empty
    foreign-currency columns for businesses that never trade in one."""
    plain = json.loads(json.dumps(_JOURNAL))
    for entry in plain["entries"]:
        entry["fx"] = None
        for line in entry["lines"]:
            line["fx_currency"] = None
            line["fx_rate"] = None
    r = await _get(ui_client, "/accounting?tab=journal", get_journal=plain)
    assert r.status_code == 200
    assert "FX Debit" not in r.text and "FX Credit" not in r.text


@pytest.mark.asyncio
async def test_journal_print_shows_fx_columns(ui_client):
    r = await _get(ui_client, "/accounting/print/journal")
    assert r.status_code == 200
    assert "FX Debit" in r.text and "FX Credit" in r.text


@pytest.mark.asyncio
async def test_void_control_shown_for_auto_entries(ui_client):
    """The control stays on auto-posted entries; the server explains the refusal.
    Hiding it would leave the user guessing why an entry cannot be voided."""
    r = await _get(ui_client, "/accounting?tab=journal")
    assert r.status_code == 200
    # Two posted entries in the fixture, one manual and one auto-generated;
    # both carry a void control.
    assert r.text.count("/void") >= 2


# ---------------------------------------------------------------------------
# Voiding entries from the page: one at a time, or a selection
# ---------------------------------------------------------------------------

_VOID_ONE = {"je_id": "je:manual:abc", "status": "void", "void_reason": "Keyed twice"}
_VOID_BATCH = {"results": [{"je_id": "je:manual:abc", "status": "void",
                            "void_reason": "Duplicate batch"}],
               "voided": 1, "refused": 0}


def _toast(response) -> dict:
    return json.loads(response.headers["hx-trigger"])["celerpToast"]


@pytest.mark.asyncio
async def test_journal_page_selects_entries_not_postings(ui_client):
    """A selection is of entries: the fixture holds two posted entries with four
    postings between them, and offers two boxes, on the entry rows."""
    r = await _get(ui_client, "/accounting")
    assert r.status_code == 200
    assert r.text.count('class="bulk-select"') == 2
    assert 'value="je:manual:abc"' in r.text
    # The header box that ticks them all, and the bar that acts on them.
    assert "bulk-select-all" in r.text
    assert t("acct.bulk_void") in r.text
    assert "/accounting/journal/bulk-void" in r.text


@pytest.mark.asyncio
async def test_void_reason_hides_below_the_bar_until_void_is_chosen(ui_client):
    """The optional reason is not a popup and does not crowd the action list: it
    sits in a hidden group below the bar, and the void action is marked so the JS
    reveals that group only when the reader picks void, with its own confirm."""
    r = await _get(ui_client, "/accounting")
    assert r.status_code == 200
    # The reason is a narrowed field inside the reveal group, not inline in the bar.
    assert "bulk-fields" in r.text
    assert "bulk-field--reason" in r.text
    assert t("acct.void_reason_optional") in r.text
    # The action is flagged so the toolbar defers instead of firing at once, and
    # the group carries its own way to confirm and to back out.
    assert '"fields": true' in r.text
    assert "bulk-apply" in r.text and "bulk-cancel" in r.text
    assert "bulk-fields--open" in r.text  # the JS that reveals it ships on the page


@pytest.mark.asyncio
async def test_journal_page_offers_no_boxes_on_voided_entries(ui_client):
    """A voided entry has nothing left to void, so it keeps the column and
    leaves it empty rather than offering an action that would be refused."""
    voided = json.loads(json.dumps(_JOURNAL))
    for entry in voided["entries"]:
        entry["status"] = "void"
        entry["void_reason"] = "Reversed"
    r = await _get(ui_client, "/accounting", get_journal=voided)
    assert r.status_code == 200
    assert 'class="bulk-select"' not in r.text
    # The column is still there, so the rows still line up under the header.
    assert "col-checkbox" in r.text


@pytest.mark.asyncio
async def test_bulk_void_posts_every_ticked_entry_once(ui_client):
    """The ids the reader ticked and the reason from the bar reach the API as
    they were given: the bar's field travels with the selection."""
    calls = []

    async def _capture(token, je_ids, reason=None):
        calls.append((list(je_ids), reason))
        return {"results": [{"je_id": i, "status": "void"} for i in je_ids],
                "voided": len(je_ids), "refused": 0}

    ps = _patches()
    ps.append(patch("ui.api_client.bulk_void_journal_entries", new=_capture))
    for p in ps:
        p.start()
    try:
        r = await ui_client.post(
            "/accounting/journal/bulk-void?date_from=2026-01-01&date_to=2026-01-31",
            cookies=_cookies(),
            data={"selected": ["je:manual:abc", "je:auto:doc1:fin"],
                  "reason": "Duplicate batch"})
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert calls == [(["je:manual:abc", "je:auto:doc1:fin"], "Duplicate batch")]


@pytest.mark.asyncio
async def test_journal_bulk_void_refreshes_the_totals(ui_client):
    """The figures over the rows come back with the rows, out of band, so a void
    cannot leave the totals from before it standing over the table after it."""
    r = await _post(ui_client,
                    "/accounting/journal/bulk-void?date_from=2026-01-01&date_to=2026-01-31",
                    {"selected": "je:manual:abc", "reason": "Duplicate batch"},
                    bulk_void_journal_entries=_VOID_BATCH)
    assert r.status_code == 200
    assert 'id="journal-table"' in r.text
    assert 'id="journal-totals"' in r.text and 'hx-swap-oob="true"' in r.text
    # The classical book asked, so the classical book comes back.
    assert "Unit Price" not in r.text
    assert _toast(r) == {"message": t("acct.bulk_void_result", n=1), "type": "success"}


@pytest.mark.asyncio
async def test_bulk_void_toast_states_what_was_refused(ui_client):
    """A batch where something was refused is an error, and says how many and
    why in the words of the rule that refused it. The rest were still voided,
    and the count says so rather than leaving the reader to re-tick and retry."""
    detail = "Only manual journal entries can be voided here. Undo the source document instead."
    mixed = {"results": [{"je_id": "je:manual:abc", "status": "void", "void_reason": None},
                         {"je_id": "je:auto:doc1:fin", "status": "refused", "detail": detail}],
             "voided": 1, "refused": 1}
    r = await _post(ui_client, "/accounting/journal/bulk-void",
                    {"selected": "je:manual:abc"}, bulk_void_journal_entries=mixed)
    assert r.status_code == 200
    toast = _toast(r)
    assert toast["type"] == "error"
    assert t("acct.bulk_void_result", n=1) in toast["message"]
    assert detail in toast["message"]


@pytest.mark.asyncio
async def test_bulk_void_refusal_from_the_api_reaches_the_reader(ui_client):
    """A refusal of the whole request, such as a selection over the cap, is
    reported in the API's own words and swaps nothing away."""
    denied = AsyncMock(side_effect=APIError(422, "Too many journal entries in one request: 201."))
    ps = _patches()
    ps.append(patch("ui.api_client.bulk_void_journal_entries", new=denied))
    for p in ps:
        p.start()
    try:
        r = await ui_client.post("/accounting/journal/bulk-void", cookies=_cookies(),
                                 data={"selected": "je:manual:abc"})
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert r.headers["hx-reswap"] == "none"
    assert "201" in _toast(r)["message"]


@pytest.mark.asyncio
async def test_single_void_swaps_the_view_instead_of_reloading(ui_client):
    """One entry is a batch of one: it answers with the table and its totals and
    reports the same way, rather than reloading the page under the reader."""
    r = await _post(ui_client,
                    "/accounting/journal/je:manual:abc/void?date_from=2026-01-01&q=1111",
                    {"reason": "Keyed twice"}, void_journal_entry=_VOID_ONE)
    assert r.status_code == 200
    assert "hx-redirect" not in {k.lower() for k in r.headers}
    assert 'id="journal-table"' in r.text
    assert 'id="journal-totals"' in r.text and 'hx-swap-oob="true"' in r.text
    assert _toast(r) == {"message": t("acct.bulk_void_result", n=1), "type": "success"}


@pytest.mark.asyncio
async def test_bulk_void_response_keeps_the_filter_and_the_item_mode(ui_client):
    """The answer re-reads the journal the reader was looking at: the extended
    book when the void came from there, narrowed the way it was narrowed, with
    the way back pointing at the page it was fired from."""
    filtered = {**json.loads(json.dumps(_JOURNAL)), "filtered": True}
    r = await _post(
        ui_client,
        "/accounting/journal/bulk-void?items=1&date_from=2026-01-01&q=1111",
        {"selected": "je:manual:abc"},
        bulk_void_journal_entries=_VOID_BATCH, get_extended_journal=filtered)
    assert r.status_code == 200
    # Item, quantity and unit price: the extended book, not the classical one.
    assert "Unit Price" in r.text
    assert t("label.search") in r.text and "1111" in r.text
    assert "/reports/extended-journal" in r.text


@pytest.mark.asyncio
async def test_je_form_renders(ui_client):
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text
    assert "idempotency_token" in html
    assert "val-chip" in html
    assert "combobox" in html or "searchable" in html


@pytest.mark.asyncio
async def test_je_form_not_authorized_below_manager(ui_client):
    """A viewer typing the form URL gets the not-authorized banner, not a
    usable entry form."""
    ps = _patches()
    for p in ps:
        p.start()
    try:
        r = await ui_client.get("/accounting/journal/new", cookies=_cookies(role="viewer"))
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert t("acct.not_authorized") in r.text
    assert "idempotency_token" not in r.text


@pytest.mark.asyncio
async def test_journal_csv_flat_ascending_with_fx(ui_client):
    r = await _get(ui_client, "/accounting/export/journal/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="journal_' in r.headers["content-disposition"]
    lines = [l for l in r.text.strip().splitlines() if l]
    header = lines[0].split(",")
    assert header == ["date", "entry_id", "source_ref", "memo", "account_code", "account_name",
                      "debit", "credit", "currency", "fx_currency", "fx_debit", "fx_credit",
                      "exchange_rate", "status"]
    # One ledger line per row: 2 entries x 2 lines = 4 data rows
    assert len(lines) == 5
    # Ascending by date, entry fields repeated per line (pivot-friendly)
    assert lines[1].startswith("2026-01-15") and lines[3].startswith("2026-01-20")
    # FX columns on the FX entry, blank on the base one
    assert "USD" in lines[3] and "35.0" in lines[3]
    assert "USD" not in lines[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
async def test_journal_csv_neutralizes_formula_memo(ui_client, lead):
    poisoned = json.loads(json.dumps(_JOURNAL))
    poisoned["entries"][0]["memo"] = f"{lead}SUM(A1:A2)"
    r = await _get(ui_client, "/accounting/export/journal/csv", get_journal=poisoned)
    assert r.status_code == 200
    assert f"'{lead}SUM(A1:A2)" in r.text


@pytest.mark.asyncio
async def test_je_form_conflict_rerenders_with_fresh_token(ui_client):
    conflict = AsyncMock(side_effect=APIError(409, "already posted"))
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.create_journal_entry", new=conflict):
            r = await ui_client.post("/accounting/journal/new", cookies=_cookies(), data={
                "ts": "2026-01-15", "memo": "x", "idempotency_token": "stale-token",
                "account_0": "1111", "debit_0": "10", "credit_0": "0",
                "account_1": "4100", "debit_1": "0", "credit_1": "10",
            })
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert "already posted" in r.text.lower() or "review the journal" in r.text.lower()
    assert "stale-token" not in r.text  # a fresh token replaces the burnt one


# ---------------------------------------------------------------------------
# Journal filters
# ---------------------------------------------------------------------------

_FILTERS_QS = "q=rent"
# On-screen pages take the period as from/to; print sheets and exports take it as
# date_from/date_to, which is the convention across every report here.
_FILTER_QS = f"from=2026-01-01&to=2026-03-31&{_FILTERS_QS}"
_EXPORT_QS = f"date_from=2026-01-01&date_to=2026-03-31&{_FILTERS_QS}"


def _filtered_payload(*, entries: int = 1) -> dict:
    """What the API answers for a search that matched `entries` of the two."""
    out = json.loads(json.dumps(_JOURNAL))
    # The INV-1 entry: account 4100, 350.00, so the payload agrees with the filter
    # the tests ask for.
    out["entries"] = out["entries"][1:1 + entries]
    out["total_debit"] = out["total_credit"] = 350.0 * entries
    out["filtered"] = True
    return out


async def _get_spied(ui_client, url, name, mock):
    """A page fetch with one API call replaced by a mock we can question."""
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch(f"ui.api_client.{name}", new=mock):
            return await ui_client.get(url, cookies=_cookies())
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("page,call", [("/accounting", "get_journal"),
                                       ("/reports/extended-journal", "get_extended_journal")])
async def test_filtered_page_says_it_is_filtered(ui_client, page, call):
    """A filtered page must never read as the whole book: it states the search in
    words, offers the way back, and the totals it shows are of what it shows."""
    spy = AsyncMock(return_value=_filtered_payload())
    r = await _get_spied(ui_client, f"{page}?{_FILTER_QS}", call, spy)
    assert r.status_code == 200
    html = r.text
    assert t("acct.filtered_totals") in html
    assert "rent" in html
    assert t("acct.filter_clear") in html
    # The one search box comes back holding what was asked for, so the reader can
    # see and adjust it rather than re-typing it.
    assert 'value="rent"' in html
    assert "journal-search" in html and t("acct.journal_search") in html
    # ESC empties the box (GDR 2j) and Enter appends a comma (the inventory pattern).
    assert "Escape" in html and "Enter" in html
    # The one search reached the API with the period, and the three old field
    # names are gone from the call.
    params = spy.await_args.args[1]
    assert params["q"] == "rent"
    assert "account" not in params and "amount" not in params
    assert params["date_from"] == "2026-01-01" and params["date_to"] == "2026-03-31"


@pytest.mark.asyncio
@pytest.mark.parametrize("page,call", [("/accounting", "get_journal"),
                                       ("/reports/extended-journal", "get_extended_journal")])
async def test_no_matches_state_differs_from_empty_period(ui_client, page, call):
    """"Nothing here" and "nothing matches" are different facts. Saying the first
    when the second is true sends the reader looking for missing bookkeeping."""
    matched_none = _filtered_payload(entries=0)
    r = await _get_spied(ui_client, f"{page}?{_FILTER_QS}", call,
                         AsyncMock(return_value=matched_none))
    assert r.status_code == 200
    assert t("acct.no_matches") in r.text
    assert t("acct.no_journal_entries") not in r.text

    empty_period = json.loads(json.dumps(_JOURNAL))
    empty_period["entries"] = []
    empty_period["total_debit"] = empty_period["total_credit"] = 0.0
    r = await _get_spied(ui_client, page, call, AsyncMock(return_value=empty_period))
    assert r.status_code == 200
    assert t("acct.no_journal_entries") in r.text
    assert t("acct.no_matches") not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stem,print_url,csv_url,call", [
    ("journal", "/accounting/print/journal", "/accounting/export/journal/csv", "get_journal"),
    ("extended_journal", "/reports/print/extended-journal",
     "/reports/export/extended-journal/csv", "get_extended_journal"),
])
async def test_journal_print_and_csv_carry_the_filter(ui_client, stem, print_url, csv_url, call):
    """Paper and a spreadsheet outlive the page they came from. Both have to say
    they hold a slice, and both have to hold the slice the screen showed."""
    spy = AsyncMock(return_value=_filtered_payload())
    r = await _get_spied(ui_client, f"{print_url}?{_EXPORT_QS}", call, spy)
    assert r.status_code == 200
    assert t("acct.filtered_totals") in r.text
    assert "rent" in r.text
    assert spy.await_args.args[1]["q"] == "rent"

    spy = AsyncMock(return_value=_filtered_payload())
    r = await _get_spied(ui_client, f"{csv_url}?{_EXPORT_QS}", call, spy)
    assert r.status_code == 200
    # The filename says it is a slice; the search text stays out of the header,
    # and the single header row stays what a spreadsheet reads as column names.
    assert f'filename="{stem}_2026-01-01_2026-03-31_filtered.csv"' in \
        r.headers["content-disposition"]
    assert "rent" not in r.headers["content-disposition"]
    assert r.text.strip().splitlines()[0].startswith("date,")
    assert spy.await_args.args[1]["q"] == "rent"
    # Only the filtered entry is in the file: one entry, two postings.
    assert len([row for row in r.text.strip().splitlines() if row]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("csv_url,call", [
    ("/accounting/export/journal/csv", "get_journal"),
    ("/reports/export/extended-journal/csv", "get_extended_journal"),
])
async def test_journal_csv_rejects_an_over_long_search_with_422(ui_client, csv_url, call):
    """A search the server refuses (only the length cap can refuse one now) is the
    reader's to correct. Answering it as a 500 turns a typo into an outage."""
    refused = AsyncMock(side_effect=APIError(422, "Search text is 201 characters, longer than the 200 the journal searches."))
    r = await _get_spied(ui_client, f"{csv_url}?q={'x' * 201}", call, refused)
    # The route has to hand the search over to be refused in the first place.
    assert refused.await_args.args[1]["q"] == "x" * 201
    assert r.status_code == 422
    assert "longer than" in r.text


def test_date_filter_bar_carries_encoded_values():
    from urllib.parse import quote_plus
    from fasthtml.common import to_xml
    from ui.routes.reports import _date_filter_bar
    html = to_xml(_date_filter_bar("/docs", "", "", "all",
                                   extra_params=f"&q={quote_plus('nuts & bolts')}"))
    assert 'value="nuts &amp; bolts"' in html or "value='nuts &amp; bolts'" in html


# ---------------------------------------------------------------------------
# Print views
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forbidden_renders_clean(ui_client):
    denied = AsyncMock(side_effect=APIError(403, "Forbidden"))
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.get_journal", new=denied):
            page = await ui_client.get("/accounting?tab=journal", cookies=_cookies(role="viewer"))
            export = await ui_client.get("/accounting/export/journal/csv", cookies=_cookies(role="viewer"))
    finally:
        for p in ps:
            p.stop()
    assert page.status_code == 200
    assert t("acct.not_authorized") in page.text  # the clean banner, not a raw error
    assert export.status_code == 403
    assert export.status_code != 500


# ---------------------------------------------------------------------------
# i18n: every new key exists in every locale
# ---------------------------------------------------------------------------

_NEW_KEYS = [
    "acct.tab_journal", "acct.tab_general_ledger", "acct.tab_soa",
    "th.opening", "th.closing", "th.source",
    "btn.new_entry", "btn.post",
    "acct.source_manual", "acct.source_transfer", "acct.source_reconciliation",
    "acct.source_system", "acct.no_journal_entries", "acct.new_journal_entry",
    "acct.not_authorized", "acct.soa_pick_contact", "acct.soa_title",
    "acct.closing_balance", "acct.err_amounts_numeric", "acct.memo_hint",
    "acct.void_reason_optional", "acct.soa_kind_payment",
    "acct.journal_search", "acct.journal_search_hint",
    "acct.no_entries_for_account",
    "acct.record_foreign_currency", "acct.currency_base_hint",
    "acct.imbalance_note", "acct.fx_per_line_hint",
    "acct.err_currency_unknown", "acct.err_rate_positive",
    "acct.bulk_void", "acct.bulk_void_confirm", "acct.bulk_void_result",
    "acct.bulk_void_refused", "label.n_selected", "label.select_all",
    "label.select_rows_first",
]


def test_new_locale_keys_present_everywhere():
    locales_dir = os.path.join(os.path.dirname(__file__), "../../../ui/locales")
    files = [f for f in os.listdir(locales_dir) if f.endswith(".json")]
    assert "en.json" in files
    for fname in files:
        with open(os.path.join(locales_dir, fname), encoding="utf-8") as fh:
            data = json.load(fh)
        missing = [k for k in _NEW_KEYS if not data.get(k)]
        assert not missing, f"{fname} missing locale keys: {missing}"


@pytest.mark.asyncio
async def test_je_form_every_text_field_exits_on_escape(ui_client):
    """GDR 2j: Escape exits every field on the entry form, memo included.

    The memo input was the one field left without a handler while its
    siblings all had one, so Escape worked everywhere except the field a
    user is most likely to be typing prose into.
    """
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text

    import re as _re
    for name in ("ts", "memo", "debit_0", "credit_0"):
        m = _re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
        assert m, f"no input named {name} in the entry form"
        assert "Escape" in m.group(0), f'input {name} has no Escape handler: {m.group(0)}'


@pytest.mark.asyncio
async def test_print_views_center_column_headers(ui_client):
    """Printed tables follow the same header rule as the screen.

    The print stylesheet left-aligned every header while the on-screen tables
    centered them, so the same report read differently on paper than it did in
    the browser. Headers over a right-aligned column stay right-aligned in both,
    whichever of the two right-aligning cell classes the column carries.
    """
    for url in ("/accounting/print/journal", "/reports/print/general-ledger",
                "/reports/print/trial-balance"):
        r = await _get(ui_client, url)
        assert r.status_code == 200, url
        assert "thead th { background: #f5f5f5; font-weight: 700; text-align: center;" in r.text, url
        assert ("thead th.cell--number, thead th.cell--right { text-align: right; }"
                in r.text), url


@pytest.mark.asyncio
async def test_je_form_shows_foreign_currency_control(ui_client):
    """The control exists but stays collapsed, so an accountant who never posts
    in a foreign currency sees the form they saw before."""
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text
    assert "Record in a foreign currency" in html
    # Currency and rate belong to the line, not the entry: an entry can carry
    # more than one currency.
    assert 'name="currency_0"' in html and 'name="rate_0"' in html
    assert 'name="currency"' not in html and 'name="rate"' not in html
    # Collapsed: a <details> with no open attribute on the reveal itself.
    assert "je-fx-reveal" in html
    assert "<details open" not in html.replace("<details  open", "<details open")


@pytest.mark.asyncio
async def test_je_form_line_rate_input_exits_on_escape(ui_client):
    """GDR 2j: Escape exits the per-line rate field like every other field."""
    r = await _get(ui_client, "/accounting/journal/new")
    import re as _re
    m = _re.search(r'<input[^>]*name="rate_0"[^>]*>', r.text)
    assert m and "Escape" in m.group(0), m.group(0) if m else "no line rate input"


@pytest.mark.asyncio
async def test_je_form_submit_sends_line_fx_only_when_a_currency_is_chosen(ui_client):
    """A line with no currency posts the request it posted before the columns
    existed: no currency key and no rate key."""
    from unittest.mock import AsyncMock, patch

    base = {
        "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-fx-1",
        "account_0": "1111", "debit_0": "10", "credit_0": "",
        "account_1": "4100", "debit_1": "", "credit_1": "10",
    }
    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        await _post(ui_client, "/accounting/journal/new", {
            **base, "currency_0": "USD", "rate_0": "35"})
        entries = m.call_args[0][1]["entries"]
        assert entries[0]["currency"] == "USD" and entries[0]["rate"] == 35.0
        assert "currency" not in entries[1] and "rate" not in entries[1]
        assert "fx" not in m.call_args[0][1]

    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        await _post(ui_client, "/accounting/journal/new", {**base, "idempotency_token": "tok-fx-2"})
        entries = m.call_args[0][1]["entries"]
        assert all("currency" not in e and "rate" not in e for e in entries)


@pytest.mark.asyncio
async def test_je_form_carries_a_different_currency_on_each_line(ui_client):
    """Two currencies in one entry is the case the per-line columns exist for."""
    from unittest.mock import AsyncMock, patch

    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        await _post(ui_client, "/accounting/journal/new", {
            "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-fx-mixed",
            "account_0": "1111", "debit_0": "100", "credit_0": "",
            "currency_0": "USD", "rate_0": "35",
            "account_1": "4100", "debit_1": "", "credit_1": "3000",
            "currency_1": "JPY", "rate_1": "0.23",
        })
        entries = m.call_args[0][1]["entries"]
        assert entries[0]["currency"] == "USD" and entries[1]["currency"] == "JPY"


@pytest.mark.asyncio
async def test_je_form_rejects_unknown_currency_without_calling_the_api(ui_client):
    from unittest.mock import AsyncMock, patch

    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        r = await _post(ui_client, "/accounting/journal/new", {
            "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-fx-3",
            "account_0": "1111", "debit_0": "10", "credit_0": "",
            "account_1": "4100", "debit_1": "", "credit_1": "10",
            "currency_0": "QQQ", "rate_0": "35",
        })
        assert r.status_code == 200
        assert m.await_count == 0


def test_fx_line_amounts_prefers_stored_over_derived():
    """A typed foreign figure is what the document says; division can miss it."""
    from ui.components.report_kit import fx_line_amounts

    stored = fx_line_amounts(300.25, 0, {"fx_currency": "USD", "fx_rate": 3.0025,
                                         "fx_debit": 100.0, "fx_credit": None})
    assert stored == (100.0, None)


def test_fx_line_amounts_blank_when_rate_absent():
    """No rate means no figure, not a guessed one."""
    from ui.components.report_kit import fx_line_amounts

    assert fx_line_amounts(100.0, 0, {"fx_currency": "USD", "fx_rate": 0}) == (None, None)


def test_fx_line_amounts_blank_when_currency_missing():
    """A rate with no currency cannot be formatted: rounding it at a defaulted
    precision and showing it under an unknown currency is a fabricated figure."""
    from ui.components.report_kit import fx_line_amounts

    assert fx_line_amounts(100.0, 0, {"fx_currency": None, "fx_rate": 35.0}) == (None, None)
    assert fx_line_amounts(100.0, 0, {"fx_rate": 35.0}) == (None, None)


def test_plain_error_response_keeps_a_4xx_status():
    """A print or export asked for with a bad filter is an input error the reader
    can correct, and reporting it as a 500 turns a typo into an outage."""
    from ui.api_client import APIError
    from ui.components.report_kit import plain_error_response

    refused = plain_error_response(APIError(422, "Search text is 201 characters, longer than the 200 the journal searches."))
    assert refused.status_code == 422
    assert b"longer than" in refused.body
    assert plain_error_response(APIError(404, "gone")).status_code == 404
    # Anything the reader cannot correct still reads as a server error.
    assert plain_error_response(APIError(503, "upstream")).status_code == 500
    assert plain_error_response(APIError(403, "no")).status_code == 403


@pytest.mark.asyncio
async def test_je_form_keeps_line_currency_and_rate_after_a_failed_submit(ui_client):
    """A rejected entry re-renders with the foreign values still typed in and
    the control open, rather than silently discarding them."""
    r = await _post(ui_client, "/accounting/journal/new", {
        "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-keep-1",
        "account_0": "1111", "debit_0": "10", "credit_0": "",
        "account_1": "4100", "debit_1": "", "credit_1": "10",
        "currency_0": "QQQ", "rate_0": "35.5",
    })
    assert r.status_code == 200
    html = r.text
    # The reveal is reopened so the user can see what was refused.
    import re as _re
    tag = _re.search(r'<details[^>]*id="je-fx-reveal"[^>]*>', html)
    assert tag and "open" in tag.group(0), tag.group(0) if tag else "no reveal"
    assert 'value="35.5"' in html


@pytest.mark.asyncio
async def test_je_form_splits_book_columns_and_names_the_book_currency(ui_client):
    """The book side reads like a journal: two computed debit/credit columns in
    the company currency, each header naming it, not one merged amount."""
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text
    assert "je-base-debit" in html and "je-base-credit" in html
    assert f'{t("th.debit")} (THB)' in html and f'{t("th.credit")} (THB)' in html
    # The merged cell class is gone entirely.
    assert 'je-local"' not in html
    assert "je-local-debit" not in html and "je-total-local-debit" not in html


@pytest.mark.asyncio
async def test_je_form_js_knows_the_book_currency_and_its_decimals(ui_client):
    """The preview rounds each converted amount where the server will, so the
    decimal places come from the server rather than a hardcoded two."""
    r = await _get(ui_client, "/accounting/journal/new")
    html = r.text
    assert 'celerpJeBase = "THB"' in html
    assert '"JPY": 0' in html and '"KWD": 3' in html
    # The old fixed-cent arithmetic is gone.
    assert "* 100)" not in html.split("celerpJeTotals")[-1]


@pytest.mark.asyncio
async def test_je_form_hides_the_foreign_columns_until_the_reveal_opens(ui_client):
    """Discretion: the columns exist in the markup but the script hides them
    while the reveal is shut, so the plain form looks exactly as it did."""
    r = await _get(ui_client, "/accounting/journal/new")
    html = r.text
    assert "je-fx-col" in html
    assert "cell.hidden = !details.open" in html


# ---------------------------------------------------------------------------
# Statement batch print run
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Naming the party on a journal line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_je_form_offers_a_party_on_every_line(ui_client):
    """A line posted to a control account belongs to someone, so the form has to
    let the person posting it say who. Searchable, because a company's contact
    list runs past ten."""
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text
    assert t("acct.je_line_party") in html
    assert 'name="contact_0"' in html and 'name="contact_1"' in html
    # The picker searches the whole contact list rather than the page it opened on.
    assert "/contacts/search-options?contact_type=all&amp;add_new=0" in html
    # Both the rendered rows and the template a new row is cloned from.
    assert 'name="contact___IDX__"' in html


@pytest.mark.asyncio
async def test_je_form_sends_the_chosen_party_and_omits_an_unchosen_one(ui_client):
    """The contact key reaches the API only when someone picked a party. A line
    nobody named posts the request it posted before the column existed, so an
    empty picker can never attribute an entry to anyone."""
    created = AsyncMock(return_value={"je_id": "je:manual:x"})
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.create_journal_entry", new=created):
            r = await ui_client.post("/accounting/journal/new", cookies=_cookies(), data={
                "ts": "2026-01-15", "memo": "Rebill", "idempotency_token": "tok-party",
                "account_0": "1120", "debit_0": "10", "credit_0": "0",
                "contact_0": "contact:9",
                "account_1": "4100", "debit_1": "0", "credit_1": "10",
                "contact_1": "",
            })
    finally:
        for p in ps:
            p.stop()
    assert r.status_code in (200, 303)
    entries = created.call_args[0][1]["entries"]
    assert entries[0]["contact"] == "contact:9"
    assert "contact" not in entries[1]


@pytest.mark.asyncio
async def test_je_form_keeps_a_chosen_party_through_a_failed_post(ui_client):
    """A rejected entry comes back with what was typed still in it. Losing the
    party on the way back would have the user re-pick it with nothing saying so."""
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.create_journal_entry",
                   new=AsyncMock(side_effect=APIError(409, "already posted"))):
            r = await ui_client.post("/accounting/journal/new", cookies=_cookies(), data={
                "ts": "2026-01-15", "memo": "Rebill", "idempotency_token": "stale",
                "account_0": "1120", "debit_0": "10", "credit_0": "0",
                "contact_0": "contact:9",
                "account_1": "4100", "debit_1": "0", "credit_1": "10",
            })
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    assert 'value="contact:9"' in r.text
