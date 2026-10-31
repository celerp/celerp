# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A webhook runs only for the connection that was verified: the Shopify delivery's
shop must match the store the execution reaches, and a WooCommerce delivery's
signing secret must still be the connection's secret inside the execution."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp import connectors
from celerp.connectors.base import ConnectorContext
from celerp.connectors.webhooks import WebhookEvent, handle_webhook
from celerp.gateway.client import GatewayClient
from connector_test_support import connect, connector_session, seed_company, stand_in_execution  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_shopify_webhook_hands_its_shop_to_the_handler():
    gw = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://x")

    session = MagicMock()
    rows = MagicMock()
    rows.all.return_value = [("co-1",)]
    session.execute = AsyncMock(return_value=rows)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    handler = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
            patch("celerp.connectors.webhooks.handle_webhook", new=handler):
        await gw._handle_shopify_webhook({"topic": "orders/create", "shop": "shop.example", "data": {"id": 1}})

    handler.assert_awaited_once()
    event, company_id = handler.await_args.args
    assert (event.topic, company_id) == ("orders/create", "co-1")
    store_matches = handler.await_args.kwargs["store_matches"]
    assert store_matches("shop.example") and not store_matches("other.example")


@pytest.mark.parametrize("store, runs", [("shop.example", True), ("other.example", False)])
async def test_shopify_webhook_runs_only_for_the_matching_store(connector_session, monkeypatch, store, runs):
    cid = await seed_company(connector_session, "ShopHook")
    await connect(connector_session, cid, "shopify", connectors.get("shopify").default_directions,
                  first_sync="completed")
    stand_in_execution(monkeypatch, ConnectorContext(company_id=str(cid), access_token="t", store_handle=store))
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as run:
        await handle_webhook(WebhookEvent(platform="shopify", topic="orders/create"), cid,
                             store_matches=lambda handle: handle == "shop.example")
    assert run.await_count == (1 if runs else 0)


@pytest.mark.parametrize("verified, runs", [("current-secret", True), ("old-secret", False)])
async def test_woocommerce_webhook_rechecks_the_secret_inside_the_execution(
        connector_session, monkeypatch, verified, runs):
    cid = await seed_company(connector_session, "WooHook")
    await connect(connector_session, cid, "woocommerce", {"orders": "inbound", "products": "off"},
                  first_sync="completed", webhook_secret="current-secret")
    stand_in_execution(monkeypatch, ConnectorContext(
        company_id=str(cid), access_token="k:s", store_handle="https://shop.test"))
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as run:
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="order.updated"), cid,
                             expected_webhook_secret=verified)
    assert run.await_count == (1 if runs else 0)
