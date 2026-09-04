# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""list_detail item-metadata must be bounded: enriched server-side, never fetched per line.

A large list rendered the item-metadata map by fanning out one internal item
fetch per line, so a 2000-line list issued ~2000 authenticated requests and
exhausted the API DB pool. The page endpoint now enriches the visible slice and
returns it as item_meta in the same body, so the renderer issues ZERO UI-side
metadata calls regardless of line count; when the body carries no item_meta it
degrades to the stored line values, never fabricating metadata and never crashing.
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


def _page_side_effect(list_payload: dict, include_meta: bool = True):
    """Serve one bounded page from the stored array, mirroring the /page endpoint:
    the list header (stored state without line_items), the requested slice, and (as the
    real server now does) an item_meta map for the slice unless include_meta is False."""
    lines = list_payload.get("line_items", [])
    header = {k: v for k, v in list_payload.items() if k != "line_items"}

    async def _page(token, entity_id, offset: int = 0, limit: int = 100):
        off = max(0, int(offset))
        lim = max(1, min(int(limit), 100))
        page_items = lines[off:off + lim]
        body = {
            "list": header,
            "items": page_items,
            "total": len(lines),
            "version": list_payload.get("version", 1),
        }
        if include_meta:
            body["item_meta"] = {li["item_id"]: _item_meta(li["item_id"])
                                 for li in page_items if li.get("item_id")}
        return body

    return _page


def _base_stubs(list_payload: dict, include_meta: bool = True) -> dict:
    """The api.* stubs list_detail touches, all safe defaults so the render reaches
    (and completes) the metadata block without external calls."""
    return {
        "ui.api_client.get_list_page": AsyncMock(
            side_effect=_page_side_effect(list_payload, include_meta)),
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
    """A 2000-line list issues ZERO UI-side metadata calls: the enrichment arrives in the
    page body's item_meta, so neither the per-item get_item nor a bulk get_items_metadata
    runs, whatever the line count."""
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
    assert bulk_spy.call_count == 0, (
        f"UI-side metadata fetching must be gone (the server enriches the page body); "
        f"get_items_metadata called {bulk_spy.call_count} times")


@pytest.mark.asyncio
async def test_list_detail_missing_item_meta_degrades_to_stored_values(ui_app):
    """When the page body carries no item_meta (server enrichment unavailable), the page
    still renders (200) from the stored line values; no fabricated metadata, no per-line
    or bulk fallback fetch, no crash."""
    bulk_spy = AsyncMock(side_effect=lambda token, eids: {e: _item_meta(e) for e in eids})
    get_item_spy = AsyncMock(side_effect=lambda token, eid: _item_meta(eid))

    stubs = _base_stubs(_audit_list(5), include_meta=False)
    stubs["ui.api_client.get_item"] = get_item_spy
    stubs["ui.api_client.get_items_metadata"] = bulk_spy

    with _Patches(stubs):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get("/lists/list:big", cookies=_cookies())

    assert r.status_code == 200, r.text
    assert "Item 0" in r.text, "stored line description must still render when item_meta is absent"
    assert get_item_spy.call_count == 0 and bulk_spy.call_count == 0, (
        "an absent item_meta must degrade to stored values, never fall back to a UI-side fetch")
