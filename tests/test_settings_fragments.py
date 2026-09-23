# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for /settings/cloud-status HTMX fragment and email-status removal.

The email-status endpoint and fragment were removed project-wide (item 4b).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import celerp.gateway.state as gw_state


@pytest.fixture
async def owner_h(client) -> dict:
    """A bootstrap owner's bearer header. /settings/* now requires an
    authenticated user, and the owner holds manage_integrations by default."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "FragCo", "email": "frag@example.com", "name": "Admin", "password": "pwvalid1"},
    )
    assert reg.status_code == 200, reg.text
    return {"Authorization": f"Bearer {reg.json()['access_token']}"}


# ---------------------------------------------------------------------------
# /settings/email-status removal verification (item 4b)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_status_backend_route_removed(client):
    """GET /settings/email-status (backend) returns 404 after removal.

    Red statement: The route exists and returns 200 (health.py:174).
    """
    r = await client.get("/settings/email-status")
    assert r.status_code == 404, (
        f"Expected 404 for removed email-status route, got {r.status_code}"
    )


def test_email_locale_key_removed():
    """settings._email_notifications_are_disabled is absent from every locale file."""
    locale_dir = Path(__file__).parent.parent / "ui" / "locales"
    for locale_file in locale_dir.glob("*.json"):
        data = json.loads(locale_file.read_text())
        settings_keys = data.get("settings", {})
        assert "_email_notifications_are_disabled" not in settings_keys, (
            f"Locale key settings._email_notifications_are_disabled still present in {locale_file.name}"
        )


# ---------------------------------------------------------------------------
# /settings/cloud-status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_status_not_connected(client, owner_h):
    """Returns connected=False when gateway_token is empty."""
    with patch("celerp.config.settings.gateway_token", ""):
        r = await client.get("/settings/cloud-status", headers=owner_h)
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is False
    assert data["tier"] is None
    assert data["email_quota"] == 0
    assert data["email_resets_on"] is None


@pytest.mark.asyncio
async def test_cloud_status_reports_disconnected_flag(client, owner_h):
    """cloud-status carries cloud_disconnected so the UI can withhold auto-connect
    on a sticky-disconnected install."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.cloud_disconnected", True),
    ):
        r = await client.get("/settings/cloud-status", headers=owner_h)
    assert r.status_code == 200
    assert r.json()["cloud_disconnected"] is True


def test_connect_panels_never_auto_submit_and_single_flight_mutations():
    """Opening Connect UI is read-only; explicit controls are single-flight."""
    from fasthtml.common import to_xml
    from ui.routes.account import account_panel
    from ui.routes.settings import _cloud_relay_unconnected

    for html in (
        to_xml(_cloud_relay_unconnected("iid-1")),
        to_xml(account_panel("en", intent="claim", panel_id="cloud-relay-tab")),
    ):
        assert "cloud_activate_tried" not in html
        assert "sessionStorage" not in html
        assert "cloud-connect-btn" in html
        assert 'hx-disabled-elt="this"' in html
        assert 'hx-sync="#cloud-relay-tab:drop"' in html



@pytest.mark.asyncio
async def test_cloud_status_connected_relay_unreachable(client, owner_h):
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
        r = await client.get("/settings/cloud-status", headers=owner_h)
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] is None
    assert data["email_quota"] == 0
    assert data["email_used"] == 0


@pytest.mark.asyncio
async def test_cloud_status_uses_ws_pushed_tier_when_relay_unreachable(client, owner_h):
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
        r = await client.get("/settings/cloud-status", headers=owner_h)
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] == "free"



@pytest.mark.asyncio
async def test_cloud_status_relay_http_tier_overrides_stale_ws_tier(client, owner_h):
    """The durable relay answer wins over stale runtime subscription state."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tier": "team", "status": "active",
        "email_quota": 1000, "email_used": 42,
    }
    billing = AsyncMock(return_value=mock_response)
    sync = AsyncMock(return_value={
        "tier": "team", "status": "active", "connect_entitled": True,
    })

    with (
        patch("celerp.config.settings.gateway_token", "tok"),
        patch("celerp.config.settings.gateway_url", "wss://relay.celerp.com/ws/connect"),
        patch.object(gw_state, "_subscription_tier", "free"),
        patch.object(gw_state, "_subscription_status", "active"),
        patch(
            "celerp.services.cloud_entitlement.subscription_status",
            new=AsyncMock(return_value={
                "tier": "team", "status": "active", "connect_entitled": True,
            }),
        ),
        patch(
            "celerp.services.cloud_entitlement.sync_existing_entitlement",
            new=sync,
        ),
        patch(
            "celerp.services.cloud_entitlement.authenticated_request",
            new=billing,
        ),
        patch("celerp.gateway.client.get_client", return_value=MagicMock(relay_status="active")),
    ):
        r = await client.get("/settings/cloud-status", headers=owner_h)

    assert r.status_code == 200
    assert r.json()["tier"] == "team"
    sync.assert_awaited_once_with(require_persisted_key=True)
    billing.assert_awaited_once_with("GET", "/billing/status", total_s=3.0)


@pytest.mark.asyncio
async def test_cloud_status_connected_relay_ok(client, owner_h):
    """Returns durable relay data when the authenticated billing read succeeds."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tier": "team",
        "status": "active",
        "last_backup": "2026-03-15T06:00:00Z",
        "email_quota": 1000,
        "email_used": 42,
        "email_resets_on": "2026-08-01",
    }
    billing = AsyncMock(return_value=mock_response)

    with (
        patch("celerp.config.settings.gateway_token", "tok"),
        patch("celerp.config.settings.gateway_url", "wss://relay.celerp.com/ws/connect"),
        patch(
            "celerp.services.cloud_entitlement.subscription_status",
            new=AsyncMock(return_value={
                "tier": "team", "status": "active", "connect_entitled": True,
            }),
        ),
        patch(
            "celerp.services.cloud_entitlement.authenticated_request",
            new=billing,
        ),
        patch("celerp.gateway.client.get_client", return_value=MagicMock(relay_status="active")),
    ):
        r = await client.get("/settings/cloud-status", headers=owner_h)

    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["tier"] == "team"
    assert data["email_quota"] == 1000
    assert data["email_used"] == 42
    assert data["email_resets_on"] == "2026-08-01"
    assert data["last_backup"] == "2026-03-15T06:00:00Z"
    billing.assert_awaited_once_with("GET", "/billing/status", total_s=3.0)

@pytest.mark.asyncio
async def test_billing_portal_returns_relay_url(client, owner_h):
    """Proxies the relay's Stripe Billing Portal session URL to the UI."""
    with patch("celerp.services.payments.billing_portal_url",
               AsyncMock(return_value="https://billing.stripe.com/p/session_x")):
        r = await client.post("/settings/cloud/billing-portal", headers=owner_h)
    assert r.status_code == 200
    assert r.json()["portal_url"] == "https://billing.stripe.com/p/session_x"


@pytest.mark.asyncio
async def test_billing_portal_unavailable_is_an_error(client, owner_h):
    """Relay unreachable or no billing account: an explanatory 502, never a
    fabricated URL."""
    with patch("celerp.services.payments.billing_portal_url", AsyncMock(return_value=None)):
        r = await client.post("/settings/cloud/billing-portal", headers=owner_h)
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

    # Plan cards are present, but prices are omitted until the live catalog is supplied.
    assert "cloud-plans" in html
    assert "$29" not in html
    assert "$49" not in html
    assert "$99" not in html          # Team card is not self-service sold


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

def _relay_tab_html(relay_status, token_bound, *, tier="free", entitled=False):
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    with patch("celerp.gateway.client.get_client", return_value=None):
        return to_xml(_cloud_relay_tab(
            relay_status=relay_status, public_url="",
            tier=tier, token_bound=token_bound, entitlement_known=True,
            entitled=entitled))


def test_cloud_tab_connecting_hides_account_view():
    """While the relay has not yet accepted the token, the tab shows only the
    connection state - no tier benefits, no subscription link, no disconnect."""
    html = _relay_tab_html("connecting", token_bound=True)
    assert "Establishing connection" in html
    assert "Link subscription" not in html
    assert "cloud-disconnect" not in html


def test_cloud_tab_error_shows_recovery_only():
    """A failed known connection keeps its status plus retry and disconnect."""
    html = _relay_tab_html("error", token_bound=True, tier="cloud", entitled=True)
    assert "Connection failed" in html
    assert "cloud-connect-btn" in html
    assert "cloud-disconnect" in html
    assert "Link subscription" not in html


@pytest.mark.parametrize("token_bound", [False, True])
def test_cloud_tab_error_unknown_entitlement_offers_link_recovery(token_bound):
    """Rejected/unverifiable credentials never terminate without recovery."""
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status="error", public_url="", tier="",
        token_bound=token_bound, entitlement_known=False))
    assert "Connection failed" in html
    assert "cloud-connect-btn" in html
    assert "Link subscription" in html
    assert ("cloud-disconnect" in html) is token_bound


def test_cloud_tab_connecting_unknown_entitlement_keeps_polling():
    """A separate entitlement-read outage must not override transient transport."""
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status="connecting", public_url="", tier="",
        token_bound=True, entitlement_known=False))
    assert "Establishing connection" in html
    assert 'hx-get="/settings/cloud-relay-tab"' in html
    assert "could not be connected" not in html
    assert "Link subscription" not in html


def test_cloud_tab_connecting_polls_for_outcome():
    """The connecting card refreshes itself so the outcome (account view or the
    failure card) appears without a manual page reload; terminal states render
    without the polling wiring, which stops the refresh cycle."""
    html = _relay_tab_html("connecting", token_bound=True)
    assert 'hx-get="/settings/cloud-relay-tab"' in html
    assert 'hx-trigger="every 2s"' in html
    for terminal in ("error", "active"):
        html = _relay_tab_html(terminal, token_bound=True)
        assert "/settings/cloud-relay-tab" not in html


def test_cloud_tab_signed_in_free_shows_account_view():
    """A signed-in free instance (token held, no live tunnel) keeps the account
    view: tier note, subscription link, disconnect."""
    html = _relay_tab_html("inactive", token_bound=True)
    assert "Link subscription" in html
    assert "cloud-disconnect" in html
    assert "Initializing connection" not in html


def test_cloud_tab_unknown_entitlement_is_never_rendered_as_free():
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status="inactive", public_url="", tier="",
        token_bound=True, entitlement_known=False))
    assert "Free account" not in html
    assert "could not be connected" in html
    assert "cloud-connect-btn" in html
    assert "Link subscription" in html


def test_cloud_tab_inactive_paid_offers_reconnect():
    """Known paid entitlement that failed to self-heal is never Disconnect-only."""
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status="inactive", public_url="", tier="cloud",
        token_bound=True, entitlement_known=True, entitled=True))
    assert "Initializing connection" in html
    assert "cloud-connect-btn" in html
    assert "Link subscription" not in html
    assert "cloud-disconnect" in html


def test_cloud_tab_active_unknown_entitlement_keeps_authenticated_view():
    """An accepted transport remains authoritative during a billing-read outage."""
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status="active", public_url="https://demo.celerp.com", tier="",
        token_bound=True, entitlement_known=False))
    assert ">Active<" in html
    assert "cloud-disconnect" in html
    assert "could not be connected" not in html


def test_cloud_tab_active_shows_account_view():
    """An authenticated connection renders the full account view."""
    html = _relay_tab_html("active", token_bound=True)
    assert "Link subscription" in html
    assert "cloud-disconnect" in html


@pytest.mark.parametrize("relay_status", ["inactive", "active", "error"])
def test_cloud_tab_lapsed_paid_always_offers_subscription_recovery(relay_status):
    """Authoritative but non-entitled paid state must never be a Disconnect-only dead end."""
    from fasthtml.common import to_xml
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status=relay_status, public_url="", tier="cloud",
        token_bound=True, entitlement_known=True, entitled=False))
    assert "Link subscription" in html
    assert "cloud-disconnect" in html
    if relay_status in ("inactive", "error"):
        assert "cloud-connect-btn" in html
    if relay_status == "inactive":
        assert "Initializing connection" not in html


@pytest.mark.asyncio
async def test_relay_state_preserves_authoritative_entitled_false():
    """Do not collapse a lapsed paid account into merely 'entitlement known'."""
    from ui.routes.settings_cloud import _relay_state
    payload = {
        "relay_status": "inactive",
        "public_url": "",
        "tier": "cloud",
        "cloud_disconnected": False,
        "gateway_token_set": True,
        "entitlement_known": True,
        "entitled": False,
    }
    with patch("ui.api_client.get_relay_status", new=AsyncMock(return_value=payload)):
        state = await _relay_state("token")
    assert state == ("inactive", "", "cloud", False, True, True, False)


@pytest.mark.parametrize("status,expected", [
    ({"entitlement_known": True, "entitled": True}, True),
    ({"entitlement_known": True, "entitled": False,
      "connected": True, "gateway_token_set": True}, False),
    ({"entitlement_known": False, "connected": True}, True),
    ({"entitlement_known": False, "gateway_token_set": True}, True),
    ({"entitlement_known": False}, False),
])
def test_relay_paid_access_has_one_status_interpretation(status, expected):
    from ui.routes.settings_cloud import _relay_has_paid_access
    assert _relay_has_paid_access(status) is expected


def test_paid_access_callers_reuse_shared_status_interpretation():
    from pathlib import Path
    payments = Path("ui/routes/settings_payments.py").read_text()
    inventory = Path("ui/routes/settings_inventory.py").read_text()
    for source in (payments, inventory):
        assert "_relay_has_paid_access(" in source
        assert 'get("entitlement_known")' not in source
