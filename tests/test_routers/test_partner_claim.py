# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for the existing-install partner-claim API endpoints.

POST /settings/partner-claim/resolve previews the partner identity behind a
claim token (no binding). POST /settings/partner-claim/accept triggers the relay
bind. Both are owner/admin only, validate the claim token at the function
boundary before any relay call, and degrade honestly when the relay is
unreachable or parked (the app stores no relay-authoritative state and stays
celerp_direct). All relay HTTP calls are mocked so tests run offline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.services.auth import create_access_token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _role_token(role: str) -> str:
    token, _ = create_access_token(
        subject="00000000-0000-0000-0000-000000000001",
        company_id="00000000-0000-0000-0000-000000000002",
        role=role,
    )
    return token


def _relay_resp(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    return resp


_IDENTITY = {
    "partner_name": "Acme Partners",
    "partner_id": "prt_123",
    "effect": "This install will be managed by Acme Partners.",
}


# ── authorization (adversarial #10) ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator", "manager"])
@pytest.mark.parametrize("path", [
    "/settings/partner-claim/resolve",
    "/settings/partner-claim/accept",
])
async def test_partner_claim_requires_owner_admin(client, role, path):
    """Both endpoints refuse non-owner/admin roles with 403, independently of any
    UI render gate, and never reach the relay."""
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock()
        r = await client.post(path, headers=_h(_role_token(role)), json={"claim_token": "tok-abc"})
    assert r.status_code == 403
    # No relay call was made for a rejected role.
    assert mock_httpx.return_value.__aenter__.return_value.post.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_partner_claim_resolve_allows_owner_admin(client, role):
    """Owner and admin reach the resolve path (relay mocked to a valid identity)."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-1"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=_relay_resp(200, _IDENTITY))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token(role)), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" not in r.json()


# ── resolve: success + honest degradation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_partner_claim_resolve_shows_partner_identity(client):
    """A successful resolve previews the exact partner identity and effect, and
    binds nothing."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-1"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=_relay_resp(200, _IDENTITY))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert data["partner_name"] == "Acme Partners"
    assert data["partner_id"] == "prt_123"
    # Nothing is bound by a resolve: the local commercial mode is untouched.
    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


@pytest.mark.asyncio
async def test_partner_claim_resolve_degrades_when_relay_unreachable(client):
    """A relay connection error yields a neutral could-not-verify message; the
    install stays celerp_direct and nothing is bound."""
    import httpx

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-1"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("no route"))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "partner_name" not in data
    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


@pytest.mark.asyncio
async def test_partner_claim_resolve_degrades_on_non_200(client):
    """A non-200 relay response (invalid/expired/used token, or parked endpoint)
    degrades to a neutral message, never a partial render."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-1"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=_relay_resp(404, {"detail": "unknown claim"}))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" in r.json()
    assert "partner_name" not in r.json()


# ── input validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", ["", "   ", None])
async def test_partner_claim_resolve_rejects_empty_token(client, bad_token):
    """An empty/whitespace/missing claim token is rejected before any relay call."""
    body = {} if bad_token is None else {"claim_token": bad_token}
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock()
        r = await client.post(
            "/settings/partner-claim/resolve", headers=_h(_role_token("owner")), json=body)
    assert r.status_code == 200
    assert "error" in r.json()
    assert mock_httpx.return_value.__aenter__.return_value.post.await_count == 0


@pytest.mark.asyncio
async def test_partner_claim_resolve_rejects_oversized_token(client):
    """A claim token over the 512-char bound is rejected at the API function
    boundary, before any relay call (bounds the payload sent to the relay)."""
    oversized = "x" * 513
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock()
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": oversized})
    assert r.status_code == 200
    assert "error" in r.json()
    assert mock_httpx.return_value.__aenter__.return_value.post.await_count == 0


@pytest.mark.asyncio
async def test_partner_claim_resolve_rejects_malformed_identity_payload(client):
    """A relay 200 with missing/wrong-typed identity fields is treated as
    could-not-verify, never partially rendered or fabricated."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-1"),
    ):
        # Missing partner_name / partner_id, and identity is not an object shape.
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=_relay_resp(200, {"partner_name": 123}))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "partner_name" not in data


# ── accept + decline ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partner_claim_accept_calls_relay(client):
    """Accept posts the claim token and instance id to the relay accept endpoint
    and returns success; it never touches gateway_token."""
    from celerp.config import settings as _s
    before = _s.gateway_token

    post_mock = AsyncMock(return_value=_relay_resp(200, {"accepted": True}))
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-accept"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = post_mock
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-accept"})
    assert r.status_code == 200
    assert "error" not in r.json()
    # The relay accept was called with the claim token and instance id.
    assert post_mock.await_count == 1
    _, kwargs = post_mock.await_args
    sent = kwargs.get("json", {})
    assert sent.get("claim_token") == "tok-accept"
    assert sent.get("instance_id") == "iid-accept"
    # gateway_token (live session credential) is untouched.
    assert _s.gateway_token == before


@pytest.mark.asyncio
async def test_partner_claim_accept_double_submit_is_idempotent(client):
    """A second accept for an already-partner_managed install is a neutral no-op,
    never a raw error (the relay reports already-owned; the app surfaces it
    cleanly and stays consistent)."""
    resp = _relay_resp(200, {"accepted": True, "already_owned": True})
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-dup"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        r1 = await client.post(
            "/settings/partner-claim/accept",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-dup"})
        r2 = await client.post(
            "/settings/partner-claim/accept",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-dup"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert "error" not in r2.json()


@pytest.mark.asyncio
async def test_partner_claim_accept_degrades_when_relay_unreachable(client):
    """Accept degrades honestly when the relay is unreachable: neutral error,
    nothing bound, stays celerp_direct."""
    import httpx

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.ensure_instance_id", return_value="iid-1"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("no route"))
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" in r.json()
    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


@pytest.mark.asyncio
async def test_partner_claim_decline_binds_nothing():
    """Declining a claim through the UI decline route makes no relay call and
    leaves the install celerp_direct (adversarial #10: nothing binds without an
    explicit accept). The route restores the neutral claim card."""
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import patch as _patch, AsyncMock as _AsyncMock
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    with _patch("ui.api_client._api_client") as mock_api_client:
        # Any relay call would go through the API client; assert it is never entered.
        r = None
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.post(
                "/settings/partner-claim/decline",
                cookies={"celerp_token": make_test_token(role="owner")})
        assert r.status_code == 200
        # Decline is a pure client-side dismissal: no relay/API client call.
        assert mock_api_client.call_count == 0

    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"
