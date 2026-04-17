# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for /settings/cloud-status and /settings/email-status HTMX fragments."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import celerp.gateway.state as gw_state


# ---------------------------------------------------------------------------
# /settings/email-status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_status_no_config(client):
    """Returns smtp_configured=False, gateway_connected=False when nothing configured."""
    with (
        patch("celerp.config.settings.smtp_host", ""),
        patch("celerp.config.settings.gateway_token", ""),
    ):
        r = await client.get("/settings/email-status")
    assert r.status_code == 200
    data = r.json()
    assert data["smtp_configured"] is False
    assert data["gateway_connected"] is False


@pytest.mark.asyncio
async def test_email_status_smtp_configured(client):
    """Returns smtp_configured=True when smtp_host is set."""
    with (
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.gateway_token", ""),
    ):
        r = await client.get("/settings/email-status")
    assert r.status_code == 200
    data = r.json()
    assert data["smtp_configured"] is True
    assert data["gateway_connected"] is False


@pytest.mark.asyncio
async def test_email_status_gateway_configured(client):
    """Returns gateway_connected=True when gateway_token is set."""
    with (
        patch("celerp.config.settings.smtp_host", ""),
        patch("celerp.config.settings.gateway_token", "mytoken"),
    ):
        r = await client.get("/settings/email-status")
    assert r.status_code == 200
    data = r.json()
    assert data["smtp_configured"] is False
    assert data["gateway_connected"] is True


@pytest.mark.asyncio
async def test_email_status_both_configured(client):
    """Both smtp_configured and gateway_connected can be True simultaneously."""
    with (
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.gateway_token", "mytoken"),
    ):
        r = await client.get("/settings/email-status")
    assert r.status_code == 200
    data = r.json()
    assert data["smtp_configured"] is True
    assert data["gateway_connected"] is True


# ---------------------------------------------------------------------------
# /settings/cloud-status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_status_not_connected(client):
    """Returns connected=False when gateway_token is empty."""
    with patch("celerp.config.settings.gateway_token", ""):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is False
    assert data["tier"] is None
    assert data["email_quota"] == 0


@pytest.mark.asyncio
async def test_cloud_status_connected_relay_unreachable(client):
    """Returns connected=True but defaults when relay API times out."""
    import httpx

    mock_inner_client = AsyncMock()
    mock_inner_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
    mock_inner_client.__aenter__ = AsyncMock(return_value=mock_inner_client)
    mock_inner_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("celerp.config.settings.gateway_token", "tok"),
        patch("celerp.config.settings.gateway_url", "wss://relay.celerp.com/ws/connect"),
        patch.object(gw_state, "_session_token", "sess"),
        patch("celerp.config.settings.gateway_instance_id", "inst-123"),
        patch("celerp.config.settings.gateway_http_url", ""),
        patch("httpx.AsyncClient", return_value=mock_inner_client),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] is None
    assert data["email_quota"] == 0
    assert data["email_used"] == 0


@pytest.mark.asyncio
async def test_cloud_status_connected_relay_ok(client):
    """Returns relay data when relay API responds successfully."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tier": "team",
        "last_backup": "2026-03-15T06:00:00Z",
        "email_quota": 1000,
        "email_used": 42,
    }

    captured_params = {}

    async def _mock_get(path, **kwargs):
        captured_params.update(kwargs.get("params", {}))
        return mock_response

    mock_inner_client = AsyncMock()
    mock_inner_client.get = _mock_get
    mock_inner_client.__aenter__ = AsyncMock(return_value=mock_inner_client)
    mock_inner_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("celerp.config.settings.gateway_token", "tok"),
        patch("celerp.config.settings.gateway_url", "wss://relay.celerp.com/ws/connect"),
        patch.object(gw_state, "_session_token", "sess-token"),
        patch("celerp.config.settings.gateway_instance_id", "inst-abc"),
        patch("celerp.config.settings.gateway_http_url", "https://relay.celerp.com"),
        patch("httpx.AsyncClient", return_value=mock_inner_client),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] == "team"
    assert data["email_quota"] == 1000
    assert data["email_used"] == 42
    assert data["last_backup"] == "2026-03-15T06:00:00Z"
    # Verify correct auth params are sent
    assert captured_params.get("instance_id") == "inst-abc"
    assert captured_params.get("session_token") == "sess-token"


# ---------------------------------------------------------------------------
# /settings/cloud — value-prop page (unconnected state)
# ---------------------------------------------------------------------------


def test_value_prop_messaging():
    """Value-prop page explains relay concept clearly and shows email claim form."""
    from ui.routes.settings_cloud import _value_prop_page
    from fasthtml.common import to_xml

    html = to_xml(_value_prop_page("test-iid"))

    # Key messaging: data stays on your machine
    assert "your data stays there" in html.lower() or "data stays" in html.lower()
    assert "relay the connection" in html.lower()
    assert "yourname.celerp.com" in html

    # Email claim form is always visible (not gated behind a failed activate)
    assert 'name="claim_email"' in html
    assert "cloud-send-otp" in html
    assert "Link subscription" in html

    # Auto-connect button present
    assert "cloud-connect-btn" in html
    assert "cloud-activate" in html

    # Plan cards present
    assert "$29" in html
    assert "$49" in html
    assert "$99" in html


def test_value_prop_no_cloud_service_language():
    """Value-prop page should NOT use language that implies we host user data."""
    from ui.routes.settings_cloud import _value_prop_page
    from fasthtml.common import to_xml
    import re

    html = to_xml(_value_prop_page("test-iid"))
    text = re.sub(r"<[^>]+>", " ", html).lower()

    # Should not say things like "migrate to cloud" or "cloud hosting"
    assert "migrate to" not in text
    assert "cloud hosting" not in text
    assert "we store your data" not in text


# ---------------------------------------------------------------------------
# Forgot-password context-aware page
# ---------------------------------------------------------------------------

def test_forgot_password_cli_page_no_email_transport():
    """When no email transport is configured, forgot-password shows CLI instructions."""
    from ui.routes.auth import _forgot_password_cli
    from fasthtml.common import to_xml
    html = to_xml(_forgot_password_cli())
    assert "celerp reset-password" in html
    assert "Want email-based password resets?" in html
    assert "$29/mo" in html
    assert "Back to login" in html


def test_forgot_password_cli_page_has_subscribe_link():
    """CLI forgot-password page includes subscribe link with instance_id."""
    from ui.routes.auth import _forgot_password_cli
    from fasthtml.common import to_xml
    html = to_xml(_forgot_password_cli())
    assert "celerp.com/subscribe?instance_id=" in html


def test_forgot_password_email_form_exists():
    """The email-based forgot-password form still exists for cloud users."""
    from ui.routes.auth import _forgot_password_form
    from fasthtml.common import to_xml
    html = to_xml(_forgot_password_form())
    assert 'action="/forgot-password"' in html
    assert "Send reset link" in html
