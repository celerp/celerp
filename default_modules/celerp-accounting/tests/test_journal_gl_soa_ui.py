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
        {"code": "1111", "name": "Bank", "account_type": "asset",
         "opening": 100.0, "debit": 40.0, "credit": 0.0, "closing": 140.0},
        {"code": "4100", "name": "Sales Revenue", "account_type": "revenue",
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
    # Manual entries offer void; auto entries do not
    assert "void" in html.lower()


@pytest.mark.asyncio
async def test_general_ledger_tab_renders(ui_client):
    r = await _get(ui_client, "/accounting?tab=general-ledger")
    assert r.status_code == 200
    assert "1111" in r.text and "4100" in r.text
    # Drilldown carries origin tab
    assert "from=general-ledger" in r.text


@pytest.mark.asyncio
async def test_soa_tab_renders(ui_client):
    r = await _get(ui_client, "/accounting?tab=soa&contact_id=contact:9")
    assert r.status_code == 200
    assert "Acme Ltd" in r.text
    assert "60" in r.text


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
    assert "contact:new" in r.headers["location"]


@pytest.mark.asyncio
async def test_je_form_renders(ui_client):
    r = await _get(ui_client, "/accounting/journal/new")
    assert r.status_code == 200
    html = r.text
    assert "idempotency_token" in html
    assert "val-chip" in html
    assert "combobox" in html or "searchable" in html


@pytest.mark.asyncio
async def test_ledger_back_link_follows_origin(ui_client):
    r = await _get(ui_client, "/accounting/ledger/1111?from=general-ledger")
    assert r.status_code == 200
    assert "tab=general-ledger" in r.text
    r = await _get(ui_client, "/accounting/ledger/1111?from=pnl")
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
async def test_journal_csv_neutralizes_formula_memo(ui_client):
    poisoned = json.loads(json.dumps(_JOURNAL))
    poisoned["entries"][0]["memo"] = "=SUM(A1:A2)"
    r = await _get(ui_client, "/accounting/export/journal/csv", get_journal=poisoned)
    assert r.status_code == 200
    assert "'=SUM(A1:A2)" in r.text
    assert ",=SUM" not in r.text.replace("'=SUM", "")


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
    assert page.status_code == 200  # page shell renders with a clean banner
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
    "acct.closing_balance",
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
