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
                {"account": "1120", "name": "Accounts Receivable", "debit": 350.0, "credit": 0.0},
                {"account": "4100", "name": "Sales Revenue", "debit": 0.0, "credit": 350.0},
            ],
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
    "acct.no_entries_for_account",
    "acct.record_foreign_currency", "acct.currency_base_hint",
    "acct.rounding_line_preview", "acct.err_currency_unknown", "acct.err_rate_positive",
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
    assert 'name="currency"' in html and 'name="rate"' in html
    assert 'value="1.0000"' in html
    # Collapsed: a <details> with no open attribute on the reveal itself.
    assert "je-fx-reveal" in html
    assert "<details open" not in html.replace("<details  open", "<details open")


@pytest.mark.asyncio
async def test_je_form_rate_input_exits_on_escape(ui_client):
    """GDR 2j: Escape closes the whole disclosure, matching the void control."""
    r = await _get(ui_client, "/accounting/journal/new")
    import re as _re
    m = _re.search(r'<input[^>]*name="rate"[^>]*>', r.text)
    assert m and "Escape" in m.group(0), m.group(0) if m else "no rate input"


@pytest.mark.asyncio
async def test_je_form_submit_sends_fx_only_when_currency_chosen(ui_client):
    """An untouched reveal posts an ordinary entry with no fx key at all."""
    from unittest.mock import AsyncMock, patch

    base = {
        "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-fx-1",
        "account_0": "1111", "debit_0": "10", "credit_0": "",
        "account_1": "4100", "debit_1": "", "credit_1": "10",
    }
    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        await _post(ui_client, "/accounting/journal/new", {**base, "currency": "USD", "rate": "35"})
        payload = m.call_args[0][1]
        assert payload["fx"] == {"currency": "USD", "rate": 35.0}

    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        await _post(ui_client, "/accounting/journal/new", {**base, "idempotency_token": "tok-fx-2"})
        assert "fx" not in m.call_args[0][1]


@pytest.mark.asyncio
async def test_je_form_rejects_unknown_currency_without_calling_the_api(ui_client):
    from unittest.mock import AsyncMock, patch

    with patch("ui.api_client.create_journal_entry", new=AsyncMock(return_value={})) as m:
        r = await _post(ui_client, "/accounting/journal/new", {
            "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-fx-3",
            "account_0": "1111", "debit_0": "10", "credit_0": "",
            "account_1": "4100", "debit_1": "", "credit_1": "10",
            "currency": "QQQ", "rate": "35",
        })
        assert r.status_code == 200
        assert m.await_count == 0


def test_fx_line_amounts_prefers_stored_over_derived():
    """A typed foreign figure is what the document says; division can miss it."""
    from ui.routes.accounting import _fx_line_amounts

    stored = _fx_line_amounts(300.25, 0, {"currency": "USD", "rate": 3.0025},
                              100.0, None)
    assert stored == (100.0, None)


def test_fx_line_amounts_blank_when_rate_absent():
    """No rate means no figure, not a guessed one."""
    from ui.routes.accounting import _fx_line_amounts

    assert _fx_line_amounts(100.0, 0, {"currency": "USD", "rate": 0}) == (None, None)


@pytest.mark.asyncio
async def test_je_form_keeps_currency_and_rate_after_a_failed_submit(ui_client):
    """A rejected entry re-renders with the foreign values still typed in and
    the control open, rather than silently discarding them."""
    r = await _post(ui_client, "/accounting/journal/new", {
        "ts": "2026-03-01", "memo": "m", "idempotency_token": "tok-keep-1",
        "account_0": "1111", "debit_0": "10", "credit_0": "",
        "account_1": "4100", "debit_1": "", "credit_1": "10",
        "currency": "QQQ", "rate": "35.5",
    })
    assert r.status_code == 200
    html = r.text
    # The reveal is reopened so the user can see what was refused.
    assert '<details open class="je-fx-reveal"' in html
    assert 'value="35.5"' in html


def test_fx_line_amounts_blank_when_currency_missing():
    """A rate with no currency cannot be formatted: rounding it at a defaulted
    precision and showing it under an unknown currency is a fabricated figure."""
    from ui.routes.accounting import _fx_line_amounts

    assert _fx_line_amounts(100.0, 0, {"currency": None, "rate": 35.0}) == (None, None)
    assert _fx_line_amounts(100.0, 0, {"rate": 35.0}) == (None, None)


# ---------------------------------------------------------------------------
# FX entry form: split book-currency columns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_je_form_splits_book_columns_and_ids_the_headers(ui_client):
    """The book side reads like a journal: two computed debit/credit columns,
    not one merged amount, and every currency-bearing header carries an id the
    script can label with the currency code."""
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text
    for hid in ("je-debit-head", "je-credit-head",
                "je-local-debit-head", "je-local-credit-head"):
        assert f'id="{hid}"' in html, hid
    assert 'id="je-local-head"' not in html
    assert "je-local-debit" in html and "je-local-credit" in html
    # The merged cell class is gone entirely.
    assert 'je-local"' not in html


@pytest.mark.asyncio
async def test_je_form_js_knows_the_book_currency(ui_client):
    """The book-column labels need the company currency; it is injected
    server-side, never guessed client-side."""
    r = await _get(ui_client, "/accounting/journal/new")
    assert 'celerpJeBase = "THB"' in r.text


@pytest.mark.asyncio
async def test_je_form_renders_hidden_book_total_chips(ui_client):
    """Book totals exist as chips the script reveals only under a rate, so the
    plain form shows no empty pills."""
    import re as _re
    r = await _get(ui_client, "/accounting/journal/new")
    for cid in ("je-total-local-debit", "je-total-local-credit"):
        m = _re.search(rf'<span[^>]*id="{cid}"[^>]*>', r.text)
        assert m, cid
        assert "hidden" in m.group(0), m.group(0)


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
