# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard's "Remove demo items" link opens the inventory list filtered to
setup's untouched samples (?filter=demo), and that list offers the existing bulk
Delete. What the filter selects is tested at the API (test_demo_items_filter.py).
"""
from __future__ import annotations

import html
import re
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fasthtml.common import to_xml

from test_helpers import authed_cookies
from ui.components.demo_items import DEMO_ITEMS_FILTER
from ui.routes.inventory import _bulk_toolbar


def test_demo_items_view_offers_bulk_delete():
    """Delete sits in the bulk menu for archived and expired items, and for the demo
    items the dashboard link opens; any other active list does not offer it."""
    demo = to_xml(_bulk_toolbar([], p={"filter": DEMO_ITEMS_FILTER}))
    assert 'value="delete"' in demo
    for other in ({"q": "rice"}, {"q": "name:[DEMO]"}, {"filter": "low_stock"}):
        assert 'value="delete"' not in to_xml(_bulk_toolbar([], p=other)), other


_EMPTY = {"items": [], "total": 0}


@pytest.fixture()
async def ui():
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://testserver") as c:
        yield c


@pytest.mark.asyncio
async def test_inventory_search_box_shows_the_search_from_the_address(ui):
    """A list opened with ?q= (a shared link, a refresh) shows that
    search in its box, so the filter is visible and can be cleared."""
    mocks = {
        "get_item_schema": [], "get_all_category_schemas": {}, "get_company_category_schemas": {},
        "get_column_prefs": {}, "get_valuation": {"item_count": 0, "category_counts": {}},
        "get_company": {}, "get_locations": _EMPTY, "list_items": _EMPTY,
        "list_import_batches": {"batches": []}, "get_units": [], "get_price_lists": [],
    }
    with patch.multiple("ui.api_client", **{k: AsyncMock(return_value=v) for k, v in mocks.items()}):
        r = await ui.get("/inventory", params={"q": "name:rice"}, cookies=authed_cookies(role="owner"))
    assert r.status_code == 200
    assert 'id="search-input"' in r.text
    assert 'value="name:rice"' in r.text


@pytest.mark.asyncio
async def test_bulk_delete_reloads_the_list_the_owner_is_on(ui):
    """After select-all plus Delete on the demo list, the table reloads that same
    list, not the whole catalog, so the emptied list reads as done."""
    with patch("ui.api_client.bulk_delete", new=AsyncMock(return_value={"deleted": 4})):
        r = await ui.post(
            "/api/items/bulk/delete", data={"selected": ["item:a", "item:b"]},
            headers={"HX-Current-URL": "http://testserver/inventory?filter=demo&page=2"},
            cookies=authed_cookies(role="owner"),
        )
    assert r.status_code == 200
    url = urlsplit(html.unescape(re.search(r'hx-get="([^"]+)"', r.text).group(1)))
    assert url.path == "/inventory/content", r.text
    assert parse_qs(url.query)["filter"] == [DEMO_ITEMS_FILTER], r.text
    assert "page" not in parse_qs(url.query), r.text


def test_demo_hint_names_the_delete_option_unambiguously():
    """The hint names the Delete option as each language shows it. Where the selection
    bar's Clear button carries the same word, the hint also names the Action menu, so
    it cannot be read as that button."""
    import json
    from pathlib import Path
    for path in sorted((Path(__file__).resolve().parents[1] / "ui" / "locales").glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        hint = d["shell.demo_hint"]
        assert d["btn.delete"] in hint, path.name
        if d["btn.clear"] == d["btn.delete"]:
            assert d["inv.action"].rstrip(".…") in hint, (path.name, hint)


@pytest.mark.asyncio
async def test_counts_are_asked_for_with_the_same_filters_as_the_rows(ui):
    """The tabs and status cards come from the valuation call; it gets the very filters
    the row list gets, the demo filter included, so the counts describe the listed rows."""
    valuation = AsyncMock(return_value={"item_count": 0, "category_counts": {}})
    rows = AsyncMock(return_value=_EMPTY)
    static = AsyncMock(return_value=([], {}, {}, [], [], {}))
    with patch.multiple("ui.api_client", get_valuation=valuation, list_items=rows,
                        get_company=AsyncMock(return_value={}),
                        list_import_batches=AsyncMock(return_value={"batches": []})), \
         patch("ui.routes.inventory._load_inventory_static_metadata", new=static):
        r = await ui.get("/inventory", params={"filter": DEMO_ITEMS_FILTER, "category": "Grain", "attr.size": "L"},
                         cookies=authed_cookies(role="owner"))
    assert r.status_code == 200
    list_filters = {k: v for k, v in rows.await_args.args[1].items() if k not in ("limit", "offset", "sort", "dir")}
    assert list_filters == {"filter": DEMO_ITEMS_FILTER, "category": "Grain", "attr.size": "L"}
    assert valuation.await_args.args[1] == list_filters


def test_every_demo_set_fits_on_one_page_of_the_list():
    """The hint says to tick the box to select them all, and select-all ticks the rows
    on the page. The demo list holds at most one business type's untouched set
    (switching type replaces the untouched set, and setup seeds one), so every set
    must fit on the list's default page for that sentence to be true."""
    from celerp.services.demo import _GENERIC_ITEMS, _VERTICAL_ITEMS
    from ui.routes.inventory import _DEFAULT_PER_PAGE
    sizes = {"generic": len(_GENERIC_ITEMS), **{k: len(v) for k, v in _VERTICAL_ITEMS.items()}}
    assert max(sizes.values()) <= _DEFAULT_PER_PAGE, sizes
