# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression coverage for Connect identity, proof, and disconnect ordering."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from test_config import _reload_config, _restore_config_module  # noqa: F401


def _relay_state(*, tier="cloud", status="active", entitled=True, **extra):
    data = {
        "tier": tier,
        "status": status,
        "connect_entitled": entitled,
        "feature_flags": {
            "payments_enabled": False,
            "external_db": False,
            "external_storage": False,
            "grace_period_ends": None,
        },
    }
    data.update(extra)
    return data


def test_foreign_credential_cannot_override_pending_local_proof(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {
        "token": "key-a", "instance_id": "instance-b",
        "activation_verifier": "proof-b"}})
    assert mod.record_cloud_activation(
        "key-a", "instance-a", expected_api_key="key-a") is False
    cloud = mod.read_config()["cloud"]
    assert cloud["token"] == "key-a"
    assert cloud["instance_id"] == "instance-b"
    assert cloud["activation_verifier"] == "proof-b"


def test_verifier_requires_exact_proof_and_local_identity(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {
        "instance_id": "instance-b", "activation_verifier": "proof-b"}})
    assert mod.record_cloud_activation(
        "new-key", "instance-a", expected_verifier="proof-b") is False
    assert mod.record_cloud_activation(
        "new-key", "instance-b", expected_verifier="wrong") is False
    assert mod.record_cloud_activation(
        "new-key", "instance-b", expected_verifier="proof-b") is True


def test_disconnect_wins_over_inflight_activation(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {"token": "key", "instance_id": "iid"}})
    mod.set_cloud_disconnected(True)
    assert mod.record_cloud_activation(
        "key", "iid", expected_api_key="key") is False
    assert mod.read_config()["cloud"]["disconnected"] is True


@pytest.mark.asyncio
async def test_fetch_relay_auth_is_observational(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "access_token": "jwt-a", "instance_id": "instance-a"}
    client = MagicMock(post=AsyncMock(return_value=response))
    from celerp.config import settings
    monkeypatch.setattr(settings, "gateway_instance_id", "instance-b")
    monkeypatch.setattr(settings, "activation_verifier", "proof-b")
    from celerp.gateway.state import fetch_relay_auth
    assert await fetch_relay_auth(client, api_key="key-a") == (
        "jwt-a", "instance-a")
    assert settings.gateway_instance_id == "instance-b"
    assert settings.activation_verifier == "proof-b"


@pytest.mark.asyncio
async def test_exact_stale_token_recovery_prefers_local_verifier(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "proof-b")
    response = MagicMock(status_code=200)
    response.json.return_value = _relay_state(gateway_token="new-key-b")
    seen = []
    class Client:
        async def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return response
    async def run(_timeout, operation):
        return await operation(Client())
    with (
        patch("celerp.config.ensure_instance_id", return_value="instance-b"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.routers.health._apply_gateway_token_api",
              new=AsyncMock(return_value=True)) as apply_state,
    ):
        from celerp.routers.health import cloud_activate_api
        data = await cloud_activate_api({"intent": "connect"})
    assert data["connected"] is True
    assert seen[0][1]["json"]["instance_id"] == "instance-b"
    assert seen[0][1]["json"]["activation_verifier"] == "proof-b"
    assert "Authorization" not in seen[0][1].get("headers", {})
    assert apply_state.await_args.args[:2] == ("new-key-b", "instance-b")
    assert apply_state.await_args.kwargs["expected_verifier"] == "proof-b"


@pytest.mark.asyncio
async def test_entitlement_sync_defers_foreign_key_while_local_proof_pending(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "proof-b")
    client = MagicMock(post=AsyncMock())
    async def run(_timeout, operation):
        return await operation(client)
    with (
        patch("celerp.config.ensure_instance_id", return_value="instance-b"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
    ):
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        assert await sync_existing_entitlement() is None
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_identity_converges_only_after_successful_activation(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "")
    response = MagicMock(status_code=200)
    response.json.return_value = _relay_state()
    client = MagicMock(post=AsyncMock(return_value=response))
    async def run(_timeout, operation):
        return await operation(client)
    with (
        patch("celerp.config.ensure_instance_id", return_value="instance-b"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.services.cloud_entitlement.apply_activation_state",
              new=AsyncMock(return_value=True)) as apply_state,
    ):
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        assert await sync_existing_entitlement() is not None
    assert apply_state.await_args.args[:2] == ("key-a", "instance-a")
    assert apply_state.await_args.kwargs["expected_api_key"] == "key-a"
    assert apply_state.await_args.kwargs["persist_state"] is True


@pytest.mark.asyncio
async def test_failed_foreign_activation_never_converges_identity(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "")
    response = MagicMock(status_code=404)
    client = MagicMock(post=AsyncMock(return_value=response))
    async def run(_timeout, operation):
        return await operation(client)
    with (
        patch("celerp.config.ensure_instance_id", return_value="instance-b"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.services.cloud_entitlement.apply_activation_state",
              new=AsyncMock()) as apply_state,
    ):
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        assert await sync_existing_entitlement() is None
    apply_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_environment_key_ignores_stale_persisted_key_for_cas(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "activation_verifier", "")
    response = MagicMock(status_code=200)
    response.json.return_value = {"tier": "cloud", "status": "active"}
    client = MagicMock(post=AsyncMock(return_value=response))
    async def run(_timeout, operation):
        return await operation(client)
    with (
        patch("celerp.config.ensure_instance_id", return_value="instance-a"),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="env-key-a")),
        patch("celerp.services.cloud_entitlement.persisted_api_key",
              new=AsyncMock(return_value="stale-disk-key-b")),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.services.cloud_entitlement.apply_activation_state",
              new=AsyncMock(return_value=True)) as apply_state,
    ):
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        await sync_existing_entitlement()
    assert apply_state.await_args.kwargs["expected_api_key"] is None
    assert apply_state.await_args.kwargs["persist_state"] is False


@pytest.mark.asyncio
async def test_send_otp_omits_foreign_bearer():
    response = MagicMock(status_code=200)
    response.json.return_value = {}
    seen = {}
    class Client:
        async def post(self, _url, **kwargs):
            seen.update(kwargs)
            return response
    async def run(_timeout, operation):
        return await operation(Client())
    with (
        patch("celerp.config.ensure_connect_identity",
              return_value=("instance-b", "proof-b")),
        patch("celerp.services.cloud_entitlement.stored_api_key",
              new=AsyncMock(return_value="key-a")),
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
    ):
        from celerp.routers.health import cloud_send_otp_api
        data = await cloud_send_otp_api({"email": "owner@example.com"})
    assert data["ok"] is True
    assert "Authorization" not in seen["headers"]
    assert seen["json"]["instance_id"] == "instance-b"


@pytest.mark.asyncio
async def test_account_only_activation_preserves_disconnect(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(settings, "celerp_public_url", "")
    monkeypatch.setattr(settings, "cloud_disconnected", True)
    monkeypatch.setattr(settings, "backup_enabled", False)
    live = MagicMock()
    shutdown = AsyncMock()
    with (
        patch("celerp.config.record_cloud_activation", return_value=True) as record,
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.services.backup_scheduler.stop") as stop,
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "new-key", "instance-b", expected_verifier="proof-b",
            keep_disconnected=True) is True
    assert settings.cloud_disconnected is True
    assert settings.gateway_token == ""
    shutdown.assert_awaited_once()
    stop.assert_called_once()
    assert record.call_args.kwargs["keep_disconnected"] is True


@pytest.mark.asyncio
async def test_healthy_serving_instance_is_not_restarted(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "gateway_token", "same-key")
    monkeypatch.setattr(settings, "gateway_instance_id", "same-iid")
    monkeypatch.setattr(settings, "celerp_public_url", "https://same.celerp.app")
    monkeypatch.setattr(settings, "backup_enabled", False)
    live = MagicMock()
    live.is_serving.return_value = True
    live.relay_status = "active"
    shutdown = AsyncMock()
    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.ensure_running"),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("cloud", "active")),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "paid-session", "X-Instance-ID": "same-iid"}),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        accepted = await apply_activation_state(
            "same-key", "same-iid",
            public_url="https://same.celerp.app",
            tier="cloud", status="active",
            expected_api_key="same-key")
    assert accepted is True
    shutdown.assert_not_awaited()
    assert settings.gateway_token == "same-key"


@pytest.mark.asyncio
async def test_boot_sync_uses_configured_credential_without_persistence_policy():
    sync = AsyncMock()
    with patch(
        "celerp.services.cloud_entitlement.sync_existing_entitlement", new=sync
    ):
        from celerp.main import _try_sync_existing_entitlement
        await _try_sync_existing_entitlement()
    sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_persisted_sync_converges_free_identity_and_clears_stale_paid_url(
        tmp_path, monkeypatch):
    """A valid free credential repairs identity but never inherits a stale paid URL."""
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {
        "token": "key-a",
        "instance_id": "instance-b",
        "public_url": "https://stale.celerp.com",
    }})
    mod.load_cloud_config()
    mod.settings.backup_enabled = False

    response = MagicMock(status_code=200)
    response.json.return_value = {
        "gateway_token": "key-a",
        "instance_id": "instance-a",
        "public_url": None,
        "tier": "free",
        "status": "active",
        "connect_entitled": False,
        "feature_flags": {
            "payments_enabled": False,
            "external_db": False,
            "external_storage": False,
            "grace_period_ends": None,
        },
    }

    class Client:
        async def post(self, _url, **_kwargs):
            return response

    async def run(_timeout, operation):
        return await operation(Client())

    with (
        patch("celerp.gateway.state.fetch_relay_auth",
              new=AsyncMock(return_value=("jwt-a", "instance-a"))),
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.gateway.client.get_client", return_value=None),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        data = await sync_existing_entitlement()

    assert data is not None and data["tier"] == "free"
    cloud = mod.read_config()["cloud"]
    assert cloud["instance_id"] == "instance-a"
    assert cloud["token"] == "key-a"
    assert not cloud.get("public_url")
    assert mod.settings.gateway_instance_id == "instance-a"
    assert mod.settings.celerp_public_url == ""
    ensure_running.assert_not_called()


@pytest.mark.asyncio
async def test_activation_restarts_active_free_runtime_when_account_becomes_paid(
        monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "backup_enabled", False)
    monkeypatch.setattr(settings, "celerp_public_url", "https://paid.celerp.com")
    live = MagicMock(relay_status="active")
    live.is_serving.return_value = True
    live.is_draining_for_reconfigure.return_value = False
    live.has_inflight_proxy_requests.return_value = False
    replacement = MagicMock(relay_status="active")
    shutdown = AsyncMock()

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("free", "active")),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "", "X-Instance-ID": "iid"}),
        patch("celerp.gateway.client.get_client",
              side_effect=[live, replacement]),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "key", "iid", public_url="https://paid.celerp.com",
            tier="cloud", status="active", connect_entitled=True,
            expected_api_key="key")

    shutdown.assert_awaited_once()
    ensure_running.assert_called_once()


@pytest.mark.asyncio
async def test_activation_restarts_active_paid_runtime_when_account_becomes_free_with_share(
        monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "backup_enabled", False)
    live = MagicMock(relay_status="active")
    live.is_serving.return_value = True
    live.is_draining_for_reconfigure.return_value = False
    live.has_inflight_proxy_requests.return_value = False
    replacement = MagicMock(relay_status="active")
    shutdown = AsyncMock()

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("cloud", "active")),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "paid-session", "X-Instance-ID": "iid"}),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=True)),
        patch("celerp.gateway.client.get_client",
              side_effect=[live, replacement]),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "key", "iid", public_url=None,
            tier="free", status="active", connect_entitled=False,
            expected_api_key="key")

    shutdown.assert_awaited_once()
    ensure_running.assert_called_once()


@pytest.mark.asyncio
async def test_paid_to_paid_snapshot_updates_without_transport_restart(monkeypatch):
    """Tier/feature changes do not reconnect when paid transport remains paid."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "backup_enabled", False)
    live = MagicMock(relay_status="active")
    live.is_serving.return_value = True
    shutdown = AsyncMock()
    apply_flags = AsyncMock()
    flags = {
        "payments_enabled": True,
        "external_db": False,
        "external_storage": False,
        "grace_period_ends": None,
    }

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "paid-session", "X-Instance-ID": "iid"}),
        patch("celerp.gateway.state.apply_feature_flags_async", new=apply_flags),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "key", "iid", public_url="https://paid.celerp.com",
            tier="ai", status="active", connect_entitled=True,
            feature_flags=flags, expected_api_key="key")

    apply_flags.assert_awaited_once_with(flags, persist=True)
    shutdown.assert_not_awaited()
    ensure_running.assert_called_once()


@pytest.mark.asyncio
async def test_activation_defers_restart_until_current_proxy_response_finishes(monkeypatch):
    """A remote Web Access claim must return before its gateway generation is rebuilt."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "backup_enabled", False)

    live = MagicMock(relay_status="active")
    live.is_serving.return_value = True
    live.is_draining_for_reconfigure.return_value = False
    live.has_inflight_proxy_requests.return_value = True
    live.begin_proxy_drain = MagicMock()
    release = asyncio.Event()

    async def _wait_for_idle():
        await release.wait()

    live.wait_for_proxy_idle = _wait_for_idle
    live.end_proxy_drain = MagicMock()
    shutdown = AsyncMock()

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("free", "active")),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "", "X-Instance-ID": "iid"}),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "key", "iid", public_url="https://paid.celerp.com",
            tier="cloud", status="active", connect_entitled=True,
            expected_api_key="key")

        live.begin_proxy_drain.assert_called_once()
        shutdown.assert_not_awaited()
        ensure_running.assert_not_called()

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if shutdown.await_count:
                break

    shutdown.assert_awaited_once()
    ensure_running.assert_called_once()


def test_ensure_running_does_not_supersede_response_safe_drain(monkeypatch):
    """A concurrent share/start request cannot cut off a draining claim response."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "gateway_token", "new-key")
    monkeypatch.setattr(settings, "cloud_disconnected", False)

    live = MagicMock()
    live.is_draining_for_reconfigure.return_value = True
    with (
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.client.GatewayClient") as constructor,
    ):
        from celerp.gateway import ensure_running
        ensure_running()

    constructor.assert_not_called()
    live.stop.assert_not_called()


@pytest.mark.asyncio
async def test_environment_only_activation_never_persists_local_config(monkeypatch):
    """Runtime convergence from an env-only credential performs no durable write."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "backup_enabled", False)
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(settings, "gateway_instance_id", "")
    monkeypatch.setattr(settings, "celerp_public_url", "")
    record = MagicMock()
    flags = {
        "payments_enabled": False,
        "external_db": False,
        "external_storage": False,
        "grace_period_ends": None,
    }
    with (
        patch("celerp.config.record_cloud_activation", new=record),
        patch("celerp.gateway.client.get_client", return_value=None),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.gateway.state.apply_feature_flags_async", new=AsyncMock()) as apply_flags,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "env-key", "iid", public_url=None,
            tier="free", status="active", connect_entitled=False,
            feature_flags=flags, persist_state=False)

    record.assert_not_called()
    apply_flags.assert_awaited_once_with(flags, persist=False)
    ensure_running.assert_not_called()
    assert settings.gateway_token == "env-key"
    assert settings.cloud_disconnected is False


@pytest.mark.asyncio
async def test_paid_to_free_without_share_applies_snapshot_then_stays_down(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "backup_enabled", False)
    live = MagicMock(relay_status="active")
    live.is_serving.return_value = True
    live.is_draining_for_reconfigure.return_value = False
    live.has_inflight_proxy_requests.return_value = False
    shutdown = AsyncMock()
    flags = {
        "payments_enabled": True,
        "external_db": False,
        "external_storage": False,
        "grace_period_ends": None,
    }

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "paid-session", "X-Instance-ID": "iid"}),
        patch("celerp.gateway.state.apply_feature_flags_async", new=AsyncMock()) as apply_flags,
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "key", "iid", public_url=None,
            tier="free", status="active", connect_entitled=False,
            feature_flags=flags, expected_api_key="key")

    apply_flags.assert_awaited_once_with(flags, persist=True)
    shutdown.assert_awaited_once()
    ensure_running.assert_not_called()
    assert settings.celerp_public_url == ""


@pytest.mark.asyncio
async def test_second_activation_reuses_existing_proxy_drain_owner(monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "backup_enabled", False)
    live = MagicMock(relay_status="active")
    live.is_serving.return_value = True
    live.is_draining_for_reconfigure.return_value = True
    shutdown = AsyncMock()

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "", "X-Instance-ID": "iid"}),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.shutdown", new=shutdown),
        patch("celerp.gateway.ensure_running") as ensure_running,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        from celerp.services.cloud_entitlement import apply_activation_state
        assert await apply_activation_state(
            "key", "iid", public_url="https://paid.celerp.com",
            tier="cloud", status="active", connect_entitled=True,
            expected_api_key="key")

    live.begin_proxy_drain.assert_not_called()
    shutdown.assert_not_awaited()
    ensure_running.assert_not_called()