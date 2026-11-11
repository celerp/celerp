# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refused period lock or fiscal year close keeps the period lock tab.

The refusal shows as a banner above the re-rendered tab, so both forms stay on screen
and the close form still carries the date that was entered: the user corrects it in
place instead of finding the card gone. A failed read of the current lock does not
take the forms with it.
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


def _close_input_value(html: str) -> str | None:
    tag = re.search(r'<input[^>]*name="fiscal_year_end"[^>]*>', html)
    assert tag, html
    value = re.search(r'value="([^"]*)"', tag.group())
    return value.group(1) if value else ""


def _assert_tab_kept(html: str, banner: str, close_value: str) -> None:
    assert 'id="period-lock-content"' in html
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
    elif case == "blank close date":
        r = await _post(ui_client, "/settings/accounting/close-year", {"fiscal_year_end": " "},
                        AsyncMock(return_value=_LOCK), close_fiscal_year=AsyncMock())
        _assert_tab_kept(r.text, t("acct.fiscal_year_end_date_is_required"), "")
    else:
        r = await _post(ui_client, "/settings/accounting/close-year", {"fiscal_year_end": "2026-12-31"},
                        AsyncMock(return_value=_LOCK), close_fiscal_year=refused)
        _assert_tab_kept(r.text, _REFUSED, "2026-12-31")
    assert r.status_code == 200
    # The lock read fills the lock form as the tab's own render does.
    assert 'name="lock_date" value="2025-12-31"' in r.text


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
