# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The first sync's preview page: counts and tables for the chosen directions, the
order date kept in the URL, an explicit Start, and a way back."""
from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ui.i18n import t

WOO = {"products": "both", "stock": "outbound", "orders": "inbound", "customers": "off"}
PREVIEW = {
    "directions": WOO, "orders_from": "2026-09-01T00:00:00+00:00",
    "matches": [{"key": "1", "sku": "MATCH-1", "name": "Store shirt", "celerp_name": "Celerp shirt",
                 "modified": "2026-09-02T10:00:00+00:00", "entity_id": "item:m",
                 "differences": {"sale_price": [25.0, 30.0]}}],
    "new": [{"key": "2", "sku": "NEW-1", "name": None, "price": 8.0, "modified": None}],
    "no_sku": [{"key": "3", "sku": None, "name": "Untitled", "modified": None}],
    "problems": [], "to_publish": [{"entity_id": "item:o", "sku": "ONLY-1", "name": "Only", "modified": None}],
    "orders": [{"id": "12", "number": "1012", "status": "pending", "created": None, "total": 5.0,
                "currency": "USD", "posts": False},
               {"id": "10", "number": "1010", "status": "processing", "created": None, "total": 40.0,
                "currency": "USD", "posts": True}],
    "orders_by_status": {"pending": 1, "processing": 1}, "posting_orders": 1,
    "order_effect": "1 orders in Processing or Completed will be issued as invoices and post revenue and stock.",
}


def _patches(preview=None, start=None, first_sync_at=None):
    config = SimpleNamespace(entity_directions=dict(WOO), first_sync_at=first_sync_at)
    return [
        patch("ui.api_client.get_company", AsyncMock(return_value={
            "id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner"})),
        patch("ui.api_client.get_first_sync_preview", preview or AsyncMock(return_value=PREVIEW)),
        patch("ui.api_client.start_first_sync", start or AsyncMock(return_value={"ok": True})),
        patch("ui.routes.settings_connectors._get_connector_config", AsyncMock(return_value=config)),
    ]


async def _client_get(path: str, **patches):
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    with ExitStack() as stack:
        for p in _patches(**patches):
            stack.enter_context(p)
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": make_test_token(role="owner")})
            return await client.get(path)


@pytest.mark.asyncio
async def test_preview_shows_counts_tables_and_start():
    preview = AsyncMock(return_value=PREVIEW)
    r = await _client_get("/settings/connectors/woocommerce/first-sync?show=new&orders_from=2026-09-01",
                          preview=preview)
    assert r.status_code == 200
    assert preview.await_args.args[1:] == ("woocommerce", "2026-09-01")
    html = r.text
    for label in (t("connectors.preview_tab_matches", n=1), t("connectors.preview_tab_new", n=1),
                  t("connectors.preview_tab_no_sku", n=1), t("connectors.preview_tab_publish", n=1),
                  t("connectors.preview_tab_orders", n=2)):
        assert label in html
    # The tab links keep the date; the shown table is the one the URL names.
    assert 'href="/settings/connectors/woocommerce/first-sync?show=orders&amp;orders_from=2026-09-01"' in html
    assert t("connectors.preview_new_created", service="WooCommerce") in html
    assert "NEW-1" in html and "<td>--</td>" in html  # a product with no name shows --
    assert 'th class="cell--number"' in html  # the price header sits over its figures
    assert PREVIEW["order_effect"] in html
    assert t("connectors.start_first_sync") in html
    assert 'href="/settings/connectors/woocommerce"' in html  # cancel goes back, nothing changed


@pytest.mark.asyncio
async def test_preview_after_first_sync_goes_to_the_connector():
    from datetime import datetime, timezone

    r = await _client_get("/settings/connectors/woocommerce/first-sync",
                          first_sync_at=datetime.now(timezone.utc))
    assert r.status_code == 302 and r.headers["location"] == "/settings/connectors/woocommerce"


@pytest.mark.asyncio
async def test_start_runs_the_first_sync_with_the_choices():
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    start = AsyncMock(return_value={"ok": True})
    with ExitStack() as stack:
        for p in _patches(start=start):
            stack.enter_context(p)
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": make_test_token(role="owner")})
            r = await client.post("/settings/connectors/woocommerce/first-sync",
                                  data={"match": "take", "orders_from": "2026-09-01"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/settings/connectors/woocommerce"
    _token, platform, directions, match, orders_from = start.await_args.args
    assert (platform, directions, match, orders_from) == ("woocommerce", WOO, "take", "2026-09-01")


def test_summary_offers_every_way_back():
    from fasthtml.common import to_xml

    from ui.routes.settings_connectors_first_sync import first_sync_summary_card

    html = to_xml(first_sync_summary_card("woocommerce", {
        "first_sync_at": "2026-09-01T00:00:00+00:00", "batch": {"id": "b1", "rows": 3, "status": "active"},
        "links": 2, "links_undone_at": None,
        "invoices": [{"entity_id": "doc:woocommerce:order:10", "number": "INV-1", "total": 40.0, "status": "final"}],
    }, "en"))
    assert 'href="/settings/inventory?tab=import-history"' in html
    assert 'hx-post="/settings/connectors/woocommerce/first-sync/undo-links"' in html
    assert 'href="/docs/doc:woocommerce:order:10"' in html
