# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.connectors.base import ConnectorContext
from celerp.connectors.webhooks import dispatch_woocommerce_webhook
from celerp.gateway.client import GatewayClient


@pytest.mark.asyncio
async def test_shopify_webhook_carries_store_snapshot_into_handler():
    gw = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://x")

    session = MagicMock()
    rows = MagicMock()
    rows.all.return_value = [("co-1",)]
    session.execute = AsyncMock(return_value=rows)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    ctx = ConnectorContext(
        company_id="co-1",
        access_token="token",
        store_handle="shop.example",
    )
    handler = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), patch(
        "celerp.connectors.relay_token.fetch_context",
        new=AsyncMock(return_value=ctx),
    ), patch(
        "celerp.connectors.webhooks.handle_webhook",
        new=handler,
    ):
        await gw._handle_shopify_webhook(
            {
                "topic": "orders/create",
                "shop": "shop.example",
                "data": {"id": 1},
            }
        )

    handler.assert_awaited_once()
    assert handler.await_args.kwargs["expected_store_handle"] == "shop.example"


@pytest.mark.asyncio
async def test_woocommerce_webhook_revalidates_current_secret_under_fence():
    snapshot_session = MagicMock()
    snapshot_rows = MagicMock()
    snapshot_rows.all.return_value = [("company-test", "old-secret")]
    snapshot_session.execute = AsyncMock(return_value=snapshot_rows)

    guard_session = AsyncMock()
    guard_session.commit = AsyncMock()
    guard_session.rollback = AsyncMock()

    @asynccontextmanager
    async def snapshot_cm():
        yield snapshot_session

    @asynccontextmanager
    async def guard_cm():
        yield guard_session

    contexts = iter([snapshot_cm(), guard_cm()])
    connector = MagicMock()
    connector.validate_webhook.side_effect = [True, False]
    config = SimpleNamespace(
        id=7,
        direction="both",
        webhook_secret="new-secret",
    )
    fetch = AsyncMock()

    with patch(
        "celerp.connectors.webhooks.connector_registry.get",
        return_value=connector,
    ), patch(
        "celerp.db.get_session_ctx",
        side_effect=lambda: next(contexts),
    ), patch(
        "celerp.connectors.ownership.lock_connector_operation",
        new=AsyncMock(return_value=config),
    ), patch(
        "celerp.connectors.relay_token.fetch_context",
        new=fetch,
    ):
        handled = await dispatch_woocommerce_webhook(
            b'{"id": 10}',
            "signature",
            "product.updated",
        )

    assert handled is False
    fetch.assert_not_awaited()
    guard_session.rollback.assert_awaited_once()
