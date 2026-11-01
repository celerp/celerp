# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An item's sales channels in the catalog and on the item: its state in each connected
store (or -- when not linked), the actions that apply, and the confirmation asked before
an existing store product with the same SKU is linked. The channels come from the
connectors module's manifest, registered as the app registers it."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from ui.i18n import t

DEFAULT_MODULES = Path(__file__).resolve().parents[2] / "default_modules"
COMPANY = {"id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner"}
EID = "item:chan-1"
PUBLISHED = {"woocommerce": {"linked": True, "enabled": True, "remote_status": "publish", "anchor_id": EID}}


@pytest.fixture(autouse=True)
def _app_slots():
    from celerp.modules.loader import register_core_folded_slots
    register_core_folded_slots(DEFAULT_MODULES)


def _connected(*platforms: str):
    return patch("ui.routes.inventory.connected_connector_ids", AsyncMock(return_value=set(platforms)))


async def _ui(method: str, path: str, patches: list, **kw):
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": make_test_token(role="owner")})
            return await client.request(method, path, **kw)


def _item(state: dict) -> AsyncMock:
    return AsyncMock(return_value={"id": EID, "sku": "CH-1", "name": "Channel shirt", "_channel_state": state})


@pytest.mark.asyncio
async def test_channels_column_with_real_wiring():
    from ui.components.table import data_table
    from ui.routes.inventory import _catalog_channels, _column_manager, _inventory_table

    with _connected("woocommerce", "shopify"):
        _, channels = await _catalog_channels(COMPANY, "owner")
    assert [c["id"] for c in channels] == ["woocommerce", "shopify"]
    rows = [
        {"id": EID, "sku": "CH-1", "name": "Linked", "_channel_state": PUBLISHED},
        {"id": "item:chan-2", "sku": "CH-2", "name": "Not linked", "_channel_state": {}},
    ]
    schema = [{"key": "sku", "label": "SKU", "type": "text"}, {"key": "name", "label": "Name", "type": "text"}]
    eff_schema, visible, args = _inventory_table(
        {}, rows, schema, {}, {}, COMPANY, [], [], {}, role="owner", catalog_channels=channels)
    assert "_channels" in visible
    html = to_xml(data_table(args.pop("schema"), rows, **args))
    linked = html.split('id="channel-item-chan-1-woocommerce"', 1)[1].split("</div>", 1)[0]
    assert t("inventory.channel_published") in linked
    assert t("inventory.channel_action_unpublish") in linked and t("inventory.channel_action_stop") in linked
    assert 'hx-post="/api/items/item%3Achan-1/channel-sync/woocommerce/unpublish"' in html
    # Not linked: -- and the one action that applies; Shopify cannot create a product.
    unlinked = html.split('id="channel-item-chan-2-woocommerce"', 1)[1].split("</div>", 1)[0]
    assert "--" in unlinked and t("inventory.channel_action_publish") in unlinked
    shopify = html.split('id="channel-item-chan-2-shopify"', 1)[1].split("</div>", 1)[0]
    assert "--" in shopify and "<button" not in shopify
    # The column is listed in Manage Columns, so it can be hidden and shown again.
    assert 'value="_channels"' in to_xml(_column_manager(eff_schema, {}, "", visible))


@pytest.mark.asyncio
async def test_item_detail_channel_actions():
    from ui.routes.inventory import _advanced_panel

    with patch("celerp.modules.registry.uses_module", return_value=True):
        panel = to_xml(_advanced_panel(EID, {"quantity": 1}, None, role="owner", settings={},
                                       connected_connectors={"woocommerce"}))
    assert 'href="/inventory/item%3Achan-1/channel/woocommerce"' in panel
    assert "/channel/shopify" not in panel  # shown only for a connected store

    r = await _ui("GET", f"/inventory/{EID}/channel/woocommerce", [
        patch("ui.api_client.get_item", _item(PUBLISHED)),
        patch("ui.api_client.get_company", AsyncMock(return_value=COMPANY)),
    ])
    assert r.status_code == 200
    assert t("inventory.channel_page_title", name="Channel shirt", service="WooCommerce") in r.text
    assert t("inventory.channel_published") in r.text
    assert f'hx-post="/api/items/item%3Achan-1/channel-sync/woocommerce/unpublish"' in r.text
    assert 'href="/inventory/item%3Achan-1"' in r.text  # back to the item


@pytest.mark.asyncio
async def test_sku_match_inline_confirmation():
    ask = {"entity_id": EID, "sku": "CH-1", "remote_id": "88", "remote_name": "Store shirt",
           "message": t("connectors.link_confirm", name="Store shirt", service="WooCommerce")}
    sync = AsyncMock(return_value={"updated": 0, "errors": [], "needs_confirmation": [ask]})
    base = [patch("ui.api_client.get_company", AsyncMock(return_value=COMPANY)),
            patch("ui.api_client.set_connector_item_sync", sync)]

    r = await _ui("POST", f"/api/items/{EID}/channel-sync/woocommerce/publish",
                  [*base, patch("ui.api_client.get_item", _item({}))])
    assert r.status_code == 200
    assert ask["message"] in r.text
    assert t("inventory.channel_link") in r.text and t("btn.cancel") in r.text
    assert "confirm_remote" in r.text and "88" in r.text  # Link confirms the store product shown
    assert 'hx-get="/api/items/item%3Achan-1/channel/woocommerce"' in r.text  # Cancel changes nothing
    assert sync.await_args.args[1:] == ("woocommerce", [EID], "publish")
    assert sync.await_args.kwargs["confirm_link"] is None

    # Link: the same action, confirmed, and the block shows the linked product.
    sync.return_value = {"updated": 1, "errors": [], "needs_confirmation": []}
    r = await _ui("POST", f"/api/items/{EID}/channel-sync/woocommerce/publish",
                  [*base, patch("ui.api_client.get_item", _item(PUBLISHED))], data={"confirm_remote": "88"})
    assert sync.await_args.kwargs["confirm_link"] == {EID: "88"}
    assert t("inventory.channel_published") in r.text and ask["message"] not in r.text

    # Bulk: the items that need it are listed with one Link all.
    sync.return_value = {"updated": 1, "errors": [], "needs_confirmation": [ask]}
    r = await _ui("POST", "/api/items/bulk/channel-sync/woocommerce/publish", base,
                  data={"selected": [EID, "item:chan-2"]})
    assert ask["message"] in r.text and t("inventory.channel_link_all") in r.text
    assert f'value="{EID}"' in r.text and 'value="item:chan-2"' not in r.text
    assert t("inventory.channel_bulk_done", n=1, service="WooCommerce") in r.text
    assert f'value="{EID}:88"' in r.text  # Link all confirms each store product shown

    # Link all sends each item with the store product it was shown.
    sync.return_value = {"updated": 1, "errors": [], "needs_confirmation": []}
    await _ui("POST", "/api/items/bulk/channel-sync/woocommerce/publish", base,
              data={"selected": [EID], "confirm": [f"{EID}:88"]})
    assert sync.await_args.kwargs["confirm_link"] == {EID: "88"}


@pytest.mark.asyncio
async def test_legacy_shopify_bulk_route_gone(client, auth):
    r = await _ui("POST", "/api/items/bulk/shopify-sync/enable", [], data={"selected": [EID]})
    assert r.status_code in (404, 405)
    r = await client.post("/items/bulk/shopify-sync", headers=auth["headers"], json={"entity_ids": [EID], "enable": True})
    assert r.status_code in (404, 405)
