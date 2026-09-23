# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import celerp.gateway as gateway
import celerp.gateway.client as gateway_client
import celerp.gateway.state as gateway_state
from celerp.config import settings
from celerp.gateway.client import GatewayClient


@pytest.fixture
def gw():
    return GatewayClient("key-a", "iid-a", "wss://relay.celerp.com/ws/connect")


def test_proxy_drain_is_generation_owned(gw):
    first = gw.begin_proxy_drain()
    second = gw.begin_proxy_drain()
    gw.end_proxy_drain(first)
    assert gw.owns_proxy_drain(second)
    gw.end_proxy_drain(second)
    assert gw.is_draining_for_reconfigure() is False


@pytest.mark.asyncio
async def test_close_invalidates_deferred_drain(gw):
    generation = gw.begin_proxy_drain()
    await gw.close()
    assert gw.owns_proxy_drain(generation) is False


@pytest.mark.asyncio
async def test_reaper_defers_during_drain_or_inflight(gw):
    generation = gw.begin_proxy_drain()
    assert await gw._should_reap() is False
    gw.end_proxy_drain(generation)
    gw._inflight["r1"] = MagicMock()
    assert await gw._should_reap() is False


@pytest.mark.asyncio
async def test_disconnect_state_ignores_late_session_refresh(gw, monkeypatch):
    monkeypatch.setattr(settings, "cloud_disconnected", True)
    gateway_state.set_session_token("")
    await gw._dispatch({
        "type": "session.refresh",
        "payload": {"session_token": "late-token"},
    })
    assert gateway_state.get_session_token() == ""


@pytest.mark.asyncio
async def test_disconnect_state_ignores_late_handshake(gw, monkeypatch):
    monkeypatch.setattr(settings, "cloud_disconnected", True)
    gateway_state.set_session_token("")
    await gw._dispatch({
        "type": "hello_ack",
        "payload": {"instance_id": "iid-a", "session_token": "late-token"},
    })
    assert gateway_state.get_session_token() == ""
    assert gw.relay_status != "active"


@pytest.mark.asyncio
async def test_active_elsewhere_is_retryable_product_state(gw):
    await gw._dispatch({
        "type": "error",
        "payload": {"code": "installation_active", "message": "busy"},
    })
    assert gw.ownership_conflict is True
    assert gw.relay_status == "active_elsewhere"
    assert gw.is_serving("key-a") is True


@pytest.mark.asyncio
async def test_shutdown_cannot_clear_newer_generation():
    old = MagicMock()
    new = MagicMock()
    old_task = asyncio.create_task(asyncio.sleep(60))
    new_task = asyncio.create_task(asyncio.sleep(60))

    async def close_old():
        gateway_client.set_client(new)
        gateway._run_task = new_task

    old.close = close_old
    gateway_client.set_client(old)
    gateway._run_task = old_task
    try:
        await gateway.shutdown()
        assert gateway_client.get_client() is new
        assert gateway._run_task is new_task
        assert new_task.cancelled() is False
    finally:
        new_task.cancel()
        old_task.cancel()
        await asyncio.gather(new_task, old_task, return_exceptions=True)
        gateway_client.set_client(None)
        gateway._run_task = None


def test_ensure_running_never_replaces_existing_client(monkeypatch):
    existing = MagicMock()
    gateway_client.set_client(existing)
    monkeypatch.setattr(settings, "gateway_token", "key-a")
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    try:
        with patch("celerp.gateway.client.GatewayClient") as constructor:
            gateway.ensure_running()
        constructor.assert_not_called()
        existing.stop.assert_not_called()
    finally:
        gateway_client.set_client(None)


@pytest.mark.asyncio
async def test_explicit_reconnect_requests_one_runtime_restart(monkeypatch):
    from celerp.services import cloud_entitlement

    live = MagicMock(relay_status="inactive")
    live.uses_token.return_value = True
    monkeypatch.setattr(settings, "backup_enabled", False)
    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("cloud", "active")),
        patch("celerp.gateway.state.relay_session_headers",
              return_value={"X-Session-Token": "", "X-Instance-ID": "iid-a"}),
        patch.object(
            cloud_entitlement, "reconfigure_gateway_runtime",
            new=AsyncMock(return_value=False),
        ) as reconfigure,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        assert await cloud_entitlement.apply_activation_state(
            "key-a", "iid-a", public_url="https://x.celerp.com",
            tier="cloud", status="active", restart_transport=True)

    reconfigure.assert_awaited_once_with(restart=True)


@pytest.mark.asyncio
async def test_share_lookup_failure_cannot_abort_authoritative_downgrade(monkeypatch):
    from celerp.services import cloud_entitlement

    live = MagicMock(relay_status="active")
    live.uses_token.return_value = True
    monkeypatch.setattr(settings, "backup_enabled", False)
    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.has_active_share",
              new=AsyncMock(side_effect=RuntimeError("db unavailable"))),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("cloud", "active")),
        patch("celerp.gateway.state.relay_session_headers",
              return_value={"X-Session-Token": "paid", "X-Instance-ID": "iid-a"}),
        patch.object(
            cloud_entitlement, "reconfigure_gateway_runtime",
            new=AsyncMock(return_value=False),
        ) as reconfigure,
        patch("celerp.services.backup_scheduler.stop") as stop_backup,
    ):
        assert await cloud_entitlement.apply_activation_state(
            "key-a", "iid-a", public_url=None,
            tier="free", status="active")

    reconfigure.assert_awaited_once_with(restart=False)
    stop_backup.assert_called_once()


@pytest.mark.asyncio
async def test_takeover_prepares_fresh_verifier_without_relay_call(monkeypatch):
    from celerp.routers.health import cloud_activate_api

    monkeypatch.setattr(settings, "activation_verifier", "copied-proof")
    with (
        patch("celerp.config.refresh_activation_verifier",
              return_value="fresh-proof") as refresh,
        patch("celerp.config.ensure_instance_id", return_value="iid-a"),
        patch("celerp.gateway.state.with_relay_client",
              new=AsyncMock()) as relay_call,
    ):
        result = await cloud_activate_api({"intent": "takeover"})

    assert result["verification_required"] is True
    refresh.assert_called_once()
    relay_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicted_connect_prefers_fresh_verifier(monkeypatch):
    from celerp.routers.health import cloud_activate_api

    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "fresh-proof")
    live = MagicMock(ownership_conflict=True)
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "gateway_token": "rotated-key",
        "tier": "cloud",
        "status": "active",
    }
    seen = {}

    class Client:
        async def post(self, _url, **kwargs):
            seen.update(kwargs)
            return response

    async def run(_timeout, operation):
        return await operation(Client())

    with (
        patch("celerp.config.ensure_instance_id", return_value="iid-a"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="copied-key")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="copied-key")),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(side_effect=AssertionError("old key must not win"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.routers.health._apply_gateway_token_api",
              new=AsyncMock(return_value=True)) as apply_state,
    ):
        result = await cloud_activate_api({"intent": "connect"})

    assert result["connected"] is True
    assert seen["json"]["activation_verifier"] == "fresh-proof"
    assert "Authorization" not in seen.get("headers", {})
    assert apply_state.await_args.kwargs["expected_verifier"] == "fresh-proof"


@pytest.mark.asyncio
async def test_retryable_service_error_does_not_claim_ownership(gw):
    await gw._dispatch({
        "type": "error",
        "payload": {"code": "service_unavailable", "message": "retry"},
    })
    assert gw.ownership_conflict is False
    assert gw.relay_status == "error"
    assert gw._retryable_error is True


@pytest.mark.asyncio
async def test_conflicted_connect_waits_for_fresh_proof(monkeypatch):
    from celerp.routers.health import cloud_activate_api

    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "fresh-proof")
    live = MagicMock(ownership_conflict=True)
    response = MagicMock(status_code=401)
    response.json.return_value = {"detail": "proof not approved"}

    class Client:
        async def post(self, _url, **_kwargs):
            return response

    async def run(_timeout, operation):
        return await operation(Client())

    with (
        patch("celerp.config.ensure_instance_id", return_value="iid-a"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="copied-key")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="copied-key")),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(side_effect=AssertionError("old key must not win"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.routers.health._apply_gateway_token_api",
              new=AsyncMock()) as apply_state,
    ):
        result = await cloud_activate_api({"intent": "connect"})

    assert result["verification_pending"] is True
    apply_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_reconfigure_stale_generation_cannot_close_replacement():
    from celerp.services import cloud_entitlement

    current = MagicMock()
    current.has_inflight_proxy_requests.return_value = False
    replacement = MagicMock()
    shutdown = AsyncMock()
    ensure = MagicMock()

    with (
        patch("celerp.gateway.client.get_client",
              side_effect=[current, replacement]),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running", new=ensure),
        patch.object(settings, "cloud_disconnected", False),
    ):
        assert await cloud_entitlement.reconfigure_gateway_runtime(
            restart=True) is False

    shutdown.assert_not_awaited()
    ensure.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_reconfigure_waits_for_inflight_response():
    from celerp.services import cloud_entitlement

    idle = asyncio.Event()
    current = MagicMock()
    current.has_inflight_proxy_requests.return_value = True
    current.begin_proxy_drain.return_value = 7
    current.owns_proxy_drain.return_value = True
    current.wait_for_proxy_idle = AsyncMock(side_effect=idle.wait)
    current.end_proxy_drain = MagicMock()

    shutdown = AsyncMock()
    ensure = MagicMock()
    with (
        patch("celerp.gateway.client.get_client", return_value=current),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running", new=ensure),
        patch.object(settings, "cloud_disconnected", False),
    ):
        assert await cloud_entitlement.reconfigure_gateway_runtime(
            restart=True) is True
        shutdown.assert_not_awaited()
        idle.set()
        await asyncio.gather(
            *list(cloud_entitlement._runtime_transition_tasks))

    shutdown.assert_awaited_once()
    ensure.assert_called_once()
    current.end_proxy_drain.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_runtime_reconfigure_timeout_cannot_hold_transition(monkeypatch):
    from celerp.services import cloud_entitlement

    never = asyncio.Event()
    current = MagicMock()
    current.has_inflight_proxy_requests.return_value = True
    current.begin_proxy_drain.return_value = 11
    current.owns_proxy_drain.return_value = True
    current.wait_for_proxy_idle = AsyncMock(side_effect=never.wait)

    shutdown = AsyncMock()
    monkeypatch.setattr(cloud_entitlement, "RUNTIME_DRAIN_TIMEOUT", 0)
    with (
        patch("celerp.gateway.client.get_client", return_value=current),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running"),
        patch.object(settings, "cloud_disconnected", True),
    ):
        assert await cloud_entitlement.reconfigure_gateway_runtime(
            restart=False) is True
        await asyncio.gather(
            *list(cloud_entitlement._runtime_transition_tasks))

    shutdown.assert_awaited_once()
    current.end_proxy_drain.assert_called_once_with(11)
