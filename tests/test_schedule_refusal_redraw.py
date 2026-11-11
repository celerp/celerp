# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refused schedule change leaves no view showing what was not saved: with the refusal, the
answer redraws every element that shows the schedule from what the agreement stores (the days
box hides again when Custom was refused). When that redraw cannot be read, the refusal is shown
alone, never a view made up without the stored state."""
from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from test_helpers import authed_cookies
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_SUB = {"entity_id": "doc:sub-1", "doc_type": "subscription_invoice", "status": "active", "frequency": "monthly",
        "currency": "USD", "line_items": [], "version": 2}
_PERIODS = {"rows": []}
_REFUSAL = "This agreement has billed periods, so its frequency cannot change."
# A value each schedule field reads as its type, so every PATCH reaches the API.
_VALUES = {"frequency": "custom", "anchor_date": "2026-12-01", "custom_interval_days": "10", "billing_timing": "arrears",
           "term_start": "2026-12-01", "term_end": "2027-12-01", "cancel_at_period_end": "true",
           "generation_mode": "auto_invoice", "auto_send": "true", "catchup_periods": "3"}


async def _patch(field: str, value: str, sub=None):
    from ui.app import app as ui_app
    get_sub = sub or AsyncMock(return_value=_SUB)
    with patch("ui.api_client.update_subscription_schedule", new=AsyncMock(side_effect=APIError(409, _REFUSAL))), \
         patch("ui.api_client.get_subscription", new=get_sub), \
         patch("ui.api_client.get_subscription_periods", new=AsyncMock(return_value=_PERIODS)):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            ui.cookies.update(authed_cookies("owner"))
            return await ui.patch(f"/subscriptions/doc:sub-1/schedule/{field}", data={"value": value},
                                  headers={"HX-Request": "true"})


def _toast(r) -> str:
    return json.dumps(json.loads(r.headers.get("HX-Trigger") or "{}"))


def _oob_ids(body: str) -> set[str]:
    return {m.group(1) for tag in re.findall(r"<[^>]+>", body) if 'hx-swap-oob="true"' in tag
            for m in [re.search(r'\bid="([^"]+)"', tag)] if m}


async def test_schedule_refusal_redraws_frequency_views():
    r = await _patch("frequency", "custom")
    assert _REFUSAL in _toast(r)
    days = re.search(r'<label[^>]*id="sub-custom-days"[^>]*>', r.text)
    assert days and 'hx-swap-oob="true"' in days.group(0) and "display:none" in days.group(0), r.text
    # The redraw read fails: the refusal alone, nothing drawn without the stored state.
    r = await _patch("frequency", "custom", sub=AsyncMock(side_effect=APIError(500, "The agreement could not be read.")))
    assert _REFUSAL in _toast(r)
    assert _oob_ids(r.text) == set()


async def test_every_schedule_refusal_redraws_the_stored_views():
    from ui.routes.subscriptions import _SCHEDULE_EDITS, _schedule_views
    assert set(_VALUES) == set(_SCHEDULE_EDITS)
    stored = _oob_ids(to_xml(tuple(v(hx_swap_oob="true") for v in _schedule_views("doc:sub-1", _SUB, _PERIODS) if v != "")))
    assert "sub-custom-days" in stored or "sub-schedule" in stored
    for field, value in _VALUES.items():
        r = await _patch(field, value)
        assert (field, _REFUSAL in _toast(r)) == (field, True), (dict(r.headers), r.text[:300])
        assert (field, _oob_ids(r.text) >= stored) == (field, True), r.text
