# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for API-owned connector credential setup and teardown."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import HTTPException

from celerp_connectors.routes import (
    ApiKeyCredentials,
    ItemSyncRequest,
    revoke_credentials,
    set_item_sync,
    store_credentials,
)

RELAY = "https://relay.test"
STORE = "https://store.test"


def _relay_state():
    return (
        patch("celerp.gateway.state.relay_http_url", return_value=RELAY),
        patch("celerp.gateway.state.relay_session_headers",
              return_value={"X-Session-Token": "s", "X-Instance-ID": "i"}),
    )


def _creds(store_url: str | None = STORE) -> ApiKeyCredentials:
    return ApiKeyCredentials(consumer_key="ck_x", consumer_secret="cs_y", store_url=store_url)


def _session():
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture(autouse=True)
def _owned_connector_boundary():
    with patch(
        "celerp.connectors.ownership.claim_connector_ownership",
        new=AsyncMock(return_value=(object(), True)),
    ), patch(
        "celerp.connectors.ownership.lock_connector_operation",
        new=AsyncMock(return_value=SimpleNamespace(webhook_ids=[], webhook_secret=None)),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership",
        new=AsyncMock(),
    ), patch(
        "celerp.connectors.ownership.connector_owned_by_company",
        new=AsyncMock(return_value=True),
    ), patch(
        "celerp_inventory.services.detach_external_links_for_platform",
        new=AsyncMock(),
    ), patch(
        "celerp.services.outbound_url.asyncio.get_running_loop",
        return_value=SimpleNamespace(
            getaddrinfo=AsyncMock(return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443))
            ])
        ),
    ), patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks",
        new=AsyncMock(return_value=["11"]),
    ):
        yield


# ── store_credentials ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_store_probes_then_stores_on_relay():
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p, respx.mock:
        probe = respx.get(f"{STORE}/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[]))
        respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(404))
        relay = respx.post(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200, json={"stored": True}))
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())

    assert result == {"ok": True}
    assert probe.called
    assert probe.calls.last.request.url.params["_fields"] == "id"
    req = relay.calls.last.request
    assert req.headers["X-Session-Token"] == "s"
    assert b'"consumer_key":"ck_x"' in req.content.replace(b" ", b"")


@pytest.mark.asyncio
async def test_store_relay_402_maps_to_subscription_required():
    url_p, hdr_p = _relay_state()
    release = AsyncMock()
    with patch(
        "celerp.connectors.ownership.release_connector_ownership", release
    ), url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(return_value=httpx.Response(200, json=[]))
        respx.delete(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(404))
        respx.post(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(402))
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())
    assert result["ok"] is False
    assert result["error"] == "subscription_required"
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_requires_disconnect_before_replacing_credentials():
    url_p, hdr_p = _relay_state()
    release = AsyncMock()
    with patch(
        "celerp.connectors.ownership.claim_connector_ownership",
        new=AsyncMock(return_value=(object(), False)),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership", release
    ), url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[]))
        relay = respx.post(f"{RELAY}/tokens/woocommerce")
        result = await store_credentials(
            "woocommerce", _creds(), "company-test", None, _session()
        )

    assert result["error"] == "already_connected"
    assert not relay.called
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_relay_failure_maps_to_relay_error():
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(return_value=httpx.Response(200, json=[]))
        respx.delete(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(404))
        respx.post(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(401))
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())
    assert result["ok"] is False
    assert result["error"] == "relay_error"


@pytest.mark.asyncio
async def test_store_bad_key_rejected_before_relay():
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(return_value=httpx.Response(401))
        relay = respx.post(f"{RELAY}/tokens/woocommerce")
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())
    assert result["error"] == "store_rejected"
    assert not relay.called


@pytest.mark.asyncio
async def test_store_requires_https_store_url(monkeypatch):
    monkeypatch.delenv("CELERP_ALLOW_HTTP_STORE", raising=False)
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p:
        result = await store_credentials("woocommerce", _creds("http://store.test"), "company-test", None, _session())
    assert result["error"] == "store_unreachable"
    assert "supported scheme" in result["detail"]


@pytest.mark.asyncio
async def test_store_requires_store_url():
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p:
        result = await store_credentials("woocommerce", _creds(None), "company-test", None, _session())
    assert result["error"] == "store_unreachable"


@pytest.mark.asyncio
async def test_store_refuses_http_relay(monkeypatch):
    monkeypatch.delenv("CELERP_ALLOW_HTTP_RELAY", raising=False)
    with patch("celerp.gateway.state.relay_http_url", return_value="http://relay.test"):
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())
    assert result["error"] == "relay_not_https"


@pytest.mark.asyncio
async def test_store_unknown_connector_404():
    with pytest.raises(HTTPException) as exc:
        await store_credentials("nope", _creds(), "company-test", None, _session())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_store_webhook_failure_rolls_back_relay_credential():
    url_p, hdr_p = _relay_state()
    release = AsyncMock()
    with patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks",
        new=AsyncMock(side_effect=RuntimeError("webhook failed")),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership", release
    ), url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[]))
        respx.post(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200, json={"stored": True}))
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200))
        result = await store_credentials(
            "woocommerce", _creds(), "company-test", None, _session()
        )

    assert result["ok"] is False
    assert delete.called
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_webhook_failure_keeps_owner_when_revoke_is_ambiguous():
    url_p, hdr_p = _relay_state()
    release = AsyncMock()
    session = _session()
    with patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks",
        new=AsyncMock(side_effect=RuntimeError("webhook failed")),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership", release
    ), url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[]))
        respx.post(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200, json={"stored": True}))
        respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            side_effect=[httpx.Response(404), httpx.Response(500)])
        result = await store_credentials(
            "woocommerce", _creds(), "company-test", None, session
        )

    assert result["ok"] is False
    assert session.commit.await_count == 1
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_item_sync_serializes_with_connector_operations():
    session = _session()
    lock = AsyncMock(return_value=SimpleNamespace(direction="both"))
    anchor = SimpleNamespace(
        entity_id="item:1",
        is_sync_to_shopify=False,
        state={
            "sku": "SKU-1",
            "external_links": {
                "woocommerce": {
                    "product_id": "10",
                    "sync_enabled": True,
                    "remote_deleted": False,
                }
            },
        },
    )
    set_state = AsyncMock()
    with patch(
        "celerp.connectors.ownership.lock_connector_operation", lock
    ), patch(
        "celerp_inventory.services.resolve_catalog_anchor_for_item",
        new=AsyncMock(return_value=anchor),
    ), patch(
        "celerp_inventory.services.set_external_link_state", set_state
    ):
        result = await set_item_sync(
            "woocommerce",
            ItemSyncRequest(entity_ids=["item:1"], enable=False),
            "company-test",
            SimpleNamespace(id="user-1"),
            None,
            session,
        )

    lock.assert_awaited_once_with(
        session, "company-test", "woocommerce", require_owner=True
    )
    set_state.assert_awaited_once()
    assert result["updated"] == 1
    assert session.commit.await_count == 1


@pytest.mark.asyncio
async def test_item_sync_rejects_unbounded_batch_before_locking():
    lock = AsyncMock()
    with patch("celerp.connectors.ownership.lock_connector_operation", lock):
        with pytest.raises(HTTPException) as exc:
            await set_item_sync(
                "woocommerce",
                ItemSyncRequest(
                    entity_ids=[f"item:{i}" for i in range(201)],
                    enable=False,
                ),
                "company-test",
                SimpleNamespace(id="user-1"),
                None,
                _session(),
            )
    assert exc.value.status_code == 422
    assert "At most 200" in str(exc.value.detail)
    lock.assert_not_awaited()


# ── revoke_credentials ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,ok", [(200, True), (404, True), (500, False)])
async def test_revoke_status_mapping(status, ok):
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p, respx.mock:
        respx.delete(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(status))
        result = await revoke_credentials("woocommerce", "company-test", None, _session())
    assert result.get("ok", False) is ok


@pytest.mark.asyncio
async def test_revoke_woocommerce_cleans_webhooks_before_credentials():
    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(
        webhook_ids=["11"], webhook_secret="secret"
    )
    cleanup = AsyncMock()

    session = _session()
    lock = AsyncMock(return_value=config)
    with patch(
        "celerp.connectors.ownership.lock_connector_operation", lock,
    ), patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.deregister_webhooks",
        cleanup,
    ), url_p, hdr_p, respx.mock:
        token = respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "ck_x:cs_y", "store_handle": STORE}
            )
        )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200)
        )
        result = await revoke_credentials(
            "woocommerce", "company-test", None, session
        )

    assert result == {"ok": True}
    cleanup.assert_awaited_once()
    assert len(token.calls) == 2
    assert delete.called
    assert session.commit.await_count == 1
    lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_webhook_cleanup_failure_still_revokes_credential():
    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(
        webhook_ids=["11"], webhook_secret="secret"
    )
    release = AsyncMock()
    with patch(
        "celerp.connectors.ownership.lock_connector_operation",
        new=AsyncMock(return_value=config),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership", release,
    ), patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.deregister_webhooks",
        new=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    ), url_p, hdr_p, respx.mock:
        token = respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "ck_x:cs_y", "store_handle": STORE}
            )
        )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200)
        )
        result = await revoke_credentials(
            "woocommerce", "company-test", None, _session()
        )

    assert result == {"ok": True}
    assert len(token.calls) == 2
    assert delete.called
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_refuses_new_credential_after_webhook_cleanup():
    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(
        webhook_ids=["11"], webhook_secret="secret"
    )
    lock = AsyncMock(return_value=config)
    with patch(
        "celerp.connectors.ownership.lock_connector_operation", lock,
    ), patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.deregister_webhooks",
        new=AsyncMock(),
    ), url_p, hdr_p, respx.mock:
        token = respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
            side_effect=[
                httpx.Response(
                    200, json={"access_token": "old:key", "store_handle": STORE}
                ),
                httpx.Response(
                    200, json={"access_token": "new:key", "store_handle": STORE}
                ),
            ]
        )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce")
        result = await revoke_credentials(
            "woocommerce", "company-test", None, _session()
        )

    assert len(token.calls) == 2
    assert result["ok"] is False
    assert result["error"] == "connection_changed"
    assert not delete.called


@pytest.mark.asyncio
async def test_revoke_lapsed_woocommerce_still_removes_relay_credential():
    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(
        webhook_ids=["11"], webhook_secret="secret"
    )
    release = AsyncMock()
    with patch(
        "celerp.connectors.ownership.lock_connector_operation",
        new=AsyncMock(return_value=config),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership", release,
    ), url_p, hdr_p, respx.mock:
        respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
            return_value=httpx.Response(402)
        )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200)
        )
        result = await revoke_credentials(
            "woocommerce", "company-test", None, _session()
        )

    assert result == {"ok": True}
    assert delete.called
    release.assert_awaited_once()
