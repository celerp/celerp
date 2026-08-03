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
    assert data["email_resets_on"] is None


@pytest.mark.asyncio
async def test_cloud_status_reports_disconnected_flag(client):
    """cloud-status carries cloud_disconnected so the UI can withhold auto-connect
    on a sticky-disconnected install."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.cloud_disconnected", True),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    assert r.json()["cloud_disconnected"] is True


def test_unconnected_fragment_suppresses_autoconnect_when_disconnected():
    """The disconnect response must not carry the on-load auto-connect script, or
    it would instantly re-fire Connect and undo the disconnect; the default
    (fresh/subscribed install) still auto-connects."""
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_unconnected
    on = to_xml(_cloud_relay_unconnected("iid-1"))
    off = to_xml(_cloud_relay_unconnected("iid-1", suppress_autoconnect=True))
    assert "cloud_activate_tried" in on
    assert "cloud_activate_tried" not in off
    # The Connect button itself stays either way (one-click reconnect).
    assert "cloud-connect-btn" in off


def test_claim_panel_suppresses_autoconnect_when_disconnected():
    from fasthtml.common import to_xml
    from ui.routes.account import account_panel
    on = to_xml(account_panel("en", intent="claim", panel_id="cloud-relay-tab"))
    off = to_xml(account_panel("en", intent="claim", panel_id="cloud-relay-tab",
                               suppress_autoconnect=True))
    assert "cloud_activate_tried" in on
    assert "cloud_activate_tried" not in off
    assert "cloud-connect-btn" in off


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
        patch.object(gw_state, "_subscription_tier", ""),
        patch("celerp.config.settings.gateway_instance_id", "inst-123"),
        patch("celerp.config.settings.gateway_http_url", ""),
        patch("httpx.AsyncClient", return_value=mock_inner_client),
        patch("celerp.gateway.client.get_client", return_value=MagicMock(relay_status="active")),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] is None
    assert data["email_quota"] == 0
    assert data["email_used"] == 0


@pytest.mark.asyncio
async def test_cloud_status_uses_ws_pushed_tier_when_relay_unreachable(client):
    """The gateway's own WS push (subscription_updated) already told us the tier;
    a free instance's session_token may never resolve a live /billing/status call,
    so tier must not silently fall back to None when the WS already supplied it
    (regression: this used to hide the free-tier note whenever the HTTP round trip
    to the relay failed or session_token wasn't set)."""
    with (
        patch("celerp.config.settings.gateway_token", "tok"),
        patch("celerp.config.settings.gateway_url", "wss://relay.celerp.com/ws/connect"),
        patch.object(gw_state, "_session_token", ""),
        patch.object(gw_state, "_subscription_tier", "free"),
        patch.object(gw_state, "_subscription_status", "active"),
        patch("celerp.gateway.client.get_client", return_value=MagicMock(relay_status="active")),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] == "free"


@pytest.mark.asyncio
async def test_cloud_status_relay_http_tier_overrides_stale_ws_tier(client):
    """The relay's live /billing/status answer (e.g. after an upgrade) still wins
    over a WS-pushed tier that may be stale until the next subscription_updated push."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"tier": "team", "email_quota": 1000, "email_used": 42}

    mock_inner_client = AsyncMock()
    mock_inner_client.get = AsyncMock(return_value=mock_response)
    mock_inner_client.__aenter__ = AsyncMock(return_value=mock_inner_client)
    mock_inner_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("celerp.config.settings.gateway_token", "tok"),
        patch("celerp.config.settings.gateway_url", "wss://relay.celerp.com/ws/connect"),
        patch("celerp.gateway.state.get_session_token", return_value="sess-token"),
        patch.object(gw_state, "_subscription_tier", "free"),
        patch("celerp.config.settings.gateway_instance_id", "inst-abc"),
        patch("celerp.config.settings.gateway_http_url", "https://relay.celerp.com"),
        patch("httpx.AsyncClient", return_value=mock_inner_client),
        patch("celerp.gateway.client.get_client", return_value=MagicMock(relay_status="active")),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    assert r.json()["tier"] == "team"


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
        "email_resets_on": "2026-08-01",
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
        patch("celerp.gateway.state.get_session_token", return_value="sess-token"),
        patch("celerp.config.settings.gateway_instance_id", "inst-abc"),
        patch("celerp.config.settings.gateway_http_url", "https://relay.celerp.com"),
        patch("httpx.AsyncClient", return_value=mock_inner_client),
        patch("celerp.gateway.client.get_client", return_value=MagicMock(relay_status="active")),
    ):
        r = await client.get("/settings/cloud-status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] == "team"
    assert data["email_quota"] == 1000
    assert data["email_used"] == 42
    assert data["email_resets_on"] == "2026-08-01"
    assert data["last_backup"] == "2026-03-15T06:00:00Z"
    # Verify correct auth params are sent
    assert captured_params.get("instance_id") == "inst-abc"
    assert captured_params.get("session_token") == "sess-token"


# ---------------------------------------------------------------------------
# /settings/cloud/billing-portal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_billing_portal_returns_relay_url(client):
    """Proxies the relay's Stripe Billing Portal session URL to the UI."""
    with patch("celerp.services.payments.billing_portal_url",
               AsyncMock(return_value="https://billing.stripe.com/p/session_x")):
        r = await client.post("/settings/cloud/billing-portal")
    assert r.status_code == 200
    assert r.json()["portal_url"] == "https://billing.stripe.com/p/session_x"


@pytest.mark.asyncio
async def test_billing_portal_unavailable_is_an_error(client):
    """Relay unreachable or no billing account: an explanatory 502, never a
    fabricated URL."""
    with patch("celerp.services.payments.billing_portal_url", AsyncMock(return_value=None)):
        r = await client.post("/settings/cloud/billing-portal")
    assert r.status_code == 502
    assert "subscription management" in r.json()["detail"].lower()


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


# The forgot-password no-email path is now a persistent CLI-instruction toast on the
# login screen (not a full CLI page). It is covered in tests/test_ui.py
# ::test_forgot_password_no_email_toasts_cli_instruction.


def test_forgot_password_email_form_exists():
    """The email-based forgot-password form still exists for cloud users."""
    from ui.routes.auth import _forgot_password_form
    from fasthtml.common import to_xml
    html = to_xml(_forgot_password_form())
    assert 'action="/forgot-password"' in html
    assert "Send reset link" in html


# ── Celerp Connect tab: account view only after authentication ───────────────

def _relay_tab_html(relay_status, token_bound):
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    with patch("celerp.gateway.client.get_client", return_value=None):
        return to_xml(_cloud_relay_tab(relay_status=relay_status, public_url="",
                                       tier="free", token_bound=token_bound))


def test_cloud_tab_connecting_hides_account_view():
    """While the relay has not yet accepted the token, the tab shows only the
    connection state - no tier benefits, no subscription link, no disconnect."""
    html = _relay_tab_html("connecting", token_bound=True)
    assert "Establishing connection" in html
    assert "Link subscription" not in html
    assert "cloud-disconnect" not in html


def test_cloud_tab_error_shows_recovery_only():
    """A failed connection shows the failure and the disconnect recovery path,
    never the account view."""
    html = _relay_tab_html("error", token_bound=True)
    assert "Connection failed" in html
    assert "cloud-disconnect" in html
    assert "Link subscription" not in html


def test_cloud_tab_signed_in_free_shows_account_view():
    """A signed-in free instance (token held, no live tunnel) keeps the account
    view: tier note, subscription link, disconnect."""
    html = _relay_tab_html("inactive", token_bound=True)
    assert "Link subscription" in html
    assert "cloud-disconnect" in html


def test_cloud_tab_active_shows_account_view():
    """An authenticated connection renders the full account view."""
    html = _relay_tab_html("active", token_bound=True)
    assert "Link subscription" in html
    assert "cloud-disconnect" in html
