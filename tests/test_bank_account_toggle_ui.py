# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Archiving or restoring a bank account from the bank accounts list says why when the
books refuse it, and keeps the list on screen."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.api_client import APIError
from ui.i18n import t

_BANK = {"id": "b1", "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
         "chart_account_code": "1112", "currency": "USD", "balance": 0.0, "is_active": False}
_OTHER = {**_BANK, "id": "b2", "bank_name": "Main Bank", "chart_account_code": "1111", "is_active": True}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


@pytest.mark.asyncio
async def test_a_restore_the_books_refuse_shows_why_and_keeps_the_list(ui_client):
    refused = AsyncMock(side_effect=APIError(422, t("acct.err_account_archived", "en", code="1112")))
    with patch("ui.api_client.get_bank_account", new=AsyncMock(return_value=_BANK)), \
         patch("ui.api_client.patch_bank_account", new=refused), \
         patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": [_BANK, _OTHER]})):
        r = await ui_client.patch("/settings/accounting/bank-accounts/b1/toggle",
                                  cookies={"celerp_token": make_test_token(role="owner")})

    assert r.status_code == 200
    assert "Account 1112 is archived" in r.text
    assert "error-banner" in r.text
    assert "Main Bank" in r.text
    refused.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_archive_the_books_accept_lists_the_active_bank_accounts(ui_client):
    archived = AsyncMock(return_value={**_OTHER, "is_active": False})
    with patch("ui.api_client.get_bank_account", new=AsyncMock(return_value=_OTHER)), \
         patch("ui.api_client.patch_bank_account", new=archived), \
         patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": [_BANK]})):
        r = await ui_client.patch("/settings/accounting/bank-accounts/b2/toggle",
                                  cookies={"celerp_token": make_test_token(role="owner")})

    assert r.status_code == 200
    assert "error-banner" not in r.text
    archived.assert_awaited_once_with(archived.await_args.args[0], "b2", {"is_active": False})
