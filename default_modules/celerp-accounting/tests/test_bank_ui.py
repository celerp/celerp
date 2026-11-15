# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The bank page, the bank list and the client's Questions page as rendered HTML: the
drop zone a bank with no statement opens on, the line row, and what Escape does on
every bank input. API answers are mocked; the API is covered in test_bank_statements.py."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

pytestmark = pytest.mark.asyncio

_BANK = {"id": "b1", "bank_name": "Main Bank", "account_number": "****1234", "currency": "USD", "bank_type": "checking",
         "chart_account_code": "1111", "balance": 0.0, "opening_balance": 0.0, "is_active": True}
_LINE = {"id": "l1", "statement_id": "s1", "line_date": "2026-01-05", "amount": -200.0, "description": "WITHDRAWAL",
         "reference": None, "raw_balance": 800.0, "counterparty_account": "4471", "status": "unmatched",
         "matched_je_ids": [], "client_query": None, "attachments": [],
         "suggestion": {"kind": "post", "account_code": "5100", "source": "history", "reason": "Posted to 5100 twice",
                        "contact_id": None}}
_TABLE = {"groups": [{"key": "SPARSE:WITHDRAWAL:out", "direction": "out", "count": 1, "total": -200.0, "ticked": False,
                      "suggestion": _LINE["suggestion"], "lines": [_LINE]}],
          "summary": {"lines": 1, "open": 1, "posted": 0}, "statements": [
              {"id": "s1", "period_start": "2026-01-01", "statement_date": "2026-01-31", "opening_balance": 1000.0,
               "closing_balance": 800.0, "status": "in_progress"}],
          "suspense_count": 0, "difference": {"amount": 0.0, "notes": []}}
_EMPTY = {"groups": [], "summary": {"lines": 0, "open": 0, "posted": 0}, "statements": [], "suspense_count": 0,
          "difference": {"amount": 0.0, "notes": []}}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _api(table):
    return [patch("ui.api_client.get_bank_account", AsyncMock(return_value=_BANK)),
            patch("ui.api_client.get_bank_accounts", AsyncMock(return_value={"items": [_BANK]})),
            patch("ui.api_client.bank_groups", AsyncMock(return_value=table)),
            patch("ui.api_client.get_chart", AsyncMock(return_value={"items": [
                {"code": "5100", "name": "Office expenses", "account_type": "expense", "is_active": True}]})),
            patch("ui.routes.reports._get_company", AsyncMock(return_value={
                "id": "c1", "name": "TestCo", "currency": "USD", "settings": {"currency": "USD"}}))]


async def _get(ui_client, url, table, role="owner"):
    patches = _api(table)
    for p in patches:
        p.start()
    try:
        ui_client.cookies.set("celerp_token", make_test_token(role=role))
        return await ui_client.get(url)
    finally:
        for p in patches:
            p.stop()


async def test_bank_page_empty_state_is_drop_zone(ui_client):
    r = await _get(ui_client, "/accounting/reconcile/bank/b1", _EMPTY)
    assert r.status_code == 200, r.text
    html = r.text
    assert 'type="file"' in html and "Start without a file" in html
    assert 'action="/accounting/reconcile/bank/b1/drop"' in html or 'hx-post="/accounting/reconcile/bank/b1/drop"' in html
    r = await _get(ui_client, "/accounting/reconcile/start", _EMPTY)
    assert r.status_code == 200 and "Main Bank" in r.text


async def test_bank_line_row_shows_the_line_in_full(ui_client):
    r = await _get(ui_client, "/accounting/reconcile/bank/b1?view=questions", _TABLE)
    html = r.text
    for text in ("2026-01-05", "WITHDRAWAL", "-200.00", "4471", "800.00", "Posted to 5100 twice"):
        assert text in html, text
    assert 'name="q"' in html and "view=questions" in html


def _tag(html: str, pattern: str) -> str:
    m = re.search(pattern, html, re.S)
    assert m, pattern
    return m.group(0)


async def test_bank_inputs_leave_on_escape(ui_client):
    """G13: each input of the bank page behaves on Escape as the bank UI table says and
    writes nothing."""
    html = (await _get(ui_client, "/accounting/reconcile/bank/b1", _TABLE)).text
    # Ask the client note panel and the split form close on Escape without sending.
    for panel in (r"<[^>]+data-panel=\"ask\"[^>]*>", r"<form[^>]+data-form=\"split\"[^>]*>"):
        tag = _tag(html, panel)
        assert "celerpRemoveClosest" in tag and "event" in tag, panel
    # The search box and plain selects blur (the page loads the shared Escape script).
    assert "ESC_BLURS" in html or "blur()" in html
    # The Answer and contact pickers are searchable selects, which restore their choice on Escape.
    assert html.count("searchable-select") >= 2
    # The bulk toolbar keeps the selection on Escape.
    assert "bulk-toolbar" in html
    # Nothing on the bank page posts on a key press: every hx-trigger is a click, submit or
    # change (the shell's global search above it reads as you type and writes nothing).
    for trig in re.findall(r'hx-trigger="([^"]+)"', html[html.index('id="bank-body"'):]):
        assert "keyup" not in trig and "keydown" not in trig, trig
    page = (await _get(ui_client, "/accounting/reconcile/bank/b1?preview=j1", _TABLE)).text
    assert 'hx-get="/accounting/reconcile/bank/b1/preview/j1"' in page
    found = {"job_id": "j1", "bank_id": "b1", "receipts": [], "failed_steps": [], "statements": [{
        "file_id": "f1", "header": {"opening_balance": "1000.00", "closing_balance": "800.00",
                                    "period_start": "2026-01-01", "period_end": "2026-01-31", "currency": "USD"},
        "lines": [_LINE], "tie": {"ok": True}, "blocking": [], "notices": []}]}
    with patch("ui.api_client.statement_preview", AsyncMock(return_value=found)):
        preview = (await _get(ui_client, "/accounting/reconcile/bank/b1/preview/j1", _TABLE)).text
    cells = re.findall(r"<[^>]+class=\"[^\"]*editable-cell[^\"]*\"[^>]*>", preview)
    assert len(cells) == 4
    for cell in cells:
        assert "celerpCellKey" in cell, cell


async def test_an_import_refusal_shows_on_the_preview_and_keeps_what_was_entered(ui_client):
    """A refused import re-renders the preview with the reason above it and the header
    figures the user typed, so nothing has to be entered again."""
    from ui.api_client import APIError
    edited = {"opening_balance": "1000.00", "closing_balance": "1234.00", "period_start": "2026-01-01",
              "period_end": "2026-01-31"}
    found = {"job_id": "j1", "bank_id": "b1", "receipts": [], "failed_steps": [], "statements": [{
        "file_id": "f1", "header": {**edited, "currency": "USD"}, "lines": [_LINE], "tie": {"ok": False},
        "blocking": [], "notices": []}]}
    preview = AsyncMock(return_value=found)
    patches = [patch("ui.api_client.import_statement", AsyncMock(side_effect=APIError(
                   409, "A statement already covers 2026-01-01 to 2026-01-31 on this bank."))),
               patch("ui.api_client.statement_preview", preview)]
    for p in patches:
        p.start()
    try:
        ui_client.cookies.set("celerp_token", make_test_token(role="owner"))
        r = await ui_client.post("/accounting/reconcile/bank/b1/preview/j1/import", data={
            "state": json.dumps({"headers": [edited], "column_map": None}), "idempotency_key": "k1"})
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200, r.text
    assert "A statement already covers 2026-01-01 to 2026-01-31 on this bank." in r.text
    assert preview.await_args.args[2] == [{"header": edited}]
    assert 'value="1234.00"' in r.text and 'data-testid="statement-preview"' in r.text
