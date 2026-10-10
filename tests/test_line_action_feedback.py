# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line action that succeeds says what it did: the answer carries a short toast that the
page shows after it reloads, the same way Receive Goods and Return Goods do."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

_L0 = "11111111-1111-4111-8111-111111111111"
_L1 = "22222222-2222-4222-8222-222222222222"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _cookies():
    return {"celerp_token": make_test_token(role="owner")}


def _toast(r) -> dict:
    assert r.status_code == 204, r.text
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,send,fields,lines,expected", [
    ("/docs/x:1/reserve-lines", "reserve_lines", {"new_status": "reserved"}, [_L0, _L1], "2 lines set as reserved."),
    ("/lists/x:1/reserve-lines", "reserve_lines", {"new_status": "reserved"}, [_L0], "1 line set as reserved."),
    ("/docs/x:1/set-available", "set_lines_available", {}, [_L0], "1 line set as available."),
    ("/lists/x:1/set-available", "set_lines_available", {}, [_L0, _L1], "2 lines set as available."),
    ("/docs/x:1/fulfill-lines", "fulfill_lines", {}, [_L0, _L1], "2 lines set as shipped."),
])
async def test_a_line_action_says_what_it_did(ui_client, path, send, fields, lines, expected):
    with patch(f"ui.api_client.{send}", new=AsyncMock(return_value={})):
        r = await ui_client.post(path, cookies=_cookies(),
                                 data={"line_id": lines, "idempotency_key": "k", **fields})
    toast = _toast(r)
    assert toast["message"] == expected
    assert toast["type"] == "info"


@pytest.mark.asyncio
async def test_undo_receipt_says_it_was_undone(ui_client):
    with patch("ui.api_client.undo_receive_goods", new=AsyncMock(return_value={})):
        r = await ui_client.delete("/docs/x:1/receive", cookies=_cookies())
    assert r.headers["HX-Redirect"] == "/docs/x:1"
    assert _toast(r) == {"message": "Receipt undone.", "type": "info"}
