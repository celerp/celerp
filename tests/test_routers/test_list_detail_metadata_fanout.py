# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""list_detail item-metadata must be bounded: ONE bulk call, never one per line.

A large list rendered the item-metadata map by fanning out one internal item
fetch per line, so a 2000-line list issued ~2000 authenticated requests and
exhausted the API DB pool. The renderer must instead issue a single bulk
metadata call whose count is constant regardless of line count, and when that
bulk call fails it must degrade to the stored line values (empty meta map), never
fabricate metadata and never crash the page.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token


@pytest.fixture
def ui_app():
    from ui.app import app as _app
    return _app


def _cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


def _audit_list(n_lines: int) -> dict:
    """A finalized audit list with *n_lines* item lines (audit consumes item meta)."""
    return {
        "entity_id": "list:big",
        "list_type": "audit",
        "status": "finalized",
        "created_at": "2026-01-01",
        "line_items": [
            {"item_id": f"item:{i}", "entity_id": f"item:{i}",
             "description": f"Item {i}", "quantity": 1}
            for i in range(n_lines)
        ],
    }


def _base_stubs(list_payload: dict) -> dict:
    """The api.* stubs list_detail touches, all safe defaults so the render reaches
    (and completes) the metadata block without external calls."""
    return {
        "ui.api_client.get_list": AsyncMock(return_value=list_payload),
        "ui.api_client.get_company": AsyncMock(return_value={"name": "Co", "settings": {}}),
        "ui.api_client.get_price_lists": AsyncMock(return_value=[]),
        "ui.api_client.get_taxes": AsyncMock(return_value=[]),
        "ui.api_client.list_list_notes": AsyncMock(return_value=[]),
        "ui.api_client.get_locations": AsyncMock(return_value={"items": [], "total": 0}),
        "ui.api_client.get_units": AsyncMock(return_value=[]),
        "ui.api_client.get_chart": AsyncMock(return_value={"items": [], "total": 0}),
        "ui.api_client.get_relay_status": AsyncMock(return_value={}),
        "ui.api_client.get_share_state": AsyncMock(return_value={}),
    }


class _Patches:
    def __init__(self, mocks: dict):
        self._patches = [patch(k, new=v) for k, v in mocks.items()]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._patches:
            p.stop()


def _item_meta(eid: str) -> dict:
    return {"id": eid, "status": "available", "quantity": 1,
            "sell_by": "piece", "weight_unit": "kg"}


@pytest.mark.asyncio
async def test_list_detail_bounded_metadata_calls(ui_app):
    """A 2000-line list issues EXACTLY ONE bulk metadata call and never calls the
    per-item get_item; the call count is constant, not proportional to line count."""
    n = 2000
    get_item_spy = AsyncMock(side_effect=lambda token, eid: _item_meta(eid))
    bulk_spy = AsyncMock(side_effect=lambda token, eids: {e: _item_meta(e) for e in eids})

    stubs = _base_stubs(_audit_list(n))
    stubs["ui.api_client.get_item"] = get_item_spy
    stubs["ui.api_client.get_items_metadata"] = bulk_spy

    with _Patches(stubs):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get("/lists/list:big", cookies=_cookies())

    assert r.status_code == 200, r.text
    assert get_item_spy.call_count == 0, (
        f"per-line fan-out must be gone; get_item called {get_item_spy.call_count} times")
    assert bulk_spy.call_count == 1, (
        f"exactly one bulk metadata call expected, got {bulk_spy.call_count}")


@pytest.mark.asyncio
async def test_list_detail_bulk_failure_degrades_to_stored_values(ui_app):
    """When the bulk metadata call raises, the page still renders (200) from stored
    line values with an empty meta map; no fabricated metadata, no crash."""
    bulk_spy = AsyncMock(side_effect=RuntimeError("bulk metadata unavailable"))
    get_item_spy = AsyncMock(side_effect=lambda token, eid: _item_meta(eid))

    stubs = _base_stubs(_audit_list(5))
    stubs["ui.api_client.get_item"] = get_item_spy
    stubs["ui.api_client.get_items_metadata"] = bulk_spy

    with _Patches(stubs):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get("/lists/list:big", cookies=_cookies())

    assert r.status_code == 200, r.text
    assert bulk_spy.call_count == 1, (
        f"the single bulk call must be attempted, got {bulk_spy.call_count}")
    assert get_item_spy.call_count == 0, (
        "the failed bulk call must not fall back to per-line fan-out")
