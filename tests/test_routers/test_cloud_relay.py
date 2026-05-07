# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for cloud relay API endpoints: activate, disconnect, accept-tos, apply-token.

All relay HTTP calls are mocked so tests run offline.
Gateway client is mocked so no WS connections are made.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, suffix: str = "") -> str:
    addr = f"cloud-{suffix or uuid.uuid4().hex[:8]}@test.local"
    r = await client.post(
        "/auth/register",
        json={"company_name": "CloudCo", "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_gw(relay_status: str = "active") -> MagicMock:
    gw = MagicMock()
    gw.relay_status = relay_status
    gw.required_tos_version = ""
    gw.stop = MagicMock()
    gw.run = AsyncMock()
    return gw


# ---------------------------------------------------------------------------
# /settings/cloud-status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_status_disconnected(client):
    """Returns connected=False when no gateway client is running."""
    token = await _register(client, "status-off")
    with patch("celerp.gateway.client.get_client", return_value=None):
        r = await client.get("/settings/cloud-status", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is False
    assert data["relay_status"] == "inactive"
    assert "public_url" in data


@pytest.mark.asyncio
async def test_cloud_status_connected(client):
    """Returns connected=True when gateway client is active."""
    token = await _register(client, "status-on")
    gw = _mock_gw("active")
    with (
        patch("celerp.gateway.client.get_client", return_value=gw),
        patch("celerp.gateway.state.get_session_token", return_value="tok"),
    ):
        r = await client.get("/settings/cloud-status", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["relay_status"] == "active"


# ---------------------------------------------------------------------------
# /settings/cloud-disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_disconnect_stops_client_preserves_token(client):
    """Disconnect stops gw client but preserves gateway_token in settings."""
    token = await _register(client, "disc")
    gw = _mock_gw("active")

    from celerp.config import settings as _s
    _s.gateway_token = "old-token"
    _s.celerp_public_url = "https://test.celerp.app"

    with (
        patch("celerp.gateway.client.get_client", return_value=gw),
        patch("celerp.gateway.client.set_client") as mock_set,
    ):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))

    assert r.status_code == 200
    assert r.json()["disconnected"] is True
    gw.stop.assert_called_once()
    mock_set.assert_called_once_with(None)
    # Token and URL must NOT be cleared
    assert _s.gateway_token == "old-token"
    assert _s.celerp_public_url == "https://test.celerp.app"


@pytest.mark.asyncio
async def test_cloud_disconnect_no_op_when_already_disconnected(client):
    """Disconnect with no active client returns success without error."""
    token = await _register(client, "disc-noop")
    with patch("celerp.gateway.client.get_client", return_value=None):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["disconnected"] is True


# ---------------------------------------------------------------------------
# /settings/cloud-reconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_reconnect_restarts_client_with_existing_token(client):
    """Reconnect re-starts gateway WS using existing token without re-claiming."""
    token = await _register(client, "reconnect-ok")
    gw = _mock_gw("active")

    from celerp.config import settings as _s
    _s.gateway_token = "saved-token"
    _s.gateway_instance_id = "test-iid"

    with (
        patch("celerp.gateway.client.get_client", return_value=None),
        patch("celerp.gateway.client.set_client"),
        patch("celerp.gateway.client.GatewayClient", return_value=gw),
        patch("asyncio.create_task"),
    ):
        r = await client.post("/settings/cloud-reconnect", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["relay_status"] == "active"


@pytest.mark.asyncio
async def test_cloud_reconnect_error_when_no_token(client):
    """Reconnect returns error when no token in config."""
    token = await _register(client, "reconnect-notoken")
    from celerp.config import settings as _s
    _s.gateway_token = ""
    with patch("celerp.gateway.client.get_client", return_value=None):
        r = await client.post("/settings/cloud-reconnect", headers=_h(token))
    assert r.status_code == 200
    assert "error" in r.json()


# ---------------------------------------------------------------------------
# /settings/cloud-activate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_activate_success(client):
    """Activate returns connected=True when relay returns 200 with a token."""
    token = await _register(client, "act-ok")

    relay_response = {
        "gateway_token": "gw-abc123",
        "public_url": "https://myco.celerp.app",
        "tos_version": "2025-01",
        "reconnect": False,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = relay_response

    gw = _mock_gw("active")

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.gateway.client.get_client", return_value=None),
        patch("celerp.gateway.client.set_client"),
        patch("celerp.gateway.client.GatewayClient", return_value=gw),
        patch("celerp.config.write_config"),
        patch("celerp.config.read_config", return_value={"cloud": {}}),
        patch("asyncio.create_task"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert "relay_status" in data
    assert data.get("public_url") == "https://myco.celerp.app"


@pytest.mark.asyncio
async def test_cloud_activate_404_returns_error_with_instance_id(client):
    """Activate returns error dict with instance_id when relay returns 404."""
    token = await _register(client, "act-404")

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"detail": "Instance not registered."}

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "instance_id" in data
    assert "Subscribe" in data["error"] or "subscription" in data["error"].lower()


@pytest.mark.asyncio
async def test_cloud_activate_reconnect_flow(client):
    """Activate returns reconnect=True payload when relay signals reconnect."""
    token = await _register(client, "act-reconnect")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "gateway_token": "gw-reconnect",
        "public_url": "https://old.celerp.app",
        "tos_version": "2025-01",
        "reconnect": True,
    }

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert data["reconnect"] is True
    assert data["gateway_token"] == "gw-reconnect"
    assert "instance_id" in data


@pytest.mark.asyncio
async def test_cloud_activate_relay_unreachable(client):
    """Activate returns error when relay is unreachable."""
    import httpx
    token = await _register(client, "act-nonet")

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("refused")
        )
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "internet" in data["error"].lower() or "firewall" in data["error"].lower()


# ---------------------------------------------------------------------------
# /settings/cloud-apply-token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_apply_token_success(client):
    """Apply-token applies the given gateway token and returns connected status."""
    token = await _register(client, "apply-ok")

    gw = _mock_gw("active")

    with (
        patch("celerp.gateway.client.get_client", return_value=None),
        patch("celerp.gateway.client.set_client"),
        patch("celerp.gateway.client.GatewayClient", return_value=gw),
        patch("celerp.config.write_config"),
        patch("celerp.config.read_config", return_value={"cloud": {}}),
        patch("asyncio.create_task"),
    ):
        r = await client.post(
            "/settings/cloud-apply-token",
            headers=_h(token),
            json={"gateway_token": "gw-xyz", "public_url": "https://co.celerp.app"},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True


@pytest.mark.asyncio
async def test_cloud_apply_token_missing_token(client):
    """Apply-token returns error when gateway_token is missing."""
    token = await _register(client, "apply-empty")
    r = await client.post("/settings/cloud-apply-token", headers=_h(token), json={})
    assert r.status_code == 200
    assert "error" in r.json()


# ---------------------------------------------------------------------------
# /settings/cloud-accept-tos
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_accept_tos_restarts_client(client):
    """Accept-TOS stops existing client, persists tos_version, starts new client."""
    token = await _register(client, "tos-ok")

    old_gw = _mock_gw("tos_required")
    old_gw.required_tos_version = "2025-02"
    new_gw = _mock_gw("active")

    with (
        patch("celerp.gateway.client.get_client", return_value=old_gw),
        patch("celerp.gateway.client.set_client"),
        patch("celerp.gateway.client.GatewayClient", return_value=new_gw),
        patch("celerp.config.write_config") as mock_write,
        patch("celerp.config.read_config", return_value={"cloud": {}}),
        patch("asyncio.create_task"),
    ):
        r = await client.post("/settings/cloud-accept-tos", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "relay_status" in data
    old_gw.stop.assert_called_once()
    # tos_version should have been written to config
    mock_write.assert_called_once()
