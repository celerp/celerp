# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""WooCommerce real-time webhook wiring: registration sets a signing secret, and
the receiver verifies the HMAC before triggering a targeted sync."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import base64
import hashlib
import hmac as _hmac
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from celerp.connectors.base import ConnectorContext
from celerp.connectors.woocommerce import WooCommerceConnector


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(_hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# ── registration sets the secret ─────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_register_webhooks_sets_secret_and_returns_ids():
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    route = respx.post("https://shop.test/wp-json/wc/v3/webhooks").mock(
        return_value=httpx.Response(201, json={"id": 7}))
    ids = await WooCommerceConnector().register_webhooks(
        ctx, "https://pub.test/connectors/woocommerce/webhook", secret="topsecret")
    assert ids == ["7"] * len(route.calls)
    sent = __import__("json").loads(route.calls[0].request.content)
    assert sent["secret"] == "topsecret"
    assert sent["delivery_url"] == "https://pub.test/connectors/woocommerce/webhook"


# ── receiver dispatch ────────────────────────────────────────────────────────

def _mock_session_ctx(configs):
    sess = MagicMock()
    res = MagicMock()
    res.all = MagicMock(return_value=configs)
    sess.execute = AsyncMock(return_value=res)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_dispatch_valid_signature_triggers_handle():
    from celerp.connectors.webhooks import dispatch_woocommerce_webhook
    body = b'{"id": 42}'
    secret = "shhh"
    hw = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=_mock_session_ctx([("co-1", secret, "both")])), \
         patch("celerp.connectors.relay_token.fetch_context", new=AsyncMock(return_value=MagicMock())), \
         patch("celerp.connectors.webhooks.handle_webhook", new=hw):
        handled = await dispatch_woocommerce_webhook(body, _sign(body, secret), "order.created")
    assert handled is True
    hw.assert_awaited_once()
    assert hw.await_args[0][0].topic == "order.created"


@pytest.mark.asyncio
async def test_dispatch_bad_signature_returns_false():
    from celerp.connectors.webhooks import dispatch_woocommerce_webhook
    body = b'{"id": 42}'
    hw = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=_mock_session_ctx([("co-1", "realsecret", "both")])), \
         patch("celerp.connectors.relay_token.fetch_context", new=AsyncMock(return_value=MagicMock())), \
         patch("celerp.connectors.webhooks.handle_webhook", new=hw):
        handled = await dispatch_woocommerce_webhook(body, _sign(body, "wrongsecret"), "order.created")
    assert handled is False
    hw.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_no_configs_returns_false():
    from celerp.connectors.webhooks import dispatch_woocommerce_webhook
    with patch("celerp.db.get_session_ctx", return_value=_mock_session_ctx([])):
        handled = await dispatch_woocommerce_webhook(b"{}", "sig", "order.created")
    assert handled is False


@pytest.mark.asyncio
async def test_dispatch_empty_signature_returns_false():
    from celerp.connectors.webhooks import dispatch_woocommerce_webhook
    handled = await dispatch_woocommerce_webhook(b"{}", "", "order.created")
    assert handled is False


# ── gateway handler (relay forwards the delivery to the instance) ─────────────

@pytest.mark.asyncio
async def test_gateway_handler_verifies_and_dispatches():
    from celerp.gateway.client import GatewayClient
    gw = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://x")
    disp = AsyncMock(return_value=True)
    body_b64 = base64.b64encode(b'{"id": 1}').decode("ascii")
    with patch("celerp.connectors.webhooks.dispatch_woocommerce_webhook", new=disp):
        await gw._handle_woocommerce_webhook(
            {"topic": "order.created", "signature": "sig", "body_b64": body_b64})
    disp.assert_awaited_once()
    raw, sig, topic = disp.await_args[0]
    assert raw == b'{"id": 1}'   # exact bytes recovered from base64 for HMAC
    assert sig == "sig"
    assert topic == "order.created"
