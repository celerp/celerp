# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for connector setup and disconnect routes."""
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
from ui.i18n import t

RELAY = "https://relay.test"
WOO_DEFAULTS = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}
STORE = "https://store.test"


def _relay_state():
    return (
        patch("celerp.gateway.state.relay_http_url", return_value=RELAY),
        patch("celerp.gateway.state.relay_session_headers",
              return_value={"X-Session-Token": "s", "X-Instance-ID": "i"}),
    )


def _creds(store_url: str | None = STORE) -> ApiKeyCredentials:
    return ApiKeyCredentials(consumer_key="ck_x", consumer_secret="cs_y", store_url=store_url)


def _revision(value: str = "rev-1", status: int = 200):
    """The relay holds a WooCommerce connection at `value`, with its credential."""
    body = {"revision": value} if status == 200 else {}
    respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
        return_value=httpx.Response(200, json={"access_token": "ck_x:cs_y", "store_handle": STORE})
    )
    return respx.get(f"{RELAY}/tokens/woocommerce/revision").mock(
        return_value=httpx.Response(status, json=body)
    )


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
        new=AsyncMock(return_value=SimpleNamespace(
            webhook_ids=[], webhook_secret=None, first_sync_at=None,
            entity_directions=dict(WOO_DEFAULTS))),
    ), patch(
        "celerp.connectors.ownership.release_connector_ownership",
        new=AsyncMock(),
    ), patch(
        "celerp.connectors.ownership.bind_connector_store",
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
    ), patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.deregister_webhooks",
        new=AsyncMock(),
    ):
        yield


# ── store_credentials ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_store_probes_then_stores_on_relay():
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p, respx.mock:
        probe = respx.get(f"{STORE}/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[]))
        _revision()
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
        _revision()
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
        _revision()
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
    assert result["detail"] == t("error.url_invalid", "en")


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
async def test_store_webhook_failure_rolls_back_the_connection():
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
        _revision()
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200))
        result = await store_credentials(
            "woocommerce", _creds(), "company-test", None, _session()
        )

    assert result["ok"] is False
    assert delete.called
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_webhook_failure_removes_hooks_created_before_it():
    """Hooks created before a failure, even ones whose ids never came back, are
    removed by where they deliver before the connection is dropped."""
    from celerp.connectors.woocommerce import WooCommerceConnector

    url_p, hdr_p = _relay_state()
    with patch(
        "celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks",
        new=AsyncMock(side_effect=httpx.ReadTimeout("no response")),
    ), url_p, hdr_p, respx.mock:
        respx.get(f"{STORE}/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[]))
        respx.post(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200, json={"stored": True}))
        _revision()
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200))
        result = await store_credentials(
            "woocommerce", _creds(), "company-test", None, _session()
        )
        cleanup = WooCommerceConnector.deregister_webhooks

    assert result["ok"] is False
    assert delete.called
    delivery_urls = {call.args[1] for call in cleanup.await_args_list}
    assert delivery_urls == {f"{RELAY}/webhooks/woocommerce/events"}
    # Once to clear hooks left by an earlier connection, once after the failure.
    assert cleanup.await_count == 3


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
        _revision()
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
    lock = AsyncMock(return_value=SimpleNamespace(entity_directions={}))
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
    assert "Sync at most 200 at a time." in str(exc.value.detail)
    lock.assert_not_awaited()


# ── revoke_credentials ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,ok", [(200, True), (404, True), (500, False)])
async def test_revoke_status_mapping(status, ok):
    url_p, hdr_p = _relay_state()
    with url_p, hdr_p, respx.mock:
        _revision()
        respx.delete(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(status))
        result = await revoke_credentials("woocommerce", "company-test", None, _session())
    assert result.get("ok", False) is ok


@pytest.mark.asyncio
async def test_revoke_woocommerce_removes_webhooks_before_the_credential():
    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(
        webhook_ids=["11"], webhook_secret="secret"
    )
    delete_called_at_cleanup = []
    cleanup = AsyncMock(side_effect=lambda *a: delete_called_at_cleanup.append(delete.called))

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
        _revision()
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200)
        )
        result = await revoke_credentials(
            "woocommerce", "company-test", None, session
        )

    assert result == {"ok": True}
    cleanup.assert_awaited_once()
    assert cleanup.await_args.args[1:] == (f"{RELAY}/webhooks/woocommerce/events", ["11"])
    assert delete_called_at_cleanup == [False]
    assert len(token.calls) == 1
    assert delete.called
    assert session.commit.await_count == 1
    lock.assert_awaited_once()


def _ambiguous_revoke(scope):
    """Patches for a disconnect while the connector is linked twice; ``scope``
    lists what this company disconnects."""
    from celerp.connectors.ownership import ConnectorOwnershipAmbiguousError

    remote = AsyncMock()
    release = AsyncMock()
    patches = (
        patch(
            "celerp.connectors.ownership.lock_connector_operation",
            new=AsyncMock(side_effect=ConnectorOwnershipAmbiguousError("ambiguous")),
        ),
        patch("celerp.connectors.ownership.connector_release_scope",
              new=AsyncMock(**scope)),
        patch("celerp.connectors.remote_state.revoke_connector_remote_state", remote),
        patch("celerp.connectors.ownership.release_connector_ownership", release),
    )
    return patches, remote, release


@pytest.mark.asyncio
async def test_revoke_while_linked_twice_still_disconnects():
    """A disconnect always removes the connection, even while the connector is
    linked to more than one company."""
    patches, remote, release = _ambiguous_revoke(
        {"return_value": [SimpleNamespace(webhook_ids=["11"])]}
    )
    session = _session()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await revoke_credentials("woocommerce", "company-test", None, session)
    assert result == {"ok": True}
    remote.assert_awaited_once_with("company-test", "woocommerce", webhook_ids=["11"], force=False)
    release.assert_awaited_once()
    assert session.commit.await_count == 1


@pytest.mark.asyncio
async def test_revoke_own_and_legacy_claim_revokes_every_webhook():
    """This company's row plus a legacy row with no company: the whole connection is
    removed, with the webhooks of both rows."""
    patches, remote, release = _ambiguous_revoke({"return_value": [
        SimpleNamespace(webhook_ids=["11"]), SimpleNamespace(webhook_ids=["11", "12"]),
    ]})
    with patches[0], patches[1], patches[2], patches[3]:
        result = await revoke_credentials("woocommerce", "company-test", None, _session())
    assert result == {"ok": True}
    remote.assert_awaited_once_with(
        "company-test", "woocommerce", webhook_ids=["11", "12"], force=False
    )
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_without_own_claim_is_refused():
    from celerp.connectors.ownership import ConnectorOwnershipError

    patches, remote, release = _ambiguous_revoke(
        {"side_effect": ConnectorOwnershipError("not connected")}
    )
    session = _session()
    with patches[0], patches[1], patches[2], patches[3], pytest.raises(HTTPException) as exc:
        await revoke_credentials("woocommerce", "company-test", None, session)
    assert exc.value.status_code == 409
    session.rollback.assert_awaited()
    remote.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token_response", [{}, {"status_code": 402}])
async def test_revoke_keeps_the_connection_when_webhook_removal_is_unconfirmed(token_response):
    """A failed webhook removal, or store access that is no longer available,
    leaves everything connected so nothing keeps posting to a dropped store."""
    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(webhook_ids=["11"], webhook_secret="secret")
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
        _revision()
        if token_response:
            respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
                return_value=httpx.Response(**token_response)
            )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200)
        )
        session = _session()
        result = await revoke_credentials("woocommerce", "company-test", None, session)

    assert result["ok"] is False
    assert result["error"] == "relay_error"
    assert not delete.called
    release.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token_response", [{}, {"status_code": 402}])
async def test_forced_revoke_disconnects_and_records_that_it_was_forced(token_response):
    from celerp.connectors.ownership import RESET_STATUS_FORCED

    url_p, hdr_p = _relay_state()
    config = SimpleNamespace(webhook_ids=["11"], webhook_secret="secret")
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
        revision = _revision("forced-revision")
        if token_response:
            respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
                return_value=httpx.Response(**token_response)
            )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce").mock(
            return_value=httpx.Response(200)
        )
        session = _session()
        result = await revoke_credentials(
            "woocommerce", "company-test", None, session, force=True
        )

    assert result == {"ok": True}
    assert delete.calls.last.request.headers["X-Celerp-Connector-Revision"] == "forced-revision"
    assert len(revision.calls) == 2
    assert release.await_args.kwargs["status"] == RESET_STATUS_FORCED
    assert session.commit.await_count == 1


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
        revision = respx.get(f"{RELAY}/tokens/woocommerce/revision").mock(
            side_effect=[
                httpx.Response(200, json={"revision": "old-revision"}),
                httpx.Response(200, json={"revision": "new-revision"}),
            ]
        )
        token = respx.get(f"{RELAY}/tokens/woocommerce/access-token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "old:key", "store_handle": STORE}
            )
        )
        delete = respx.delete(f"{RELAY}/tokens/woocommerce")
        result = await revoke_credentials(
            "woocommerce", "company-test", None, _session()
        )

    assert len(revision.calls) == 2
    assert len(token.calls) == 1
    assert result["ok"] is False
    assert result["error"] == "connection_changed"
    assert not delete.called


def test_disconnect_does_not_need_a_connect_session():
    """Leaving a connection is always possible, with or without Celerp Connect."""
    from celerp_connectors.routes import _require_session, open_router

    route = next(r for r in open_router.routes
                 if r.path == "/connectors/{connector_name}/credentials" and "DELETE" in r.methods)
    assert route.endpoint is revoke_credentials
    assert _require_session not in [d.call for d in route.dependant.dependencies]


@pytest.mark.asyncio
async def test_accountant_disconnect_goes_ahead_when_the_relay_is_out_of_reach():
    """Without Connect the relay cannot be asked to drop its copy of the key; the
    accountant connection is this company's own, so it disconnects anyway."""
    from celerp.connectors.ownership import RESET_STATUS_DISCONNECTED

    url_p, hdr_p = _relay_state()
    release = AsyncMock()
    with patch("celerp.connectors.ownership.release_connector_ownership", release), \
            url_p, hdr_p, respx.mock:
        respx.get(f"{RELAY}/tokens/celerp/revision").mock(return_value=httpx.Response(401))
        session = _session()
        result = await revoke_credentials("celerp", "company-test", None, session)

    assert result == {"ok": True}
    assert release.await_args.kwargs["status"] == RESET_STATUS_DISCONNECTED
    session.rollback.assert_not_awaited()
    assert session.commit.await_count == 1


# ── connecting starts nothing ────────────────────────────────────────────────

def _store_ok():
    respx.get(f"{STORE}/wp-json/wc/v3/products").mock(return_value=httpx.Response(200, json=[]))
    _revision()
    respx.delete(f"{RELAY}/tokens/woocommerce").mock(return_value=httpx.Response(404))
    respx.post(f"{RELAY}/tokens/woocommerce").mock(
        return_value=httpx.Response(200, json={"stored": True}))


@pytest.mark.asyncio
async def test_connect_registers_paused_webhooks_for_the_directions():
    from celerp.connectors.woocommerce import WooCommerceConnector

    register = AsyncMock(return_value=["11", "12"])
    url_p, hdr_p = _relay_state()
    with patch("celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks", register), \
            url_p, hdr_p, respx.mock:
        _store_ok()
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())

    assert result == {"ok": True}
    assert register.await_args.kwargs["status"] == "paused"
    assert register.await_args.kwargs["topics"] == WooCommerceConnector().webhook_topics(WOO_DEFAULTS)


@pytest.mark.asyncio
async def test_read_only_key_message():
    refused = httpx.Response(401, json={"code": "woocommerce_rest_cannot_create",
                                        "message": "Sorry, you are not allowed to create resources."},
                             request=httpx.Request("POST", f"{STORE}/wp-json/wc/v3/webhooks"))
    register = AsyncMock(side_effect=httpx.HTTPStatusError(
        "HTTP 401 woocommerce_rest_cannot_create", request=refused.request, response=refused))
    release = AsyncMock()
    url_p, hdr_p = _relay_state()
    with patch("celerp.connectors.woocommerce.WooCommerceConnector.register_webhooks", register), \
            patch("celerp.connectors.ownership.release_connector_ownership", release), \
            url_p, hdr_p, respx.mock:
        _store_ok()
        result = await store_credentials("woocommerce", _creds(), "company-test", None, _session())

    assert result["ok"] is False
    assert result["error"] == "read_only_key"
    assert result["detail"] == t("connectors.err_read_only_key")
    assert "Read/Write" in result["detail"]
    release.assert_awaited_once()


def _ui_connect_patches(start):
    from ui.api_client import APIError  # noqa: F401

    catalog = [{"id": "woocommerce", "name": "WooCommerce", "category": "website",
                "connected": True, "auth_type": "api_key", "entities": []}]
    config = SimpleNamespace(connector="woocommerce", entity_directions=dict(WOO_DEFAULTS),
                             first_sync_at=None, sync_frequency="realtime", connect_lapsed_at=None)
    return (
        patch("ui.api_client.get_company", AsyncMock(return_value={
            "id": "00000000-0000-0000-0000-000000000002", "settings": {}, "current_role": "owner"})),
        patch("ui.api_client.store_connector_credentials", AsyncMock(return_value={"ok": True})),
        patch("ui.api_client.start_connector_sync", start),
        patch("ui.routes.settings_connectors._fetch_catalog", AsyncMock(return_value=(catalog, "", False))),
        patch("ui.routes.settings_connectors._get_connector_config", AsyncMock(return_value=config)),
        patch("ui.routes.settings_connectors._get_last_runs", AsyncMock(return_value={})),
        patch("ui.routes.settings_connectors._attention", AsyncMock(return_value=[])),
    )


@pytest.mark.asyncio
async def test_connect_starts_no_sync():
    from contextlib import ExitStack

    from httpx import ASGITransport, AsyncClient
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    start = AsyncMock(return_value={"ok": True})
    with ExitStack() as stack:
        for p in _ui_connect_patches(start):
            stack.enter_context(p)
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as client:
            client.cookies.update({"celerp_token": make_test_token(role="owner")})
            r = await client.post("/settings/connectors/woocommerce/connect-apikey", data={
                "store_url": STORE, "consumer_key": "ck_x", "consumer_secret": "cs_y"})

    assert r.status_code == 200
    start.assert_not_awaited()
    # One next step: review the first sync.
    assert t("connectors.review_first_sync") in r.text
    assert 'href="/settings/connectors/woocommerce/first-sync"' in r.text
    assert t("connectors.sync_now") not in r.text


@pytest.mark.asyncio
async def test_first_view_starts_no_sync():
    from contextlib import ExitStack

    from fasthtml.common import to_xml
    from ui.routes import settings_connectors as routes

    start = AsyncMock(return_value={"ok": True})
    spawned = []
    with ExitStack() as stack:
        for p in _ui_connect_patches(start):
            stack.enter_context(p)
        stack.enter_context(patch.object(routes, "_awaiting_reconnect", AsyncMock(return_value=[])))
        stack.enter_context(patch("celerp.services.background.spawn_background", spawned.append))
        html = to_xml(await routes.connectors_tab_content("en", "jwt", "website", "company-1"))

    for coro in spawned:
        coro.close()
    assert spawned == []
    start.assert_not_awaited()
    assert t("connectors.review_first_sync") in html
