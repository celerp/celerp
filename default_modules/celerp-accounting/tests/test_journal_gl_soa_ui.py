# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""UI surfaces for the journal, general ledger, SOA, and manual journal entries.

API responses are mocked (the API contract itself is covered in
test_journal_gl_soa.py); these tests pin the rendering contract: routes
resolve, tabs and tables render, CSV exports are flat/safe/dated, error
states are clean, and every new locale key exists in every locale file.
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
async def test_journal_tab_renders(ui_client):
    r = await _get(ui_client, "/accounting?tab=journal")
    assert r.status_code == 200
    html = r.text
    assert "tab-bar" in html
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
async def test_general_ledger_tab_renders(ui_client):
    r = await _get(ui_client, "/accounting?tab=general-ledger")
    assert r.status_code == 200
    assert "1111" in r.text and "4100" in r.text
    # Drilldown carries origin tab
    assert "src=general-ledger" in r.text


@pytest.mark.asyncio
async def test_soa_tab_renders(ui_client):
    r = await _get(ui_client, "/accounting?tab=soa&contact_id=contact:9")
    assert r.status_code == 200
    assert "Acme Ltd" in r.text
    assert "60" in r.text


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
async def test_soa_merge_substitution_is_disclosed(ui_client):
    """Landing on the merge winner's statement must say so, on screen and on
    anything printed or exported from it."""
    page = await _get(ui_client, "/accounting?tab=soa&contact_id=contact:9&merged_from=contact:old")
    assert page.status_code == 200
    assert "merged into" in page.text

    printed = await _get(ui_client,
                         "/accounting/print/soa?contact_id=contact:9&merged_from=contact:old")
    assert printed.status_code == 200
    assert "merged into" in printed.text

    csv_out = await _get(ui_client,
                         "/accounting/export/soa/csv?contact_id=contact:9&merged_from=contact:old")
    assert csv_out.status_code == 200
    assert "merged into" in csv_out.text


@pytest.mark.asyncio
async def test_soa_tab_without_contact_prompts(ui_client):
    r = await _get(ui_client, "/accounting?tab=soa")
    assert r.status_code == 200
    assert "empty-state" in r.text


@pytest.mark.asyncio
async def test_soa_merged_contact_redirects(ui_client):
    r = await _get(ui_client, "/accounting?tab=soa&contact_id=contact:old",
                   get_soa={"merged_into": "contact:new"})
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert "contact_id=contact%3Anew" in loc or "contact_id=contact:new" in loc
    # The contact that was asked for rides along so the destination can disclose
    # the substitution, but it is never the contact being shown.
    assert "merged_from" in loc


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
    assert "You need a manager role" in r.text
    assert "idempotency_token" not in r.text


@pytest.mark.asyncio
async def test_ledger_back_link_follows_origin(ui_client):
    r = await _get(ui_client, "/accounting/ledger/1111?src=general-ledger")
    assert r.status_code == 200
    assert "tab=general-ledger" in r.text
    r = await _get(ui_client, "/accounting/ledger/1111?src=pnl")
    assert "tab=pnl" in r.text
    r = await _get(ui_client, "/accounting/ledger/1111")
    assert "tab=trial-balance" in r.text


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

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
async def test_gl_and_soa_csv(ui_client):
    r = await _get(ui_client, "/accounting/export/general-ledger/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    r = await _get(ui_client, "/accounting/export/soa/csv?contact_id=contact:9")
    assert r.status_code == 200
    assert "Acme" in r.text or "60" in r.text


@pytest.mark.asyncio
async def test_trial_balance_export_passes_dates(ui_client):
    tb_mock = AsyncMock(return_value={"lines": [], "total_debit": 0, "total_credit": 0})
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.get_trial_balance", new=tb_mock):
            r = await ui_client.get(
                "/accounting/export/trial-balance/csv?date_from=2026-01-01&date_to=2026-01-31",
                cookies=_cookies())
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    called_params = tb_mock.call_args.args[1] if len(tb_mock.call_args.args) > 1 else tb_mock.call_args.kwargs.get("params", {})
    assert called_params.get("date_from") == "2026-01-01"
    assert called_params.get("date_to") == "2026-01-31"


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
            r = await ui_client.get("/accounting/export/general-ledger/csv", cookies=_cookies())
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


@pytest.mark.asyncio
async def test_ledger_custom_range_honored_with_src(ui_client):
    """The drilldown page's own custom form submits from/to; with src present
    those dates must reach the API instead of being dropped."""
    ledger_mock = AsyncMock(return_value=_LEDGER)
    ps = _patches()
    for p in ps:
        p.start()
    try:
        with patch("ui.api_client.get_ledger", new=ledger_mock):
            r = await ui_client.get(
                "/accounting/ledger/1111?src=general-ledger&from=2026-01-01&to=2026-01-31",
                cookies=_cookies())
    finally:
        for p in ps:
            p.stop()
    assert r.status_code == 200
    params = ledger_mock.call_args.kwargs.get("params") or ledger_mock.call_args.args[-1]
    assert params.get("date_from") == "2026-01-01"
    assert params.get("date_to") == "2026-01-31"
    assert "tab=general-ledger" in r.text  # back link still follows origin


@pytest.mark.asyncio
async def test_soa_print_merge_redirect_with_encoded_contact_id(ui_client):
    r = await _get(ui_client, "/accounting/print/soa?contact_id=contact%3Aold",
                   get_soa={"merged_into": "contact:new"})
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "contact_id=contact%3Anew" in loc or "contact_id=contact:new" in loc
    assert "contact_id=contact%3Aold" not in loc and "contact_id=contact:old" not in loc
    assert "merged_from" in loc


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
async def test_print_views_render(ui_client):
    for url in ("/accounting/print/journal", "/accounting/print/general-ledger",
                "/accounting/print/soa?contact_id=contact:9"):
        r = await _get(ui_client, url)
        assert r.status_code == 200, url
    # SOA print is an outward document: company, contact, closing balance
    assert "TestCo" in r.text and "Acme Ltd" in r.text


@pytest.mark.asyncio
async def test_trial_balance_print_shows_period(ui_client):
    r = await _get(ui_client, "/accounting/print/trial-balance?date_from=2026-01-01&date_to=2026-01-31")
    assert r.status_code == 200
    assert "2026-01-01" in r.text and "2026-01-31" in r.text
    assert "As of:" not in r.text


# ---------------------------------------------------------------------------
# Error states
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
    assert "You need a manager role" in page.text  # the clean banner, not a raw error
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
    "acct.record_foreign_currency", "acct.currency_base_hint", "acct.local_amount",
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
    the browser. Money headers stay right-aligned over their figures in both.
    """
    for url in ("/accounting/print/journal", "/accounting/print/general-ledger",
                "/accounting/print/trial-balance"):
        r = await _get(ui_client, url)
        assert r.status_code == 200, url
        assert "thead th { background: #f5f5f5; font-weight: 700; text-align: center;" in r.text, url
        assert "thead th.cell--number { text-align: right; }" in r.text, url


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
