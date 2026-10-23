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
        ctx, "https://pub.test/connectors/woocommerce/webhook", secret="topsecret",
        topics=["order.created", "order.deleted"])
    assert ids == ["7"] * len(route.calls)
    sent = __import__("json").loads(route.calls[0].request.content)
    assert sent["secret"] == "topsecret"
    assert sent["delivery_url"] == "https://pub.test/connectors/woocommerce/webhook"


@pytest.mark.asyncio
@respx.mock
async def test_register_webhooks_includes_order_deletion():
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    route = respx.post("https://shop.test/wp-json/wc/v3/webhooks").mock(
        return_value=httpx.Response(201, json={"id": 7}))
    woo = WooCommerceConnector()
    await woo.register_webhooks(
        ctx, "https://pub.test/connectors/woocommerce/webhook", secret="topsecret",
        topics=woo.webhook_topics({"orders": "inbound", "products": "off"}))
    topics = [__import__("json").loads(call.request.content)["topic"] for call in route.calls]
    assert "order.deleted" in topics


@pytest.mark.asyncio
async def test_order_deletion_webhook_runs_the_reconciliation_pass():
    """WooCommerce names only the deleted order, so the delivery runs the
    orders sync with its reconciliation pass; other order topics do not."""
    from celerp.connectors.webhooks import WebhookEvent, handle_webhook

    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as run:
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="order.deleted"), ctx)
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="order.updated"), ctx)
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


# ── topics follow the per-record directions ──────────────────────────────────

from connector_test_support import connect, connector_session, seed_company  # noqa: E402,F401

_WOO = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}


def test_topics_follow_directions():
    woo = WooCommerceConnector()
    assert woo.webhook_topics(_WOO) == ["order.created", "order.updated", "order.deleted",
                                        "product.deleted"]
    assert "product.updated" in woo.webhook_topics({**_WOO, "products": "both"})
    assert woo.webhook_topics({**_WOO, "orders": "off", "products": "off"}) == []


@pytest.mark.asyncio
@respx.mock
async def test_direction_change_resyncs_topics(connector_session):
    import json
    from datetime import datetime, timezone

    from celerp_connectors.routes import DirectionRequest, set_connector_direction

    session = connector_session
    cid = await seed_company(session, "TopicResync")
    config = await connect(session, cid, "woocommerce", dict(_WOO), webhook_secret="s3",
                           first_sync_at=datetime.now(timezone.utc))
    config.webhook_ids = ["1", "2"]
    await session.flush()
    ctx = ConnectorContext(company_id=str(cid), access_token="k:s", store_handle="https://shop.test")
    respx.get("https://shop.test/wp-json/wc/v3/webhooks").mock(return_value=httpx.Response(200, json=[]))
    removed = respx.delete(url__regex=r"https://shop\.test/wp-json/wc/v3/webhooks/\d+").mock(
        return_value=httpx.Response(200, json={}))
    added = respx.post("https://shop.test/wp-json/wc/v3/webhooks").mock(
        side_effect=[httpx.Response(201, json={"id": 10 + i}) for i in range(10)])

    with patch("celerp.connectors.relay_token.fetch_context", new=AsyncMock(return_value=ctx)), \
         patch("celerp.connectors.woocommerce.webhook_delivery_url", return_value="https://relay.test/wh"):
        out = await set_connector_direction(
            "woocommerce", DirectionRequest(entity="products", direction="both"), str(cid),
            None, session)

    assert out["entity_directions"]["products"] == "both"
    assert sorted(str(c.request.url).rsplit("/", 1)[1].split("?")[0] for c in removed.calls) == ["1", "2"]
    bodies = [json.loads(c.request.content) for c in added.calls]
    assert {b["topic"] for b in bodies} == {"order.created", "order.updated", "order.deleted",
                                            "product.created", "product.updated", "product.deleted"}
    assert {b["status"] for b in bodies} == {"active"} and {b["secret"] for b in bodies} == {"s3"}
    assert config.webhook_ids == [str(10 + i) for i in range(6)]


@pytest.mark.asyncio
async def test_webhooks_registered_paused():
    """Before the first sync the store's webhooks are paused; once it starts they deliver."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from celerp_connectors.routes import resync_webhook_topics

    woo = WooCommerceConnector()
    woo_register = AsyncMock(return_value=["1"])
    config = SimpleNamespace(webhook_ids=[], webhook_secret="s", entity_directions=_WOO, first_sync_at=None)
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    with patch.object(WooCommerceConnector, "register_webhooks", woo_register), \
            patch.object(WooCommerceConnector, "deregister_webhooks", AsyncMock()), \
            patch("celerp.connectors.woocommerce.webhook_delivery_url", return_value="https://relay.test/wh"):
        await resync_webhook_topics(woo, config, ctx)
        config.first_sync_at = datetime.now(timezone.utc)
        await resync_webhook_topics(woo, config, ctx)
    assert [c.kwargs["status"] for c in woo_register.await_args_list] == ["paused", "active"]


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
    ctx = ConnectorContext(company_id=str(cid), access_token="k:s", store_handle="https://shop.test")
    body, signature = _signed_delivery("early", {"id": 42})
    run = AsyncMock()
    with patch("celerp.connectors.relay_token.fetch_context", new=AsyncMock(return_value=ctx)), \
            patch("celerp.connectors.webhooks.run_sync", new=run):
        assert await dispatch_woocommerce_webhook(body, signature, "order.created") is True
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_push_does_not_trigger_inbound_run(connector_session):
    from datetime import datetime, timezone

    from celerp.connectors.webhooks import WebhookEvent, handle_webhook
    from connector_test_support import add_item

    session = connector_session
    cid = await seed_company(session, "OwnEcho")
    await connect(session, cid, "woocommerce", {**_WOO, "products": "both"},
                  first_sync_at=datetime.now(timezone.utc))
    await add_item(session, cid, "item:echo", "ECHO-1", external_links={"woocommerce": {
        "product_id": "7", "sync_enabled": True, "remote_deleted": False,
        "last_pushed_modified": "2026-09-01T10:00:00"}})
    ctx = ConnectorContext(company_id=str(cid), access_token="k:s", store_handle="https://shop.test")
    run = AsyncMock()
    with patch("celerp.connectors.webhooks.run_sync", new=run):
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="product.updated",
                                          payload={"id": 7, "date_modified_gmt": "2026-09-01T10:00:00"}), ctx)
        run.assert_not_awaited()
        # A change made in the store after the push does run.
        await handle_webhook(WebhookEvent(platform="woocommerce", topic="product.updated",
                                          payload={"id": 7, "date_modified_gmt": "2026-09-02T08:00:00"}), ctx)
    run.assert_awaited_once()
