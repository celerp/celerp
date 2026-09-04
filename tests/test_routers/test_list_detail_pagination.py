# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""list_detail must render one bounded page of lines, not the whole array.

A large list rendered every stored line in one pass, so a 2000-line list built
2000 editable rows in the response. The renderer must instead render at most one
page of rows with a pager, and a finalized-row field edit on a later page must
address the page-absolute stored index (offset + in-page position), not the
in-page position alone, so the correct stored line is edited.
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token


_PAGE_LIMIT = 100


@pytest.fixture
def ui_app():
    from ui.app import app as _app
    return _app


def _cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


def _finalized_list(n_lines: int) -> dict:
    """A finalized (non-draft) list with n_lines lines; finalized rows render the
    double-click-to-edit cells whose edit URL carries the stored line index."""
    return {
        "entity_id": "list:big",
        "id": "list:big",
        "list_type": "quotation",
        "doc_type": "list",
        "status": "finalized",
        "created_at": "2026-01-01",
        "version": 3,
        "line_items": [
            {"item_id": f"item:{i}", "entity_id": f"item:{i}",
             "sku": f"SKU{i}", "description": f"Item {i}",
             "quantity": 1, "unit_price": 1.0}
            for i in range(n_lines)
        ],
    }


def _base_stubs(list_payload: dict) -> dict:
    """The api.* calls list_detail makes, stubbed to safe defaults so the render
    reaches (and completes) the line-table block without external calls."""
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
        "ui.api_client.get_items_metadata": AsyncMock(return_value={}),
        "ui.api_client.get_item": AsyncMock(return_value={}),
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


async def _get_list_detail(ui_app, path: str) -> str:
    with _Patches(_base_stubs(_finalized_list(250))):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get(path, cookies=_cookies())
    assert r.status_code == 200, r.text
    return r.text


def _line_edit_indices(html: str) -> list[int]:
    """Every stored line index referenced by a finalized field-edit cell URL."""
    return [int(m) for m in re.findall(r"/(?:docs|lists)/list:big/line/(\d+)/field/", html)]


def _row_count(html: str) -> int:
    """Editable finalized cells are keyed one per (line, field); the distinct line
    indices they reference is the count of rendered line rows."""
    return len(set(_line_edit_indices(html)))


@pytest.mark.asyncio
async def test_list_detail_renders_one_page(ui_app):
    """A 250-line finalized list renders at most one page of line rows and a pager,
    not the full array."""
    html = await _get_list_detail(ui_app, "/lists/list:big")
    rendered = _row_count(html)
    assert 0 < rendered <= _PAGE_LIMIT, (
        f"the render must be bounded to one page (<= {_PAGE_LIMIT} rows), "
        f"got {rendered} distinct line rows")
    assert "line-pager" in html or "list-line-pager" in html, (
        "a large list must render a pager to reach off-page lines")


@pytest.mark.asyncio
async def test_list_detail_field_edit_page_absolute_idx(ui_app):
    """A finalized-row field edit on page 2 addresses the page-absolute offset+i
    stored index, so the correct stored line is edited."""
    html = await _get_list_detail(ui_app, "/lists/list:big?offset=100&limit=100")
    indices = _line_edit_indices(html)
    assert indices, "page 2 must render finalized-row edit cells"
    assert min(indices) >= 100, (
        "page-2 edit cells must address page-absolute stored indices (offset+i); "
        f"got indices starting at {min(indices)}")
    assert max(indices) < 200, f"page 2 must not run past its window; got up to {max(indices)}"
