# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for the existing-install partner-claim API endpoints.

POST /settings/partner-claim/resolve previews the partner identity behind a
claim token (no binding). POST /settings/partner-claim/accept triggers the relay
bind. Both are owner/admin only, validate the claim token at the function
boundary before any relay call, exchange the instance credential for a relay
bearer before the claim POST, and degrade honestly when the relay is unreachable
or refuses the token. All relay HTTP calls are mocked so tests run offline.
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


_BEARER = _relay_resp(200, {"access_token": "relay-jwt-xyz"})

_RESOLVE_OK = {
    "partner_id": "prt_123",
    "display_name": "Acme Partners",
    "support_email": "help@acme.example.com",
    "support_url": "https://acme.example.com/support",
}

_ACCEPT_OK = {"partner_id": "prt_123"}


def _relay_post_mock(*results):
    """Build an AsyncMock for the shared relay client's .post that returns the
    bearer exchange first, then each supplied claim response in order.

    Every claim call is preceded by an /auth/token exchange on the same client,
    so the first result feeds that exchange and the rest feed the claim POSTs.
    """
    return AsyncMock(side_effect=[_BEARER, *results])


def _patch_identity():
    """Patch the pieces that let the routes reach the relay with a real identity:
    a present gateway_token (so the no-identity guard passes)."""
    return patch("celerp.config.settings.gateway_token", "api-key-abc")


# -- authorization -----------------------------------------------------------

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
    assert mock_httpx.return_value.__aenter__.return_value.post.await_count == 0


# -- resolve: contract identity ----------------------------------------------

@pytest.mark.asyncio
async def test_partner_claim_resolve_maps_contract_identity(client):
    """A resolve 200 maps the relay contract shape to a display identity carrying
    display_name and support fields; nothing is bound."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(200, _RESOLVE_OK))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert data["display_name"] == "Acme Partners"
    assert data["partner_id"] == "prt_123"
    assert data["support_email"] == "help@acme.example.com"
    assert data["support_url"] == "https://acme.example.com/support"
    # A resolve binds nothing: the local commercial mode is untouched.
    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


@pytest.mark.asyncio
async def test_partner_claim_resolve_body_is_token_only_with_bearer(client):
    """The relay resolve call sends body {"token": ...} (no instance_id/claim_token),
    an Authorization: Bearer header from the credential exchange, and hits the
    /partners/claims/resolve path."""
    post_mock = _relay_post_mock(_relay_resp(200, _RESOLVE_OK))
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = post_mock
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    # First call is the /auth/token exchange, second is the claim POST.
    assert post_mock.await_count == 2
    exchange_args, claim_args = post_mock.await_args_list
    exchange_url = exchange_args.args[0]
    assert exchange_url.endswith("/auth/token")
    claim_url = claim_args.args[0]
    assert claim_url.endswith("/partners/claims/resolve")
    sent = claim_args.kwargs.get("json", {})
    assert sent == {"token": "tok-abc"}
    assert "instance_id" not in sent
    assert "claim_token" not in sent
    headers = claim_args.kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer relay-jwt-xyz"


@pytest.mark.asyncio
async def test_partner_claim_drops_unsafe_support_url(client):
    """A hostile/non-https support_url is dropped, never carried through to a link."""
    hostile = dict(_RESOLVE_OK, support_url="javascript:alert(1)")
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(200, hostile))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert data["display_name"] == "Acme Partners"
    # The unsafe URL is sanitised to empty, never surfaced as an href value.
    assert data.get("support_url", "") == ""
    assert "javascript" not in str(data)


@pytest.mark.asyncio
async def test_partner_claim_resolve_rejects_malformed_identity_payload(client):
    """A resolve 200 missing/wrong-typed display_name is could-not-verify, never
    partially rendered or fabricated."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(200, {"partner_id": "prt_123", "display_name": 123}))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "display_name" not in data


# -- resolve: honest degradation ---------------------------------------------

@pytest.mark.asyncio
async def test_partner_claim_resolve_degrades_when_relay_unreachable(client):
    """A relay connection error yields a neutral could-not-reach message; nothing
    is bound."""
    import httpx

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("no route"))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "display_name" not in data
    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


@pytest.mark.asyncio
async def test_partner_claim_resolve_degrades_on_timeout(client):
    """A relay timeout during the exchange or claim degrades to a neutral error."""
    import httpx

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.TimeoutException("slow"))
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" in r.json()


# -- input validation --------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", ["", "   ", None])
async def test_partner_claim_resolve_rejects_empty_token(client, bad_token):
    """An empty/whitespace/missing claim token is rejected before any relay call."""
    body = {} if bad_token is None else {"claim_token": bad_token}
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock()
        r = await client.post(
            "/settings/partner-claim/resolve", headers=_h(_role_token("owner")), json=body)
    assert r.status_code == 200
    assert "error" in r.json()
    assert mock_httpx.return_value.__aenter__.return_value.post.await_count == 0


@pytest.mark.asyncio
async def test_partner_claim_resolve_rejects_oversized_token(client):
    """A claim token over the 512-char bound is rejected at the API function
    boundary, before any relay call."""
    oversized = "x" * 513
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock()
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": oversized})
    assert r.status_code == 200
    assert "error" in r.json()
    assert mock_httpx.return_value.__aenter__.return_value.post.await_count == 0


# -- no cloud identity -------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/settings/partner-claim/resolve",
    "/settings/partner-claim/accept",
])
async def test_partner_claim_requires_cloud_identity(client, path):
    """With no gateway_token, both routes return a neutral error and make zero
    relay calls (no instance credential to exchange for a bearer)."""
    post_mock = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", ""),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = post_mock
        r = await client.post(
            path, headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" in r.json()
    assert post_mock.await_count == 0


# -- bearer exchange failure -------------------------------------------------

@pytest.mark.asyncio
async def test_partner_claim_bearer_exchange_failure_degrades(client):
    """A non-200 /auth/token exchange degrades to a neutral error, and the claim
    endpoint is never called."""
    post_mock = AsyncMock(side_effect=[_relay_resp(401, {"detail": "bad key"})])
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        _patch_identity(),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = post_mock
        r = await client.post(
            "/settings/partner-claim/resolve",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" in r.json()
    # Only the exchange was attempted; no claim POST followed.
    assert post_mock.await_count == 1
    only_url = post_mock.await_args_list[0].args[0]
    assert only_url.endswith("/auth/token")


# -- accept: contract shape --------------------------------------------------

@pytest.mark.asyncio
async def test_partner_claim_accept_returns_partner_id(client):
    """Accept posts {"token": ...} with a bearer, reads partner_id from the 200,
    and surfaces neither accepted nor already_owned. gateway_token is untouched."""
    from celerp.config import settings as _s
    post_mock = _relay_post_mock(_relay_resp(200, _ACCEPT_OK))
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", "api-key-abc"),
    ):
        before = _s.gateway_token
        mock_httpx.return_value.__aenter__.return_value.post = post_mock
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-accept"})
        assert _s.gateway_token == before
    assert r.status_code == 200
    data = r.json()
    assert data["partner_id"] == "prt_123"
    assert "accepted" not in data
    assert "already_owned" not in data
    # exchange then accept; the accept body carries only the token, plus bearer.
    assert post_mock.await_count == 2
    _, claim_args = post_mock.await_args_list
    assert claim_args.args[0].endswith("/partners/claims/accept")
    assert claim_args.kwargs.get("json", {}) == {"token": "tok-accept"}
    assert claim_args.kwargs.get("headers", {}).get("Authorization") == "Bearer relay-jwt-xyz"


@pytest.mark.asyncio
async def test_partner_claim_accept_reused_token_not_acceptable(client):
    """A relay 409 (used/unacceptable token) becomes a neutral not-acceptable
    error, distinct from the generic could-not-verify message, never a success."""
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", "api-key-abc"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(409, {"detail": "claim not acceptable"}))
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=_h(_role_token("owner")), json={"claim_token": "tok-dup"})
    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "partner_id" not in data
    # The already-claimed copy is specific, not the generic verify-failure text.
    assert "no longer available" in data["error"].lower()


@pytest.mark.asyncio
async def test_partner_claim_accept_degrades_when_relay_unreachable(client):
    """Accept degrades honestly when the relay is unreachable: neutral error,
    nothing bound, stays celerp_direct."""
    import httpx

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", "api-key-abc"),
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


# -- decline: binds nothing --------------------------------------------------

@pytest.mark.asyncio
async def test_partner_claim_decline_binds_nothing():
    """Declining a claim through the UI decline route makes no relay call and
    leaves the install celerp_direct. The route restores the neutral claim card."""
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    with patch("ui.api_client._api_client") as mock_api_client:
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.post(
                "/settings/partner-claim/decline",
                cookies={"celerp_token": make_test_token(role="owner")})
        assert r.status_code == 200
        assert mock_api_client.call_count == 0

    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


# -- render gate: hidden on partner_managed ----------------------------------

@pytest.mark.asyncio
async def test_partner_claim_hidden_on_partner_managed():
    """Rendering /settings/cloud for an owner on a partner_managed install omits
    the claim-entry control (the claim-token input is absent) and shows the
    neutral managed note, on the same commercial-mode predicate at both render
    sites. Exercised in-process so the in-memory commercial context is visible.
    """
    import celerp.gateway.state as gw_state
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": {"display_name": "Partner Co",
                           "support_url": "https://partner.example.com/support"},
    }
    try:
        with (
            patch("ui.api_client.get_relay_status", new=AsyncMock(return_value={
                "connected": True, "relay_status": "active",
                "public_url": "https://abc.celerp.com", "tier": "cloud"})),
            patch("ui.api_client.get_backup_status", new=AsyncMock(return_value={
                "db": {}, "next_db_utc": None, "public_url": "https://abc.celerp.com"})),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=ui_app), base_url="http://ui",
                follow_redirects=False,
            ) as c:
                r = await c.get(
                    "/settings/cloud",
                    cookies={"celerp_token": make_test_token(role="owner")})
        assert r.status_code == 200
        # The claim-entry control is withheld: no claim-token input renders.
        assert 'name="claim_token"' not in r.text
        assert 'id="partner-claim-card"' not in r.text
        # A neutral managed note stands in its place.
        assert "managed by your implementation partner" in r.text
        assert 'id="partner-managed-note"' in r.text
    finally:
        gw_state._commercial_context = {}
