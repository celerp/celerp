# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Honest degradation of the inventory content fragment under a read failure.

The list, valuation, and required static metadata back a real table. When any
of them actually fails, the fragment must say so and offer a retry - never
render a blank table that reads as an empty catalog. A session-expiry (401)
still belongs to the caller's auth handler, so it propagates rather than being
swallowed into an error box. Static metadata (units, category labels) is
supplied by the shared snapshot, so the fragment never fetches them itself.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

import ui.routes.inventory as inv
from ui.api_client import APIError
from test_helpers import make_test_token


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _params(**over):
    p = {
        "q": "", "skus": "", "page": 1, "status": "", "category": "",
        "inventory_type": "", "location_id": "", "source": "", "filter": "",
        "on_memo_to": "", "consigned_from": "", "attr_filters": {},
        "sort": "", "dir": "desc", "per_page": 50, "cols": [],
    }
    p.update(over)
    return p


_COMPANY = {"currency": "USD", "settings": {"vertical": "jewelry"}}


async def _content(monkeypatch, *, valuation=None, items=None, p=None):
    """Render _inventory_content with the two dynamic getters stubbed.

    Passing an APIError instance for valuation or items makes that getter raise.
    """
    async def _get_valuation(_token, **_kw):
        if isinstance(valuation, Exception):
            raise valuation
        return valuation or {}

    async def _list_items(_token, _params):
        if isinstance(items, Exception):
            raise items
        return {"items": items or [], "total": len(items or [])}

    monkeypatch.setattr(inv.api, "get_valuation", _get_valuation)
    monkeypatch.setattr(inv.api, "list_items", _list_items)
    # Any internal metadata fetch would be a regression: units/labels are passed in.
    async def _forbidden(*_a, **_k):
        raise AssertionError("static metadata must come from the passed snapshot")
    monkeypatch.setattr(inv.api, "get_units", _forbidden)
    monkeypatch.setattr(inv.api, "get_category_display_names", _forbidden)

    return await inv._inventory_content(
        "tok", p or _params(), [], {}, {}, _COMPANY, [],
        [{"name": "each"}], {"ring": "Rings"},
        lang="en", role="owner",
    )


@pytest.mark.asyncio
async def test_list_read_failure_renders_honest_error_not_blank_table(monkeypatch):
    frag = await _content(monkeypatch, valuation={}, items=APIError(503, "saturated"))
    html = to_xml(frag)
    # An honest, retryable error - never an empty-catalog table.
    assert "flash--error" in html
    assert 'id="inventory-content"' in html
    assert "/inventory/content" in html  # retry targets the content URL
    assert "<table" not in html.lower()


@pytest.mark.asyncio
async def test_valuation_read_failure_renders_honest_error(monkeypatch):
    frag = await _content(monkeypatch, valuation=APIError(504, "timeout"), items=[])
    html = to_xml(frag)
    assert "flash--error" in html
    assert "<table" not in html.lower()


@pytest.mark.asyncio
async def test_session_expiry_propagates_to_caller(monkeypatch):
    # 401 is the auth handler's job; the fragment must not swallow it.
    with pytest.raises(APIError) as exc:
        await _content(monkeypatch, valuation=APIError(401, "expired"), items=[])
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_static_metadata_comes_from_snapshot_not_a_fetch(monkeypatch):
    # The stubs for get_units / get_category_display_names raise if called; a
    # successful render proves the passed snapshot values were used instead.
    frag = await _content(
        monkeypatch, valuation={"category_counts": {}}, items=[],
    )
    html = to_xml(frag)
    assert 'id="inventory-content"' in html


@pytest.mark.asyncio
async def test_inventory_page_does_not_refetch_company_for_shell(ui_client, monkeypatch):
    # Item 4: the page holds the company from the shared metadata snapshot and
    # passes it into base_shell, so the whole /inventory render fetches the
    # company exactly once - not a second time for the shell chrome.
    import ui.api_client as api

    calls = {"company": 0}

    async def _get_company(_token):
        calls["company"] += 1
        return {"currency": "USD", "settings": {"vertical": "jewelry"}}

    async def _empty_dict(_token):
        return {}

    async def _empty_list(_token):
        return []

    async def _locations(_token):
        return {"items": []}

    async def _valuation(_token, **_kw):
        return {}

    async def _list_items(_token, _params):
        return {"items": [], "total": 0}

    api._reset_metadata_cache_for_tests()
    monkeypatch.setattr(api, "get_company", _get_company)
    monkeypatch.setattr(api, "get_item_schema", _empty_list)
    monkeypatch.setattr(api, "get_all_category_schemas", _empty_dict)
    monkeypatch.setattr(api, "get_category_display_names", _empty_dict)
    monkeypatch.setattr(api, "get_column_prefs", _empty_dict)
    monkeypatch.setattr(api, "get_locations", _locations)
    monkeypatch.setattr(api, "get_units", _empty_list)
    monkeypatch.setattr(api, "get_valuation", _valuation)
    monkeypatch.setattr(api, "list_items", _list_items)

    r = await ui_client.get("/inventory", cookies={"celerp_token": make_test_token()})
    assert r.status_code == 200
    # One fetch total: the snapshot's. base_shell reused the passed settings.
    assert calls["company"] == 1
