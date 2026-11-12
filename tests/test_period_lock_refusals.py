# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refused period lock or fiscal year close keeps the period lock tab.

The refusal shows as a banner above the re-rendered tab, so both forms stay on screen
and the refused form still carries the date that was entered: the user corrects it in
place instead of finding the card gone or the entry swapped for the stored lock. A
failed read of the current lock does not take the forms with it.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_REFUSED = "2026-12-31 is not the last day of a fiscal year."


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://test") as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _input_value(html: str, name: str) -> str:
    tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert tag, html
    value = re.search(r'value="([^"]*)"', tag.group())
    return value.group(1) if value else ""


def _close_input_value(html: str) -> str:
    return _input_value(html, "fiscal_year_end")


def _assert_tab_kept(html: str, banner: str, close_value: str) -> None:
    assert html.count('id="period-lock-content"') == 1, html
    assert "error-banner" in html and banner in html
    assert 'hx-post="/settings/accounting/period-lock"' in html
    assert 'hx-post="/settings/accounting/close-year"' in html
    assert html.index("error-banner") < html.index('hx-post="/settings/accounting/period-lock"')
    assert _close_input_value(html) == close_value


_LOCK = {"lock_date": "2025-12-31", "lock_date_set_at": "2026-01-02T00:00:00", "firm_lock": None}


async def _post(ui_client, path, data, lock_read, **api):
    with patch("ui.api_client.get_period_lock", new=lock_read), \
            patch.multiple("ui.api_client", **api):
        return await ui_client.post(path, data=data)


@pytest.mark.parametrize("case", ["lock refusal", "blank close date", "close refusal"])
async def test_close_year_refusal_keeps_the_form_with_the_entered_date(ui_client, case):
    from ui.i18n import t

    refused = AsyncMock(side_effect=APIError(422, _REFUSED))
    if case == "lock refusal":
        r = await _post(ui_client, "/settings/accounting/period-lock", {"lock_date": "2026-01-31"},
                        AsyncMock(return_value=_LOCK), set_period_lock=refused)
        _assert_tab_kept(r.text, _REFUSED, "")
        assert _input_value(r.text, "lock_date") == "2026-01-31"
    elif case == "blank close date":
        r = await _post(ui_client, "/settings/accounting/close-year", {"fiscal_year_end": " "},
                        AsyncMock(return_value=_LOCK), close_fiscal_year=AsyncMock())
        _assert_tab_kept(r.text, t("acct.fiscal_year_end_date_is_required"), "")
    else:
        r = await _post(ui_client, "/settings/accounting/close-year", {"fiscal_year_end": "2026-12-31"},
                        AsyncMock(return_value=_LOCK), close_fiscal_year=refused)
        _assert_tab_kept(r.text, _REFUSED, "2026-12-31")
        # The lock read fills the lock form as the tab's own render does.
        assert _input_value(r.text, "lock_date") == "2025-12-31"
    assert r.status_code == 200


@pytest.mark.parametrize("lock_read", ["read", "failed read"])
async def test_a_refused_lock_save_keeps_the_entered_date_and_the_stored_lock(ui_client, lock_read):
    """The lock form keeps the refused entry; the stored lock still shows as the lock in force,
    with its unlock button, because the refusal changed nothing."""
    from ui.i18n import t

    read = AsyncMock(return_value=_LOCK) if lock_read == "read" else AsyncMock(side_effect=APIError(503, "down"))
    r = await _post(ui_client, "/settings/accounting/period-lock", {"lock_date": " 2026-01-31 "}, read,
                    set_period_lock=AsyncMock(side_effect=APIError(422, _REFUSED)))
    assert r.status_code == 200
    _assert_tab_kept(r.text, _REFUSED, "")
    assert _input_value(r.text, "lock_date") == "2026-01-31"
    stored = t("settings_accounting.currently_locked_through", lock_date="2025-12-31", updated="2026-01-02")
    assert (stored in r.text) == (lock_read == "read")
    assert ('name="unlock"' in r.text) == (lock_read == "read")


async def test_a_refused_unlock_shows_the_lock_still_in_force(ui_client):
    r = await _post(ui_client, "/settings/accounting/period-lock", {"lock_date": "2025-12-31", "unlock": "1"},
                    AsyncMock(return_value=_LOCK), set_period_lock=AsyncMock(side_effect=APIError(422, _REFUSED)))
    assert r.status_code == 200
    _assert_tab_kept(r.text, _REFUSED, "")
    assert _input_value(r.text, "lock_date") == "2025-12-31"


@pytest.mark.parametrize("case", ["blank close date", "close refusal"])
async def test_close_year_refusal_survives_a_failed_lock_read(ui_client, case):
    from ui.i18n import t

    down = AsyncMock(side_effect=APIError(503, "unavailable"))
    if case == "blank close date":
        r = await _post(ui_client, "/settings/accounting/close-year", {"fiscal_year_end": ""},
                        down, close_fiscal_year=AsyncMock())
        banner, value = t("acct.fiscal_year_end_date_is_required"), ""
    else:
        r = await _post(ui_client, "/settings/accounting/close-year", {"fiscal_year_end": "2026-12-31"},
                        down, close_fiscal_year=AsyncMock(side_effect=APIError(422, _REFUSED)))
        banner, value = _REFUSED, "2026-12-31"
    assert r.status_code == 200
    _assert_tab_kept(r.text, banner, value)


@pytest.mark.parametrize("path,data,api", [
    ("/settings/accounting/period-lock", {"lock_date": "2026-01-31"}, {"set_period_lock": AsyncMock(return_value=_LOCK)}),
    ("/settings/accounting/close-year", {"fiscal_year_end": "2025-12-31"},
     {"close_fiscal_year": AsyncMock(return_value={"lock_date": "2025-12-31", "net_income": 70})}),
])
async def test_a_lock_or_close_that_applies_redraws_the_tab_under_one_id(ui_client, path, data, api):
    """The tab htmx swaps on every lock and close response carries its id once, the success
    banner inside it, so the next submit targets the one element."""
    r = await _post(ui_client, path, data, AsyncMock(return_value=_LOCK), **api)
    assert r.status_code == 200, r.text
    assert r.text.count('id="period-lock-content"') == 1, r.text
    assert 'hx-post="/settings/accounting/close-year"' in r.text
