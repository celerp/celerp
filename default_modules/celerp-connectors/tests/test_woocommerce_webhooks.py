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
from connector_test_support import (  # noqa: F401
    connect,
    connector_session,
    seed_company,
    stand_in_execution,
)


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(_hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# ── registration sets the secret ─────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_create_webhook_sets_secret_and_delivery_url():
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    route = respx.post("https://shop.test/wp-json/wc/v3/webhooks").mock(
        return_value=httpx.Response(201, json={"id": 7, "topic": "order.created"}))
    hook = await WooCommerceConnector().create_webhook(
        ctx, "https://pub.test/connectors/woocommerce/webhook", "order.created", "topsecret")
    assert hook["id"] == 7
    sent = __import__("json").loads(route.calls[0].request.content)
    assert sent["secret"] == "topsecret" and sent["status"] == "active"
    assert sent["delivery_url"] == "https://pub.test/connectors/woocommerce/webhook"


def test_webhook_topics_include_order_deletion():
    topics = WooCommerceConnector().webhook_topics({"orders": "inbound", "products": "off"})
    assert "order.deleted" in topics


@pytest.mark.asyncio
async def test_order_deletion_webhook_runs_the_reconciliation_pass(connector_session, monkeypatch):
    """WooCommerce names only the deleted order, so the delivery runs the
    orders sync with its reconciliation pass; other order topics do not."""
    from celerp.connectors.webhooks import WebhookEvent, handle_webhook

    cid = await seed_company(connector_session, "OrderGone")
    await connect(connector_session, cid, "woocommerce", _WOO, first_sync="completed")
    stand_in_execution(monkeypatch, ConnectorContext(
        company_id=str(cid), access_token="k:s", store_handle="https://shop.test"))
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as run:
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="order.deleted"), cid)
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="order.updated"), cid)
    assert [c.args[2] for c in run.await_args_list] == ["orders", "orders"]
    assert [c.kwargs["reconcile"] for c in run.await_args_list] == [True, False]


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
    with patch("celerp.db.get_session_ctx", return_value=_mock_session_ctx([("co-1", secret)])), \
         patch("celerp.connectors.webhooks.handle_webhook", new=hw):
        handled = await dispatch_woocommerce_webhook(body, _sign(body, secret), "order.created")
    assert handled is True
    hw.assert_awaited_once()
    event, company_id = hw.await_args.args
    assert (event.topic, company_id) == ("order.created", "co-1")
    # The execution's guard requires the secret that signed it.
    assert hw.await_args.kwargs == {"expected_webhook_secret": secret}


@pytest.mark.asyncio
async def test_dispatch_bad_signature_returns_false():
    from celerp.connectors.webhooks import dispatch_woocommerce_webhook
    body = b'{"id": 42}'
    hw = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=_mock_session_ctx([("co-1", "realsecret")])), \
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


# ── topics follow the per-record directions ──────────────────────────────────


_WOO = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}


def test_topics_follow_directions():
    woo = WooCommerceConnector()
    assert woo.webhook_topics(_WOO) == ["order.created", "order.updated", "order.deleted",
                                        "product.deleted"]
    assert "product.updated" in woo.webhook_topics({**_WOO, "products": "both"})
    assert woo.webhook_topics({**_WOO, "orders": "off", "products": "off"}) == []


def _signed_delivery(secret: str, payload: dict) -> tuple[bytes, str]:
    import json

    body = json.dumps(payload).encode()
    return body, _sign(body, secret)


@pytest.mark.asyncio
async def test_delivery_before_first_sync_ignored(connector_session):
    from celerp.connectors.webhooks import dispatch_woocommerce_webhook

    session = connector_session
    cid = await seed_company(session, "EarlyDelivery")
    await connect(session, cid, "woocommerce", _WOO, webhook_secret="early")
    body, signature = _signed_delivery("early", {"id": 42})
    run = AsyncMock()
    with patch("celerp.connectors.webhooks.run_sync", new=run):
        assert await dispatch_woocommerce_webhook(body, signature, "order.created") is True
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_push_does_not_trigger_inbound_run(connector_session, monkeypatch):
    from datetime import datetime, timezone

    from celerp.connectors.webhooks import WebhookEvent, handle_webhook
    from connector_test_support import add_item

    session = connector_session
    cid = await seed_company(session, "OwnEcho")
    await connect(session, cid, "woocommerce", {**_WOO, "products": "both"}, first_sync="completed")
    await add_item(session, cid, "item:echo", "ECHO-1", external_links={"woocommerce": {
        "product_id": "7", "sync_enabled": True, "remote_deleted": False,
        "last_pushed_modified": "2026-09-01T10:00:00"}})
    stand_in_execution(monkeypatch, ConnectorContext(
        company_id=str(cid), access_token="k:s", store_handle="https://shop.test"))
    run = AsyncMock()
    with patch("celerp.connectors.webhooks.run_sync", new=run):
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="product.updated",
                                          payload={"id": 7, "date_modified_gmt": "2026-09-01T10:00:00"}), cid)
        run.assert_not_awaited()
        # A change made in the store after the push does run.
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="product.updated",
                                          payload={"id": 7, "date_modified_gmt": "2026-09-02T08:00:00"}), cid)
    run.assert_awaited_once()
