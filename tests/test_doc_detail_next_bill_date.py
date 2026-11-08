# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An agreement opened from Documents shows the next bill date its periods hold. Without the
subscriptions module (the periods route answers 404) it shows the date the agreement stores,
and "--" when it stores none."""
from __future__ import annotations

import os
import re

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from test_ui import _mock_get_company  # noqa: F401  (autouse fixture: the company settings)
from ui.api_client import APIError


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _agreement(**extra) -> dict:
    return {"entity_id": "doc:sub-1", "doc_type": "subscription_invoice", "status": "draft",
            "currency": "USD", "frequency": "monthly", "line_items": [], **extra}


async def _next_bill(ui_client, doc: dict, periods: AsyncMock) -> str:
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.get_subscription_periods", new=periods):
        ui_client.cookies.update({"celerp_token": make_test_token()})
        r = await ui_client.get(f"/docs/{doc['entity_id']}")
    assert r.status_code == 200, r.text
    cell = re.search(r'id="sub-next-bill"[^>]*>(.*?</div>.*?)</div>', r.text, re.S).group(1)
    return re.sub(r"<[^>]+>", " ", cell).split()[-1]


@pytest.mark.asyncio
async def test_next_bill_date_is_the_one_its_periods_hold(ui_client):
    periods = AsyncMock(return_value={"next_bill_date": "2027-03-01"})
    assert await _next_bill(ui_client, _agreement(next_run_date="2026-01-01"), periods) == "2027-03-01"


@pytest.mark.asyncio
async def test_without_the_module_the_stored_date_shows(ui_client):
    periods = AsyncMock(side_effect=APIError(404, "Not Found"))
    assert await _next_bill(ui_client, _agreement(next_run_date="2026-05-01"), periods) == "2026-05-01"


@pytest.mark.asyncio
async def test_without_a_date_the_cell_reads_dashes(ui_client):
    periods = AsyncMock(side_effect=APIError(404, "Not Found"))
    assert await _next_bill(ui_client, _agreement(), periods) == "--"
