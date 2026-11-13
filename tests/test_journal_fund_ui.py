# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Change fund on a posted journal that came from outside, from its row in the journal: a
panel under the row with each line's Fund select prefilled with the fund it posted on, "Set
all outside lines to" above them, a required reason and the form's key. The API is mocked;
the change itself is tested in test_change_fund."""
from __future__ import annotations

import os
import re

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from test_ui import _company_stub, _mock_get_company  # noqa: F401  (autouse fixture: the company settings)

pytestmark = pytest.mark.asyncio

_FUNDS = {"items": [{"id": "fund:r", "name": "Building fund", "archived": False}]}
_LINES = [{"account": "1111", "name": "Bank", "debit": 50.0, "credit": 0.0},
          {"account": "4100", "name": "Donations", "debit": 0.0, "credit": 50.0}]


def _entry(je_id: str, *, origin: str | None, status: str = "posted") -> dict:
    return {"je_id": je_id, "ts": "2026-03-01", "memo": "Filed", "status": status, "origin": origin,
            "source_doc": None, "module_source": None, "lines": _LINES}


_JOURNAL = {"entries": [_entry("je:out", origin="file_import"), _entry("je:own", origin=None),
                        _entry("je:gone", origin="file_import", status="void")],
            "total_debit": 100.0, "total_credit": 100.0, "filtered": False}
_FUND_LINES = {"je_id": "je:out", "version": 3, "ts": "2026-03-01", "memo": "Filed", "lines": [
    {"n": 1, "account": "1111", "name": "Bank", "debit": 50.0, "credit": 0.0, "fund": "fund:r"},
    {"n": 2, "account": "4100", "name": "Donations", "debit": 0.0, "credit": 50.0, "fund": "unrestricted"}]}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _company(funds: bool = True) -> dict:
    return {"id": "c1", "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01",
            "settings": {"funds_enabled": funds}}


async def _call(ui_client, method: str, url: str, *, funds: bool = True, change: AsyncMock | None = None,
                lines: AsyncMock | None = None, data: dict | None = None):
    with patch("ui.api_client.get_journal", new=AsyncMock(return_value=_JOURNAL)), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_journal_funds", new=lines or AsyncMock(return_value=_FUND_LINES)), \
         patch("ui.api_client.change_journal_fund", new=change or AsyncMock()), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(_company(funds)))):
        return await (ui_client.get(url) if method == "GET" else ui_client.post(url, data=data))


def _picked(html: str, name: str) -> str | None:
    """The value the fund picker named ``name`` holds, or None when there is none."""
    found = re.search(rf'<input(?=[^>]*type="hidden")[^>]*name="{name}"[^>]*>', html)
    return re.search(r'value="([^"]*)"', found.group(0)).group(1) if found else None


async def test_posted_outside_journal_row_offers_change_fund(ui_client):
    """Only a posted journal from outside offers Change fund, to a user who manages accounting
    in a company with funds in use."""
    r = await _call(ui_client, "GET", "/accounting")
    assert r.status_code == 200, r.text
    buttons = re.findall(r'<button[^>]*hx-get="(/accounting/journal/fund-change[^"]*)"[^>]*>', r.text)
    assert len(buttons) == 1 and "je=je%3Aout" in buttons[0], buttons
    assert "Change fund" in r.text

    r = await _call(ui_client, "GET", "/accounting", funds=False)
    assert "/accounting/journal/fund-change" not in r.text
    ui_client.cookies.update({"celerp_token": make_test_token(role="viewer")})
    r = await _call(ui_client, "GET", "/accounting")
    assert "/accounting/journal/fund-change" not in r.text


async def test_change_fund_panel_saves_the_changed_lines_with_a_reason(ui_client):
    """The panel opens under the row prefilled with the posted funds; Save sends only what was
    changed (a line, or Set all), the reason and the key, and answers with the journal drawn
    again; a refusal keeps the choices, the reason and the key beside Try again."""
    from ui.api_client import APIError

    r = await _call(ui_client, "GET", "/accounting/journal/fund-change?je=je%3Aout&date_from=2026-01-01")
    assert r.status_code == 200, r.text
    assert r.text.lstrip().startswith("<tr")
    form = re.search(r"<form.*?</form>", r.text, re.S).group(0)
    assert re.search(r'hx-post="/accounting/journal/fund-change\?[^"]*date_from=2026-01-01', form)
    assert _picked(form, "line_1") == "fund:r" and _picked(form, "line_2") == "unrestricted"
    assert _picked(form, "all") == "" and "Set all outside lines to" in form
    assert re.search(r'<input[^>]*name="reason"[^>]*required', form)
    assert re.search(r'<input[^>]*type="hidden"[^>]*name="idempotency_key"', form)
    assert re.search(r'<input[^>]*name="expected_version"[^>]*value="3"', form) and "Cancel" in form

    change = AsyncMock(return_value={"copies": {"je:out": "je:out:fund:1"}})
    r = await _call(ui_client, "POST", "/accounting/journal/fund-change?date_from=2026-01-01", change=change, data={
        "je": "je:out", "expected_version": "3", "reason": "Wrong grant", "idempotency_key": "k1",
        "all": "", "line_1": "unrestricted", "line_2": "unrestricted"})
    assert r.status_code == 200, r.text
    assert change.await_args.args[1:] == ("je:out", {"expected_version": 3, "request_key": "k1",
                                                     "reason": "Wrong grant", "lines": {"1": "unrestricted"}})
    assert r.headers["HX-Retarget"] == "#journal-table" and 'id="journal-table"' in r.text
    assert "Fund changed" in r.headers["HX-Trigger"]

    change = AsyncMock(return_value={"copies": {}})
    await _call(ui_client, "POST", "/accounting/journal/fund-change", change=change, data={
        "je": "je:out", "expected_version": "3", "reason": "Grant", "idempotency_key": "k2",
        "all": "fund:r", "line_1": "fund:r", "line_2": "unrestricted"})
    assert change.await_args.args[2] == {"expected_version": 3, "request_key": "k2", "reason": "Grant",
                                         "all": "fund:r", "lines": {}}

    refused = AsyncMock(side_effect=APIError(422, "This period is locked."))
    r = await _call(ui_client, "POST", "/accounting/journal/fund-change", change=refused, data={
        "je": "je:out", "expected_version": "3", "reason": "Wrong grant", "idempotency_key": "k3",
        "all": "", "line_1": "unrestricted", "line_2": "unrestricted"})
    assert "HX-Retarget" not in r.headers and r.text.lstrip().startswith("<tr")
    assert "This period is locked." in r.text and "Try again" in r.text
    assert 'value="Wrong grant"' in r.text and 'value="k3"' in r.text and _picked(r.text, "line_1") == "unrestricted"
