# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression coverage for durable Connect identity and activation ordering."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from test_config import _reload_config, _restore_config_module  # noqa: F401


def test_authenticated_key_repairs_stale_persisted_identity(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {
        "token": "key-a",
        "instance_id": "stale-b",
        "activation_verifier": "proof-for-b",
    }})
    mod.settings.gateway_instance_id = "stale-b"
    mod.settings.activation_verifier = "proof-for-b"

    assert mod.adopt_authenticated_cloud_identity("key-a", "instance-a") is True

    cloud = mod.read_config()["cloud"]
    assert cloud["token"] == "key-a", "repair must never rotate a healthy credential"
    assert cloud["instance_id"] == "instance-a"
    assert "activation_verifier" not in cloud, "proof bound to stale id must be discarded"
    assert mod.settings.gateway_instance_id == "instance-a"
    assert mod.settings.activation_verifier == ""


def test_stale_authenticated_result_cannot_overwrite_newer_credential(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {"token": "new-key", "instance_id": "new-iid"}})

    assert mod.adopt_authenticated_cloud_identity("old-key", "old-iid") is False
    cloud = mod.read_config()["cloud"]
    assert cloud["token"] == "new-key"
    assert cloud["instance_id"] == "new-iid"


def test_authenticated_sync_preserves_pending_verifier(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {
        "token": "same-key", "instance_id": "iid",
        "activation_verifier": "pending-proof",
    }})

    assert mod.record_cloud_activation(
        "same-key", "iid", expected_api_key="same-key") is True

    cloud = mod.read_config()["cloud"]
    assert cloud["token"] == "same-key"
    assert cloud["activation_verifier"] == "pending-proof"


def test_verifier_activation_consumes_only_the_exact_proof(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {
        "instance_id": "iid", "activation_verifier": "new-proof"}})

    assert mod.record_cloud_activation(
        "should-not-win", "iid", expected_verifier="old-proof") is False
    assert mod.read_config()["cloud"]["activation_verifier"] == "new-proof"

    assert mod.record_cloud_activation(
        "new-key", "iid", expected_verifier="new-proof") is True
    cloud = mod.read_config()["cloud"]
    assert cloud["token"] == "new-key"
    assert "activation_verifier" not in cloud


def test_disconnect_wins_over_inflight_activation(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    mod.write_config({"cloud": {"token": "key", "instance_id": "iid"}})
    mod.set_cloud_disconnected(True)

    assert mod.record_cloud_activation(
        "key", "iid", public_url="https://should-not-apply.example",
        expected_api_key="key",
    ) is False
    cloud = mod.read_config()["cloud"]
    assert cloud["disconnected"] is True
    assert not cloud.get("public_url")


@pytest.mark.asyncio
async def test_fetch_relay_auth_returns_proven_identity_and_repairs_persisted_key():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "jwt-a", "instance_id": "instance-a"}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch(
        "celerp.config.adopt_authenticated_cloud_identity",
        return_value=True,
    ) as adopt:
        from celerp.gateway.state import fetch_relay_auth
        bearer, iid = await fetch_relay_auth(client, api_key="key-a")

    assert (bearer, iid) == ("jwt-a", "instance-a")
    adopt.assert_called_once_with("key-a", "instance-a")


@pytest.mark.asyncio
async def test_post_claim_activation_redeems_verifier_without_old_bearer():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "gateway_token": "new-key", "tier": "cloud", "status": "active"}
    seen = []

    class Client:
        async def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return response

    async def run(_timeout, operation):
        return await operation(Client())

    with (
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch("celerp.routers.health._apply_gateway_token_api",
              new=AsyncMock(return_value=True)) as apply_token,
    ):
        from celerp.routers.health import _activate_after_claim
        result = await _activate_after_claim(
            "current-iid", "https://relay.test", "current-verifier")

    assert result is not None
    assert len(seen) == 1
    url, kwargs = seen[0]
    assert url.endswith("/auth/activate")
    assert "Authorization" not in kwargs.get("headers", {})
    assert kwargs["json"]["instance_id"] == "current-iid"
    assert kwargs["json"]["activation_verifier"] == "current-verifier"
    apply_token.assert_awaited_once()
    assert apply_token.await_args.kwargs["expected_verifier"] == "current-verifier"


@pytest.mark.asyncio
async def test_apply_activation_state_keeps_healthy_serving_client(monkeypatch):
    """Refreshing a healthy connected instance must not disturb its live tunnel."""
    from celerp.config import settings
    from celerp.services.cloud_entitlement import apply_activation_state

    monkeypatch.setattr(settings, "gateway_token", "same-key")
    monkeypatch.setattr(settings, "gateway_instance_id", "same-iid")
    monkeypatch.setattr(settings, "celerp_public_url", "https://same.celerp.app")
    monkeypatch.setattr(settings, "backup_enabled", False)

    live = MagicMock()
    live.is_serving.return_value = True
    live.close = AsyncMock()
    live.relay_status = "active"

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.ensure_running"),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.client.set_client") as set_client,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        accepted = await apply_activation_state(
            "same-key", "same-iid",
            public_url="https://same.celerp.app",
            tier="cloud", status="active",
            expected_api_key="same-key",
        )

    assert accepted is True
    live.is_serving.assert_called_once_with("same-key")
    live.close.assert_not_awaited()
    set_client.assert_not_called()
    assert settings.gateway_token == "same-key"
    assert settings.gateway_instance_id == "same-iid"


@pytest.mark.asyncio
async def test_apply_activation_state_replaces_rejected_client(monkeypatch):
    """A rejected client is closed only after the durable activation apply wins."""
    from celerp.config import settings
    from celerp.services.cloud_entitlement import apply_activation_state

    monkeypatch.setattr(settings, "backup_enabled", False)
    dead = MagicMock()
    dead.is_serving.return_value = False
    dead.close = AsyncMock()

    with (
        patch("celerp.config.record_cloud_activation", return_value=True),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.ensure_running"),
        patch("celerp.gateway.client.get_client", return_value=dead),
        patch("celerp.gateway.client.set_client") as set_client,
        patch("celerp.services.backup_scheduler.stop"),
    ):
        accepted = await apply_activation_state(
            "fresh-key", "iid", public_url=None,
            expected_api_key="old-key",
        )

    assert accepted is True
    dead.close.assert_awaited_once()
    set_client.assert_called_once_with(None)
