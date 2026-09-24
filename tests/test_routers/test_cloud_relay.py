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

from celerp.gateway.state import (
    RELAY_CONNECT_ATTEMPTS, RELAY_CONNECT_TIMEOUT_S, relay_timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, suffix: str = "") -> str:
    addr = f"cloud-{suffix or uuid.uuid4().hex[:8]}@test.local"
    r = await client.post(
        "/auth/register",
        json={"company_name": "CloudCo", "email": addr, "name": "Admin", "password": "pwvalid1"},
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
    gw.close = AsyncMock()
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




@pytest.mark.asyncio
async def test_cloud_status_active_identity_mismatch_uses_canonical_session_identity(
        client):
    token = await _register(client, "status-identity")
    gw = _mock_gw("active")

    from celerp.config import settings as _s
    _s.cloud_disconnected = False
    _s.gateway_instance_id = "persisted-iid"
    _s.celerp_public_url = "https://slug.celerp.com"

    live = MagicMock(status_code=200)
    live.json.return_value = {
        "tier": "ai", "status": "trialing", "last_backup": None,
        "email_quota": 10, "email_used": 1,
    }
    billing = AsyncMock(return_value=live)
    sync = AsyncMock(return_value={
        "tier": "ai", "status": "trialing", "connect_entitled": True
    })
    with (
        patch("celerp.gateway.client.get_client", return_value=gw),
        patch(
            "celerp.services.cloud_entitlement.subscription_status",
            new=AsyncMock(return_value={
                "tier": "ai", "status": "trialing", "connect_entitled": True
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
        patch("celerp.gateway.state.get_instance_id", return_value="canonical-iid"),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "session-a", "X-Instance-ID": "canonical-iid"
        }),
    ):
        r = await client.get("/settings/cloud-status", headers=_h(token))

    assert r.status_code == 200
    sync.assert_awaited_once_with(require_persisted_key=True)
    billing.assert_awaited_once_with("GET", "/billing/status", total_s=3.0)

@pytest.mark.asyncio
async def test_cloud_status_authenticated_plain_free_runtime_retries_stale_url_cleanup(
        client):
    """A free hello_ack retries durable cleanup after a transient boot-sync failure."""
    token = await _register(client, "status-plain-free")
    gw = _mock_gw("active")

    from celerp.config import settings as _s
    _s.cloud_disconnected = False
    _s.gateway_instance_id = "canonical-iid"
    _s.celerp_public_url = "https://stale.celerp.com"

    sync = AsyncMock(return_value={"tier": "free", "status": "active"})
    with (
        patch("celerp.gateway.client.get_client", return_value=gw),
        patch("celerp.services.cloud_entitlement.subscription_status",
              new=AsyncMock(return_value=None)),
        patch("celerp.services.cloud_entitlement.sync_existing_entitlement",
              new=sync),
        patch("celerp.gateway.state.get_instance_id", return_value="canonical-iid"),
        patch("celerp.gateway.state.get_subscription_state",
              return_value=("free", "active")),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "", "X-Instance-ID": "canonical-iid"}),
    ):
        r = await client.get("/settings/cloud-status", headers=_h(token))

    assert r.status_code == 200
    sync.assert_awaited_once_with(require_persisted_key=True)




@pytest.mark.asyncio
async def test_cloud_status_active_free_runtime_recovers_paid_entitlement(client):
    token = await _register(client, "status-paid-runtime")
    gw = _mock_gw("active")

    from celerp.config import settings as _s
    _s.cloud_disconnected = False
    _s.gateway_instance_id = "canonical-iid"
    _s.celerp_public_url = ""

    sync = AsyncMock(return_value={
        "tier": "ai", "status": "trialing", "connect_entitled": True
    })
    live = MagicMock(status_code=200)
    live.json.return_value = {
        "tier": "ai", "status": "trialing",
        "email_quota": 10, "email_used": 1,
    }
    billing = AsyncMock(return_value=live)

    with (
        patch("celerp.gateway.client.get_client", return_value=gw),
        patch(
            "celerp.services.cloud_entitlement.subscription_status",
            new=AsyncMock(return_value={
                "tier": "ai", "status": "trialing", "connect_entitled": True
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
        patch("celerp.gateway.state.get_instance_id", return_value="canonical-iid"),
        patch("celerp.gateway.state.relay_session_headers", return_value={
            "X-Session-Token": "", "X-Instance-ID": "canonical-iid"
        }),
    ):
        r = await client.get("/settings/cloud-status", headers=_h(token))

    assert r.status_code == 200
    sync.assert_awaited_once_with(require_persisted_key=True)
    billing.assert_awaited_once_with("GET", "/billing/status", total_s=3.0)

@pytest.mark.asyncio
async def test_cloud_disconnect_stops_client_and_clears_live_token(client):
    """Disconnect persists first and delegates transport teardown."""
    token = await _register(client, "disc")

    from celerp.config import settings as _s
    _s.gateway_token = "old-token"
    _s.celerp_public_url = "https://test.celerp.app"

    with (
        patch("celerp.config.set_cloud_disconnected") as persist,
        patch(
            "celerp.services.cloud_entitlement.reconfigure_gateway_runtime",
            new=AsyncMock(return_value=False),
        ) as reconfigure,
    ):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))

    assert r.status_code == 200
    assert r.json()["disconnected"] is True
    assert _s.gateway_token == ""
    assert _s.celerp_public_url == ""
    persist.assert_called_once_with(True)
    reconfigure.assert_awaited_once_with(restart=False)


@pytest.mark.asyncio
async def test_cloud_disconnect_persist_failure_leaves_runtime_untouched(client):
    token = await _register(client, "disc-persist-fail")
    from celerp.config import settings as _s
    _s.gateway_token = "old-token"
    _s.celerp_public_url = "https://test.celerp.app"

    with (
        patch("celerp.config.set_cloud_disconnected", side_effect=OSError("disk")),
        patch(
            "celerp.services.cloud_entitlement.reconfigure_gateway_runtime",
            new=AsyncMock(),
        ) as reconfigure,
    ):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))

    assert r.status_code == 200
    assert r.json()["disconnected"] is False
    assert _s.gateway_token == "old-token"
    assert _s.celerp_public_url == "https://test.celerp.app"
    reconfigure.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_disconnect_clears_session_token(client, session):
    """Disconnect must clear the in-memory session token.

    Regression: without this, get_session_token() remains truthy after
    disconnect even though the relay connection is gone.
    """
    import celerp.gateway.state as gw_state
    from celerp.services.session_tracker import clear as _clear_tracker, register_token as _register_token
    import uuid as _uuid

    token = await _register(client, "disc-session")
    from celerp.config import settings as _s
    _s.gateway_token = "old-token"
    gw_state.set_session_token("live-session-token")

    with (
        patch("celerp.config.set_cloud_disconnected"),
        patch(
            "celerp.services.cloud_entitlement.reconfigure_gateway_runtime",
            new=AsyncMock(return_value=False),
        ),
    ):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))

    assert r.status_code == 200
    # The stored session token must be empty after disconnect.
    # Check the underlying variable (conftest patches get_session_token globally).
    assert gw_state._session_token == "", "session_token must be cleared on disconnect"

    # Verify post-disconnect behavior with the cleared token.
    from datetime import datetime, timedelta, timezone as _tz
    await _clear_tracker(session)
    from test_helpers import ensure_user
    await ensure_user(session, "00000000-0000-0000-0000-000000000099")
    await _register_token(session, str(_uuid.uuid4()), "00000000-0000-0000-0000-000000000099", datetime.now(_tz.utc) + timedelta(seconds=900))
    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r2 = await client.post("/auth/login", json={"email": "cloud-disc-session@test.local", "password": "pwvalid1"})
    assert r2.status_code == 409, f"Gate should fire after disconnect, got {r2.status_code}"


@pytest.mark.asyncio
async def test_cloud_disconnect_no_op_when_already_disconnected(client):
    """Disconnect with no active client returns success without error."""
    token = await _register(client, "disc-noop")
    with (
        patch("celerp.config.set_cloud_disconnected"),
        patch(
            "celerp.services.cloud_entitlement.reconfigure_gateway_runtime",
            new=AsyncMock(return_value=False),
        ),
    ):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["disconnected"] is True


# ---------------------------------------------------------------------------
# /settings/cloud-activate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_activate_success(client):
    token = await _register(client, "act-ok")
    from celerp.config import settings as _s
    _s.cloud_disconnected = False
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "gateway_token": "gw-abc123",
        "public_url": "https://myco.celerp.app",
        "tos_version": "2025-01",
    }
    with (
        patch("celerp.config.settings.activation_verifier", "test-verifier"),
        patch("httpx.AsyncClient") as mock_httpx,
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "celerp.routers.health._apply_gateway_token_api",
            new=AsyncMock(return_value=True),
        ),
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["connected"] is True
    assert r.json()["public_url"] == "https://myco.celerp.app"

@pytest.mark.asyncio
async def test_cloud_activate_404_returns_error_with_instance_id(client):
    """Activate returns error dict with instance_id when relay returns 404."""
    token = await _register(client, "act-404")

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"detail": "Instance not registered."}

    with (
        patch("celerp.config.settings.activation_verifier", "test-verifier"),
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
async def test_cloud_activate_applies_authoritative_activation(client):
    token = await _register(client, "act-authoritative")
    from celerp.config import settings as _s
    _s.cloud_disconnected = False
    _s.gateway_token = ""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "gateway_token": "gw-authoritative",
        "public_url": "https://old.celerp.app",
        "tos_version": "2025-01",
    }
    applied = AsyncMock(return_value=True)
    with (
        patch("celerp.config.settings.activation_verifier", "test-verifier"),
        patch("httpx.AsyncClient") as mock_httpx,
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value=""),
        ),
        patch("celerp.routers.health._apply_gateway_token_api", new=applied),
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    applied.assert_awaited_once()
    assert applied.await_args.args[0] == "gw-authoritative"
    assert applied.await_args.kwargs["public_url"] == "https://old.celerp.app"

@pytest.mark.asyncio
async def test_cloud_activate_relay_unreachable(client):
    """Activate returns error when relay is unreachable."""
    import httpx
    token = await _register(client, "act-nonet")

    with (
        patch("celerp.config.settings.activation_verifier", "test-verifier"),
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
# /settings/cloud-accept-tos
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_accept_tos_restarts_client(client):
    token = await _register(client, "tos-ok")
    old_gw = _mock_gw("tos_required")
    old_gw.required_tos_version = "2025-02"
    with (
        patch("celerp.gateway.client.get_client", return_value=old_gw),
        patch("celerp.config.persist_cloud_settings") as persist,
        patch(
            "celerp.services.cloud_entitlement.reconfigure_gateway_runtime",
            new=AsyncMock(return_value=True),
        ) as reconfigure,
    ):
        r = await client.post("/settings/cloud-accept-tos", headers=_h(token))
    assert r.status_code == 200
    persist.assert_called_once_with(tos_version="2025-02")
    reconfigure.assert_awaited_once_with(restart=True)

@pytest.mark.asyncio
async def test_cloud_accept_tos_persist_failure_leaves_runtime_untouched(client):
    token = await _register(client, "tos-persist-fail")
    old_gw = _mock_gw("tos_required")
    old_gw.required_tos_version = "2025-02"
    with (
        patch("celerp.gateway.client.get_client", return_value=old_gw),
        patch("celerp.config.persist_cloud_settings", side_effect=OSError("disk")),
        patch(
            "celerp.services.cloud_entitlement.reconfigure_gateway_runtime",
            new=AsyncMock(),
        ) as reconfigure,
    ):
        r = await client.post("/settings/cloud-accept-tos", headers=_h(token))
    assert r.status_code == 200
    assert "error" in r.json()
    reconfigure.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_claim_success_activates_immediately(client):
    token = await _register(client, "claim-ok")
    claim_resp = MagicMock()
    claim_resp.status_code = 200
    claim_resp.json.return_value = {"claimed": True}
    act_resp = MagicMock()
    act_resp.status_code = 200
    act_resp.json.return_value = {
        "gateway_token": "gw-claimed",
        "public_url": "https://claimed.celerp.app",
    }
    calls = []
    async def post(url, **kwargs):
        calls.append((url, kwargs))
        return claim_resp if "billing/claim" in url else act_resp
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "celerp.config.ensure_connect_identity",
            return_value=("claim-iid", "claim-verifier"),
        ),
        patch("celerp.config.set_cloud_disconnected"),
        patch(
            "celerp.routers.health._apply_gateway_token_api",
            new=AsyncMock(return_value=True),
        ) as applied,
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=post)
        r = await client.post(
            "/settings/cloud-claim", headers=_h(token),
            json={"email": "test@example.com", "otp_code": "123456"})
    assert r.status_code == 200
    assert r.json()["connected"] is True
    assert calls[-1][1]["json"]["activation_verifier"] == "claim-verifier"
    assert "Authorization" not in calls[-1][1].get("headers", {})
    assert applied.await_args.kwargs["expected_verifier"] == "claim-verifier"

@pytest.mark.asyncio
async def test_cloud_claim_otp_invalid(client):
    """Returns otp_error dict when relay rejects OTP."""
    token = await _register(client, "claim-otp-bad")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"detail": {"code": "otp_invalid", "attempts_left": 2}}

    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        r = await client.post(
            "/settings/cloud-claim",
            headers=_h(token),
            json={"email": "test@example.com", "otp_code": "wrong"},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["otp_error"] is True
    assert data["attempts_left"] == 2


@pytest.mark.asyncio
async def test_cloud_send_otp_proxies_via_api(client):
    """send-otp forwards the entered email plus the canonical instance_id of
    THIS install from the API process, under the claim-specific relay deadline."""
    from celerp.config import ensure_instance_id
    from celerp.routers import health as health_router

    token = await _register(client, "otp-send")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}

    with patch("httpx.AsyncClient") as mock_httpx:
        sent_payload = {}
        async def capture_post(url, **kwargs):
            sent_payload.update(kwargs.get("json", {}))
            return mock_resp
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(side_effect=capture_post)
        r = await client.post(
            "/settings/cloud-send-otp",
            headers=_h(token),
            json={"email": "user@example.com"},
        )

    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert sent_payload["email"] == "user@example.com"
    assert sent_payload["instance_id"] == ensure_instance_id()
    assert sent_payload["instance_id"] == data.get("instance_id")
    challenge = sent_payload.get("activation_challenge", "")
    assert len(challenge) == 64
    int(challenge, 16)
    assert "activation_verifier" not in sent_payload
    # The relay call runs under the claim deadline, not the generic client default.
    assert mock_httpx.call_args.kwargs["timeout"] == relay_timeout(health_router.RELAY_CLAIM_TIMEOUT)


@pytest.mark.asyncio
async def test_cloud_send_otp_proves_incumbent_when_stored_key_exists(client):
    token = await _register(client, "otp-incumbent")
    captured = {}
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {}
    async def post(_url, **kwargs):
        captured.update(kwargs)
        return response
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value="stored-key"),
        ),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("instance-jwt", "canonical-iid")),
        ),
        patch(
            "celerp.config.ensure_connect_identity",
            return_value=("canonical-iid", "proof"),
        ),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=post)
        r = await client.post(
            "/settings/cloud-send-otp", headers=_h(token),
            json={"email": "owner@example.com"})
    assert r.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer instance-jwt"
    assert captured["json"]["instance_id"] == "canonical-iid"

@pytest.mark.asyncio
async def test_cloud_send_otp_relay_timeout_is_reported_as_relay_timeout(client):
    """A relay that stalls past the claim deadline is reported as a relay
    timeout in the error dict (HTTP 200), never as a local API failure."""
    import httpx

    token = await _register(client, "otp-send-timeout")

    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ReadTimeout("relay stalled"))
        r = await client.post(
            "/settings/cloud-send-otp",
            headers=_h(token),
            json={"email": "user@example.com"},
        )

    assert r.status_code == 200
    assert "timed out" in r.json()["error"]


@pytest.mark.asyncio
async def test_cloud_send_otp_reopens_after_a_silent_connect_failure(client):
    """A connection attempt that never completes (a lost SYN the machine does
    not retransmit) fails at the short connect deadline and the leg opens a
    fresh socket, so the code is still sent inside the claim deadline."""
    import httpx
    from celerp.routers import health as health_router

    token = await _register(client, "otp-connect-retry")
    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {}

    with patch("httpx.AsyncClient") as mock_httpx:
        post = AsyncMock(side_effect=[httpx.ConnectTimeout("connect deadline"), ok])
        mock_httpx.return_value.__aenter__.return_value.post = post
        r = await client.post(
            "/settings/cloud-send-otp",
            headers=_h(token),
            json={"email": "user@example.com"},
        )

    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert post.await_count == 2
    # One fresh client, and so one fresh socket, per attempt.
    assert mock_httpx.call_count == 2
    for call in mock_httpx.call_args_list:
        assert call.kwargs["timeout"].connect == RELAY_CONNECT_TIMEOUT_S
    assert RELAY_CONNECT_TIMEOUT_S * RELAY_CONNECT_ATTEMPTS <= health_router.RELAY_CLAIM_TIMEOUT


@pytest.mark.asyncio
async def test_cloud_claim_gives_up_after_the_connect_attempt_budget(client):
    """Every connection attempt failing is reported as unreachable after the
    fixed attempt budget, never as an unbounded retry."""
    import httpx

    token = await _register(client, "claim-connect-budget")

    with patch("httpx.AsyncClient") as mock_httpx:
        post = AsyncMock(side_effect=httpx.ConnectTimeout("connect deadline"))
        mock_httpx.return_value.__aenter__.return_value.post = post
        r = await client.post(
            "/settings/cloud-claim",
            headers=_h(token),
            json={"email": "user@example.com", "otp_code": "000000"},
        )

    assert r.status_code == 200
    assert "timed out" in r.json()["error"]
    assert post.await_count == RELAY_CONNECT_ATTEMPTS


@pytest.mark.asyncio
async def test_cloud_claim_never_resends_a_request_the_relay_may_have_seen(client):
    """A timeout after the request left the machine is final: the claim is not
    re-sent, so the relay can never process one submission twice."""
    import httpx

    token = await _register(client, "claim-read-timeout")

    with patch("httpx.AsyncClient") as mock_httpx:
        post = AsyncMock(side_effect=httpx.ReadTimeout("relay stalled"))
        mock_httpx.return_value.__aenter__.return_value.post = post
        r = await client.post(
            "/settings/cloud-claim",
            headers=_h(token),
            json={"email": "user@example.com", "otp_code": "000000"},
        )

    assert r.status_code == 200
    assert "timed out" in r.json()["error"]
    assert post.await_count == 1


def test_ui_claim_deadline_exceeds_api_wait():
    """UI deadline outlasts the two bounded relay operations plus overhead."""
    from celerp.routers import health as health_router
    from ui import api_client

    # Config persistence can legitimately wait up to 5s on its cross-process
    # lock. Gateway application can then wait up to 3s for readiness. Outer UI
    # deadlines must strictly contain those local phases plus relay work.
    assert api_client.ACCOUNT_METHODS_TIMEOUT >= (
        health_router.RELAY_ACCOUNT_METHODS_TIMEOUT + 6.0
    )
    assert api_client.ACCOUNT_SIGNUP_TIMEOUT >= (
        health_router.RELAY_ACCOUNT_SIGNUP_TIMEOUT + 6.0
    )
    assert api_client.OTP_TIMEOUT >= (
        health_router.RELAY_CLAIM_TIMEOUT + 6.0
    )
    assert api_client.CLAIM_TIMEOUT >= (
        health_router.RELAY_CLAIM_TIMEOUT
        + health_router.CLAIM_ACTIVATE_TIMEOUT
        + 9.0
    )
    assert api_client.CONTROL_PLANE_TIMEOUT >= (
        health_router.RELAY_CONTROL_TIMEOUT + 9.0
    )

@pytest.mark.asyncio
async def test_ui_send_otp_uses_its_own_deadline():
    """ui.api_client.send_otp opens its client with the send-otp deadline."""
    from contextlib import asynccontextmanager

    import httpx
    from ui import api_client

    seen = {}

    @asynccontextmanager
    async def fake_client(token, timeout=None):
        seen["timeout"] = timeout
        c = MagicMock()
        c.post = AsyncMock(return_value=httpx.Response(200, json={"ok": True}))
        yield c

    with patch.object(api_client, "_api_client", fake_client):
        data = await api_client.send_otp("tok", "user@example.com")

    assert data == {"ok": True}
    assert seen["timeout"] == api_client.OTP_TIMEOUT


@pytest.mark.asyncio
async def test_ui_cloud_claim_uses_claim_deadline():
    """ui.api_client.cloud_claim opens its client with the claim deadline."""
    from contextlib import asynccontextmanager

    import httpx
    from ui import api_client

    seen = {}

    @asynccontextmanager
    async def fake_client(token, timeout=None):
        seen["timeout"] = timeout
        c = MagicMock()
        c.post = AsyncMock(return_value=httpx.Response(200, json={"linked": True}))
        yield c

    with patch.object(api_client, "_api_client", fake_client):
        data = await api_client.cloud_claim("tok", {"email": "user@example.com", "otp_code": "123456"})

    assert data == {"linked": True}
    assert seen["timeout"] == api_client.CLAIM_TIMEOUT


@pytest.mark.asyncio
async def test_cloud_claim_leg_uses_relay_claim_deadline(client):
    """The claim round trip to the relay runs under the same claim-flow deadline
    as the send-code leg, so the UI's wait always outlasts it."""
    from celerp.routers import health as health_router

    token = await _register(client, "claim-deadline")

    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {"detail": {"code": "otp_invalid", "attempts_left": 1}}

    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        r = await client.post(
            "/settings/cloud-claim",
            headers=_h(token),
            json={"email": "user@example.com", "otp_code": "000000"},
        )

    assert r.status_code == 200
    assert mock_httpx.call_args.kwargs["timeout"] == relay_timeout(health_router.RELAY_CLAIM_TIMEOUT)


@pytest.mark.asyncio
async def test_cloud_claim_returns_linked_without_background_activation(client):
    import asyncio

    from celerp.routers import health as health_router

    token = await _register(client, "claim-slow-activate")
    claim_resp = MagicMock()
    claim_resp.status_code = 200
    claim_resp.json.return_value = {"claimed": True}

    activation_started = asyncio.Event()
    activation_cancelled = asyncio.Event()
    calls = 0

    async def _post(url, **kwargs):
        nonlocal calls
        calls += 1
        if "billing/claim" in url:
            return claim_resp
        activation_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            activation_cancelled.set()
            raise

    applied = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch.object(health_router, "_apply_gateway_token_api", applied),
        patch.object(health_router, "CLAIM_ACTIVATE_TIMEOUT", 0.05),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(side_effect=_post)
        r = await client.post(
            "/settings/cloud-claim",
            headers=_h(token),
            json={"email": "user@example.com", "otp_code": "123456"},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["linked"] is True
    assert data["instance_id"]
    assert activation_started.is_set()
    assert activation_cancelled.is_set()
    applied.assert_not_awaited()
    assert calls == 2

@pytest.mark.asyncio
async def test_cloud_claim_relay_timeout_names_the_restart_path(client):
    """When the claim leg itself times out, the relay may or may not have
    completed the link; the error says so and names the recovery (retry, or
    restart Celerp, which activates on startup)."""
    import httpx

    token = await _register(client, "claim-timeout")

    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ReadTimeout("relay stalled"))
        r = await client.post(
            "/settings/cloud-claim",
            headers=_h(token),
            json={"email": "user@example.com", "otp_code": "123456"},
        )

    assert r.status_code == 200
    err = r.json()["error"]
    assert "timed out" in err
    assert "restart Celerp" in err


@pytest.mark.asyncio
async def test_relay_claim_legs_log_timing_without_the_address(client, caplog):
    """Every relay round trip on the claim flow logs how long it took and its
    outcome, so a stalled leg can be diagnosed from the app log. The address
    and the code never appear in those records."""
    import logging
    import re

    token = await _register(client, "claim-timing")

    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {"sent": True}

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        caplog.at_level(logging.INFO, logger="celerp.routers.health"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=ok)
        r = await client.post(
            "/settings/cloud-send-otp",
            headers=_h(token),
            json={"email": "timing@example.com"},
        )

    assert r.status_code == 200
    legs = [rec.getMessage() for rec in caplog.records
            if rec.name == "celerp.routers.health" and "relay send-otp leg" in rec.getMessage()]
    assert len(legs) == 1, caplog.text
    assert re.search(r"\d+\.\d+s", legs[0]) and "status 200" in legs[0]
    assert all("timing@example.com" not in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# /settings/connectors-catalog
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connectors_catalog_not_connected_returns_error(client):
    """Returns error dict (not 5xx) when no gateway_token is configured."""
    token = await _register(client, "conn-notoken")

    with patch("celerp.config.settings") as mock_settings:
        mock_settings.gateway_token = ""
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        r = await client.get("/settings/connectors-catalog", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert data["connectors"] == []


@pytest.mark.asyncio
async def test_connectors_catalog_success(client):
    """Returns connector list when relay responds with catalog."""
    token = await _register(client, "conn-success")

    fake_catalog = [
        {"id": "shopify", "name": "Shopify", "category": "website", "connected": False},
        {"id": "quickbooks", "name": "QuickBooks", "category": "accounting", "connected": False},
    ]

    tok_resp = MagicMock()
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"access_token": "relay-jwt-xyz"}

    cat_resp = MagicMock()
    cat_resp.status_code = 200
    cat_resp.json.return_value = {"connectors": fake_catalog}

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid-abc"), \
         patch("celerp.connectors.ownership.connector_owned_by_company",
               new=AsyncMock(return_value=True)), \
         patch("httpx.AsyncClient") as mock_httpx:
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"

        call_count = {"n": 0}
        async def fake_request(url, **kwargs):
            call_count["n"] += 1
            if "/auth/token" in url:
                return tok_resp
            return cat_resp

        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(side_effect=fake_request)
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(return_value=cat_resp)

        r = await client.get("/settings/connectors-catalog", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" not in data
    assert len(data["connectors"]) == 2
    assert data["connectors"][0]["id"] == "shopify"


@pytest.mark.asyncio
async def test_connectors_catalog_masks_instance_connection_for_non_owner(client):
    token = await _register(client, "conn-non-owner")

    tok_resp = MagicMock()
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"access_token": "relay-jwt-non-owner"}

    cat_resp = MagicMock()
    cat_resp.status_code = 200
    cat_resp.json.return_value = {
        "connectors": [
            {"id": "woocommerce", "name": "WooCommerce", "connected": True}
        ]
    }

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid-abc"), \
         patch("celerp.connectors.ownership.connector_owned_by_company",
               new=AsyncMock(return_value=False)), \
         patch("httpx.AsyncClient") as mock_httpx:
        mock_settings.gateway_token = "api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=tok_resp
        )
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=cat_resp
        )

        response = await client.get(
            "/settings/connectors-catalog", headers=_h(token)
        )

    assert response.status_code == 200
    assert response.json()["connectors"][0]["connected"] is False


@pytest.mark.asyncio
async def test_connectors_catalog_relay_token_exchange_failure(client):
    """Returns error dict when relay refuses the API key (auth/token returns 401)."""
    token = await _register(client, "conn-badkey")

    bad_tok_resp = MagicMock()
    bad_tok_resp.status_code = 401
    bad_tok_resp.json.return_value = {"detail": "Invalid API key"}

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid-abc"), \
         patch("httpx.AsyncClient") as mock_httpx:
        mock_settings.gateway_token = "bad-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"

        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=bad_tok_resp)
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock()

        r = await client.get("/settings/connectors-catalog", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert data["connectors"] == []


@pytest.mark.asyncio
async def test_connectors_catalog_relay_unreachable(client):
    """Returns error dict on ConnectError (no crash, no 500)."""
    import httpx as _httpx
    token = await _register(client, "conn-unreachable")

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid-abc"), \
         patch("httpx.AsyncClient") as mock_httpx:
        mock_settings.gateway_token = "some-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"

        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=_httpx.ConnectError("Connection refused")
        )
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock()

        r = await client.get("/settings/connectors-catalog", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert data["connectors"] == []


# ---------------------------------------------------------------------------
# /settings/connectors/{platform}/authorize-url
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connector_authorize_url_success(client):
    """Accounting OAuth claims ownership with the accounting sync default."""
    token = await _register(client, "auth-url-ok")

    tok_resp = MagicMock()
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"access_token": "relay-jwt-abc"}

    url_resp = MagicMock()
    url_resp.status_code = 200
    url_resp.json.return_value = {
        "authorize_url":
            "https://accounts.intuit.com/oauth2/v1/authorize?state=xyz"
    }

    claim = AsyncMock(return_value=(object(), True))
    lock = AsyncMock(return_value=object())
    cancelled = MagicMock()
    cancelled.status_code = 404
    relay = AsyncMock(side_effect=[cancelled, url_resp])
    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid"), \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.gateway.state.with_relay_client", relay):
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"

        response = await client.get(
            "/settings/connectors/quickbooks/authorize-url", headers=_h(token)
        )

    assert response.status_code == 200
    data = response.json()
    assert "error" not in data
    assert "intuit.com" in data["authorize_url"]
    assert claim.await_count == 1
    assert claim.await_args.args[2] == "quickbooks"
    assert claim.await_args.kwargs["default_sync_frequency"] == "manual"
    assert claim.await_args.kwargs["report_created"] is True
    lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_connector_authorize_url_requires_disconnect_before_reconnect(client):
    token = await _register(client, "auth-url-existing")
    claim = AsyncMock(return_value=(object(), False))
    lock = AsyncMock()
    relay = AsyncMock()

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.gateway.state.with_relay_client", relay):
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        response = await client.get(
            "/settings/connectors/quickbooks/authorize-url", headers=_h(token)
        )

    assert response.status_code == 200
    assert "Disconnect the existing connector" in response.json()["error"]
    lock.assert_not_awaited()
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_connector_authorize_url_not_connected(client):
    """Returns error when no gateway_token configured."""
    token = await _register(client, "auth-url-notoken")

    with patch("celerp.config.settings") as mock_settings:
        mock_settings.gateway_token = ""
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        r = await client.get("/settings/connectors/shopify/authorize-url", headers=_h(token))

    assert r.status_code == 200
    assert "error" in r.json()


@pytest.mark.asyncio
async def test_connector_authorize_url_shopify_passes_shop(client):
    """Shopify authorize URL request forwards the shop param to the relay."""
    token = await _register(client, "auth-url-shopify")

    tok_resp = MagicMock()
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"access_token": "relay-jwt-shopify"}

    url_resp = MagicMock()
    url_resp.status_code = 200
    url_resp.json.return_value = {"authorize_url": "https://my-shop.myshopify.com/admin/oauth/authorize?client_id=abc"}

    captured_params = {}

    async def fake_get(url, **kwargs):
        captured_params.update(kwargs.get("params", {}))
        return url_resp

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid"), \
         patch("httpx.AsyncClient") as mock_httpx:
        mock_settings.gateway_token = "api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"

        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=tok_resp)
        mock_httpx.return_value.__aenter__.return_value.delete = AsyncMock(
            return_value=MagicMock(status_code=404)
        )
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(side_effect=fake_get)

        r = await client.get(
            "/settings/connectors/shopify/authorize-url",
            params={"shop": "my-shop.myshopify.com"},
            headers=_h(token),
        )

    assert r.status_code == 200
    assert "authorize_url" in r.json()
    assert captured_params.get("shop") == "my-shop.myshopify.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["connecting", "error"])
async def test_cloud_status_not_connected_while_connecting_or_error(client, status):
    """The relay dot must NOT report connected (green) for transient/failed states
    — only 'active'/'tos_required'. It was greening on 'connecting'/'error', i.e.
    showing "connected" while the relay was actually returning 502."""
    token = await _register(client, status)
    gw = _mock_gw(status)
    with patch("celerp.gateway.client.get_client", return_value=gw), \
         patch("celerp.gateway.state.get_session_token", return_value="tok"):
        r = await client.get("/settings/cloud-status", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["connected"] is False, r.json()
    assert r.json()["relay_status"] == status


@pytest.mark.asyncio
async def test_connectors_catalog_402_reports_needs_plan(client):
    """A relay 402 (free account, no entitled plan) must surface the relay's
    plain upgrade message AND the needs_plan marker so the UI can render the
    trial CTA instead of a network-error string."""
    token = await _register(client, "conn-402")

    tok_resp = MagicMock()
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"access_token": "relay-jwt-xyz"}

    gated_resp = MagicMock()
    gated_resp.status_code = 402
    gated_resp.json.return_value = {"detail": "Connectors need an active plan."}

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid-abc"), \
         patch("httpx.AsyncClient") as mock_httpx:
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"

        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=tok_resp)
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(return_value=gated_resp)

        r = await client.get("/settings/connectors-catalog", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert data["error"] == "Connectors need an active plan."
    assert data["needs_plan"] is True
    assert data["connectors"] == []


@pytest.mark.asyncio
async def test_cloud_disconnect_is_sticky(client):
    token = await _register(client, "sticky")
    gw = _mock_gw("active")
    from celerp.config import settings as _s
    _s.gateway_token = "old-token"
    _s.cloud_disconnected = False
    def persist(value):
        _s.cloud_disconnected = value
    with (
        patch("celerp.gateway.client.get_client", return_value=gw),
        patch("celerp.gateway.client.set_client"),
        patch("celerp.config.set_cloud_disconnected", side_effect=persist) as saved,
    ):
        r = await client.post("/settings/cloud-disconnect", headers=_h(token))
    assert r.status_code == 200
    assert _s.cloud_disconnected is True
    saved.assert_called_once_with(True)
    assert _s.gateway_token == ""

@pytest.mark.asyncio
async def test_cloud_activate_established_reconnect_preserves_credential(client):
    token = await _register(client, "reconnect-preserves-key")

    from celerp.config import settings as _s
    from celerp.routers import health as health_router
    _s.cloud_disconnected = False
    _s.gateway_token = "existing-gateway-key"

    tok_resp = MagicMock()
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"access_token": "same-instance-jwt"}
    act_resp = MagicMock()
    act_resp.status_code = 200
    act_resp.json.return_value = {
        "gateway_token": None,
        "public_url": "https://paid.celerp.app",
        "tos_version": "2025-01",
        "reconnect": True,
    }

    seen = []
    async def _post(url, **kwargs):
        seen.append((url, kwargs))
        return tok_resp if url.endswith("/auth/token") else act_resp

    applied = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch.object(health_router, "_apply_gateway_token_api", applied),
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(side_effect=_post)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    assert r.json()["connected"] is True
    assert len(seen) == 2
    assert seen[0][0].endswith("/auth/token")
    assert seen[0][1]["json"] == {"api_key": "existing-gateway-key"}
    assert seen[1][0].endswith("/auth/activate")
    assert seen[1][1]["headers"]["Authorization"] == "Bearer same-instance-jwt"
    assert "activation_verifier" not in seen[1][1]["json"]
    applied.assert_awaited_once()
    assert applied.await_args.args[0] == "existing-gateway-key"
    assert applied.await_args.kwargs["public_url"] == "https://paid.celerp.app"

@pytest.mark.asyncio
async def test_cloud_activate_relay_unreachable_reports_error_and_keeps_disconnect(client):
    """When the relay cannot be reached, reconnect degrades honestly: it returns an
    error and leaves the disconnect state untouched. It must NOT re-apply the stored
    token or claim connected - re-arming a credential the relay may have rotated away
    from is exactly the rejected state this flow exists to clear (HOLY determinism:
    fall back to a neutral state, never fabricate success)."""
    import httpx
    token = await _register(client, "reconnect-unreachable")

    from celerp.config import settings as _s
    _s.cloud_disconnected = True
    _s.gateway_token = ""
    _s.backup_enabled = False

    applied = {}
    # A preserved token is on disk: the flow must still refuse to replay it offline.
    stored = {"cloud": {"token": "stored-maybe-orphaned-tok", "public_url": "https://co.celerp.app"}}

    async def _fake_apply(tok, iid, public_url=None, tos_version=None):
        applied["token"] = tok  # must never run on the unreachable path
        _s.cloud_disconnected = False

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.read_config", return_value=stored),
        patch("celerp.routers.health._apply_gateway_token_api", new=_fake_apply),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("refused"))
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert "error" in data and data.get("connected") is not True
    assert applied == {}  # no token applied - nothing fabricated
    assert _s.cloud_disconnected is True  # disconnect state left honest


def test_legacy_relay_toggle_routes_absent():
    """Dead relay compatibility endpoints remain absent."""
    from celerp.main import app as _app

    registered = set(_app.openapi().get("paths", {}).keys())
    assert "/companies/me/relay/enable" not in registered
    assert "/companies/me/relay/disable" not in registered
    assert "/settings/cloud-apply-token" not in registered


@pytest.mark.asyncio
async def test_relay_settings_endpoints_reject_unauthenticated(client):
    """Relay account endpoints mutate or expose account state, so a request
    without credentials is rejected instead of executing."""
    r = await client.post("/settings/cloud-disconnect")
    assert r.status_code == 401
    r = await client.post("/settings/cloud-activate")
    assert r.status_code == 401
    r = await client.get("/settings/account-methods")
    assert r.status_code == 401



@pytest.mark.asyncio
async def test_cloud_activate_token_500_never_falls_through_to_uuid_authority(client):
    token = await _register(client, "token-500-no-downgrade")
    from celerp.config import settings as _s
    _s.gateway_token = "existing-key"

    seen = []
    async def _post(url, **kwargs):
        seen.append(url)
        if url.endswith("/auth/token"):
            resp = MagicMock()
            resp.status_code = 500
            return resp
        raise AssertionError("/auth/activate must not be called after ambiguous auth failure")

    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(side_effect=_post)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    assert "error" in r.json()
    assert seen == ["https://relay.celerp.com/auth/token"]


@pytest.mark.asyncio
async def test_cloud_activate_rejected_key_uses_proof_on_current_relay(client):
    token = await _register(client, "token-401-proof")
    from celerp.config import settings as _s
    from celerp.routers import health as health_router

    _s.gateway_token = "stale-key"
    _s.activation_verifier = "saved-verifier"

    token_resp = MagicMock()
    token_resp.status_code = 401
    methods_resp = MagicMock()
    methods_resp.status_code = 200
    methods_resp.json.return_value = {"secure_activation": True}
    activate_resp = MagicMock()
    activate_resp.status_code = 200
    activate_resp.json.return_value = {
        "gateway_token": "replacement-key",
        "public_url": "https://paid.celerp.com",
    }

    posted = []
    async def _post(url, **kwargs):
        posted.append((url, kwargs))
        return token_resp if url.endswith("/auth/token") else activate_resp

    applied = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch.object(health_router, "_apply_gateway_token_api", applied),
    ):
        c = mock_httpx.return_value.__aenter__.return_value
        c.post = AsyncMock(side_effect=_post)
        c.get = AsyncMock(return_value=methods_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    assert r.json()["connected"] is True
    assert len(posted) == 2
    assert posted[1][0].endswith("/auth/activate")
    assert posted[1][1]["json"]["activation_verifier"] == "saved-verifier"
    assert posted[1][1]["headers"] == {}
    applied.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_signup_config_wait_runs_off_event_loop(client):
    """A synchronous config-lock wait must not freeze the sole API event loop."""
    import asyncio
    import threading
    import time

    token = await _register(client, "signup-config-thread")
    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def _slow_identity():
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return ("00000000-0000-0000-0000-000000000123", "verifier")

    relay = MagicMock()
    relay.status_code = 202
    relay.json.return_value = {"sent": True}

    async def _post(*args, **kwargs):
        return relay

    ticked = asyncio.Event()

    async def _tick():
        await asyncio.sleep(0.01)
        ticked.set()

    with (
        patch("celerp.config.ensure_connect_identity", side_effect=_slow_identity),
        patch("httpx.AsyncClient") as mock_httpx,
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(side_effect=_post)
        ticker = asyncio.create_task(_tick())
        response = await client.post(
            "/settings/account-signup", headers=_h(token),
            json={"email": "threaded@example.test"})
        await ticker

    assert response.status_code == 200
    assert response.json()["sent"] is True
    assert ticked.is_set()
    assert worker_threads
    assert all(tid != main_thread for tid in worker_threads)


@pytest.mark.asyncio
async def test_connector_authorize_failure_releases_new_claim_after_cancel(client):
    token = await _register(client, "auth-url-cleanup")
    failed = MagicMock()
    failed.status_code = 502
    failed.text = "upstream failed"
    failed.json.return_value = {"detail": "upstream failed"}
    cancelled = MagicMock()
    cancelled.status_code = 404

    claim = AsyncMock(return_value=(object(), True))
    lock = AsyncMock(return_value=object())
    release = AsyncMock()
    relay = AsyncMock(side_effect=[failed, cancelled])

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid"), \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.connectors.ownership.release_connector_ownership", release), \
         patch("celerp.gateway.state.with_relay_client", relay):
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        response = await client.get(
            "/settings/connectors/quickbooks/authorize-url",
            headers=_h(token),
        )

    assert response.status_code == 200
    assert response.json()["error"] == "Could not reset the previous connection."
    release.assert_awaited_once()
    assert relay.await_count == 2


@pytest.mark.asyncio
async def test_connector_authorize_ambiguous_cleanup_keeps_new_claim(client):
    import httpx as _httpx

    token = await _register(client, "auth-url-cleanup-fails")
    claim = AsyncMock(return_value=(object(), True))
    lock = AsyncMock(return_value=object())
    release = AsyncMock()
    relay = AsyncMock(side_effect=[
        _httpx.TimeoutException("authorize timed out"),
        _httpx.ConnectError("cleanup unavailable"),
    ])

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid"), \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.connectors.ownership.release_connector_ownership", release), \
         patch("celerp.gateway.state.with_relay_client", relay):
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        response = await client.get(
            "/settings/connectors/quickbooks/authorize-url",
            headers=_h(token),
        )

    assert response.status_code == 200
    assert "disconnect it before retrying" in response.json()["error"]
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_connector_authorize_persists_owner_before_ambiguous_remote_write():
    import httpx as _httpx
    from celerp.routers.health import connector_authorize_url

    session = AsyncMock()
    claim = AsyncMock(return_value=(object(), True))
    lock = AsyncMock(return_value=object())
    release = AsyncMock()
    events = []

    async def _commit():
        events.append("commit")

    session.commit = AsyncMock(side_effect=_commit)

    async def _relay(*_args, **_kwargs):
        events.append("relay")
        if events.count("relay") == 1:
            raise _httpx.TimeoutException("authorize timed out")
        raise _httpx.ConnectError("cleanup unavailable")

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.config.ensure_instance_id", return_value="test-iid"), \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.connectors.ownership.release_connector_ownership", release), \
         patch(
             "celerp.gateway.state.with_relay_client",
             new=AsyncMock(side_effect=_relay),
         ):
        mock_settings.gateway_token = "my-api-key"
        result = await connector_authorize_url(
            "quickbooks",
            company_id="company-test",
            session=session,
        )

    assert "disconnect it before retrying" in result["error"]
    assert events[0] == "commit"
    assert events[1:] == ["relay", "relay"]
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_connector_reauthorize_requires_disconnect_without_releasing_owner(client):
    token = await _register(client, "auth-url-existing-owner")
    claim = AsyncMock(return_value=(object(), False))
    lock = AsyncMock()
    release = AsyncMock()
    relay = AsyncMock()

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.connectors.ownership.release_connector_ownership", release), \
         patch("celerp.gateway.state.with_relay_client", relay):
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        response = await client.get(
            "/settings/connectors/quickbooks/authorize-url",
            headers=_h(token),
        )

    assert response.status_code == 200
    assert "Disconnect the existing connector" in response.json()["error"]
    lock.assert_not_awaited()
    relay.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_connector_authorize_lock_failure_keeps_durable_claim(client):
    from celerp.connectors.ownership import ConnectorOwnershipError

    token = await _register(client, "auth-url-lock-fails")
    claim = AsyncMock(return_value=(object(), True))
    lock = AsyncMock(side_effect=ConnectorOwnershipError("ownership changed"))
    release = AsyncMock()

    with patch("celerp.config.settings") as mock_settings, \
         patch("celerp.connectors.ownership.claim_connector_ownership", claim), \
         patch("celerp.connectors.ownership.lock_connector_operation", lock), \
         patch("celerp.connectors.ownership.release_connector_ownership", release):
        mock_settings.gateway_token = "my-api-key"
        mock_settings.celerp_relay_url = "https://relay.celerp.com"
        response = await client.get(
            "/settings/connectors/quickbooks/authorize-url",
            headers=_h(token),
        )

    assert response.status_code == 409
    release.assert_not_awaited()
