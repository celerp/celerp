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

import uuid


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _role_headers(client, role: str) -> dict:
    """Create a DB-valid v2 session for the requested company role."""
    suffix = uuid.uuid4().hex
    owner_email = f"claim-owner-{suffix}@test.local"
    reg = await client.post(
        "/auth/register",
        json={"company_name": "Claim Test Co", "email": owner_email,
              "name": "Claim Owner", "password": "pw123456"},
    )
    assert reg.status_code == 200, reg.text
    owner_headers = _h(reg.json()["access_token"])
    if role == "owner":
        return owner_headers

    email = f"claim-{role}-{suffix}@test.local"
    created = await client.post(
        "/companies/me/users",
        headers=owner_headers,
        json={"email": email, "name": role.title(), "role": role,
              "password": "pw123456"},
    )
    assert created.status_code == 200, created.text
    login = await client.post(
        "/auth/login", json={"email": email, "password": "pw123456"})
    assert login.status_code == 200, login.text
    return _h(login.json()["access_token"])


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


def _accept_with_ctx(version=1, mode="partner_managed"):
    """An accept 200 that also carries the authoritative post-accept commercial
    context, as the relay now returns it."""
    ctx = {"version": version, "schema_version": 1, "commercial_mode": mode}
    if mode == "partner_managed":
        ctx["implementation"] = {
            "partner_id": "prt_123", "display_name": "Acme Partners",
            "support_url": "https://acme.example.com/support",
        }
    return {"partner_id": "prt_123", "commercial_context": ctx}


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
        r = await client.post(path, headers=await _role_headers(client, role), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            "/settings/partner-claim/resolve", headers=await _role_headers(client, "owner"), json=body)
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
            headers=await _role_headers(client, "owner"), json={"claim_token": oversized})
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
            path, headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-accept"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-dup"})
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
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-abc"})
    assert r.status_code == 200
    assert "error" in r.json()
    from celerp.gateway.state import get_commercial_mode
    assert get_commercial_mode() == "celerp_direct"


# -- accept: synchronous commercial-state convergence ------------------------

@pytest.fixture
def _reset_ctx():
    import celerp.gateway.state as gw_state
    saved = gw_state._commercial_context
    gw_state._commercial_context = {}
    yield
    gw_state._commercial_context = saved


@pytest.mark.asyncio
async def test_partner_claim_accept_converges_without_live_ws(client, _reset_ctx, monkeypatch):
    """Accept applies the returned commercial_context synchronously, so the local
    mode is partner_managed on return even with no WS push (self-hosted, so no
    packaged data dir)."""
    import celerp.gateway.state as gw_state
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    monkeypatch.setenv("CELERP_CONFIG", "/tmp/celerp-claim-accept-test.toml")
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", "api-key-abc"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(200, _accept_with_ctx(version=2)))
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-accept"})
    assert r.status_code == 200
    data = r.json()
    assert data["partner_id"] == "prt_123"
    # Local state converged before the response was returned.
    assert gw_state.get_commercial_mode() == "partner_managed"


@pytest.mark.asyncio
async def test_partner_claim_accept_ws_first_then_http_converges(client, _reset_ctx, monkeypatch):
    """The benign race: the WS already applied the same/newer valid version before
    the HTTP response. That is converged success, not a failure."""
    import celerp.gateway.state as gw_state
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    monkeypatch.setenv("CELERP_CONFIG", "/tmp/celerp-claim-accept-test2.toml")
    # Simulate the WS having applied the same version already.
    gw_state.apply_commercial_context(_accept_with_ctx(version=2)["commercial_context"])
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", "api-key-abc"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(200, _accept_with_ctx(version=2)))
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-accept"})
    assert r.status_code == 200
    data = r.json()
    assert data["partner_id"] == "prt_123"
    assert "error" not in data
    assert gw_state.get_commercial_mode() == "partner_managed"


@pytest.mark.asyncio
async def test_partner_claim_accept_malformed_ctx_never_overwrites(client, _reset_ctx, monkeypatch):
    """A malformed returned context does not overwrite last-known-good and is a
    failure surfaced to the caller."""
    import celerp.gateway.state as gw_state
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    monkeypatch.setenv("CELERP_CONFIG", "/tmp/celerp-claim-accept-test3.toml")
    # Last-known-good: an earlier valid partner context at version 5.
    gw_state.apply_commercial_context(_accept_with_ctx(version=5)["commercial_context"])
    malformed = {"partner_id": "prt_123", "commercial_context": {
        "version": 9, "schema_version": 1, "commercial_mode": "partner_managed"}}  # no implementation
    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.config.settings.gateway_token", "api-key-abc"),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = _relay_post_mock(
            _relay_resp(200, malformed))
        r = await client.post(
            "/settings/partner-claim/accept",
            headers=await _role_headers(client, "owner"), json={"claim_token": "tok-accept"})
    assert r.status_code == 200
    data = r.json()
    assert "error" in data
    assert "partner_id" not in data
    # Last-known-good preserved: version unchanged at 5.
    assert gw_state.get_commercial_context()["version"] == 5


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
            patch("ui.api_client.get_company", new=AsyncMock(return_value={
                "current_role": "owner", "settings": {}})),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=ui_app), base_url="http://ui",
                follow_redirects=False,
            ) as c:
                r = await c.get(
                    "/settings/cloud?tab=partner",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("relay_status,tier,public_url,token_bound", [
    ("inactive", "free", None, True),
    ("active", "cloud", "https://direct.celerp.com", True),
])
async def test_partner_claim_hidden_once_direct_install_is_connected(
    relay_status, tier, public_url, token_bound,
):
    """Connected direct customers never see partner adoption, including free tier;
    a stale ?tab=partner URL safely falls back to the normal connected status view."""
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    neutral_infra = {
        "in_grace": False,
        "has_external_url": False,
        "external_db_entitled": False,
        "storage_in_grace": False,
        "has_external_storage": False,
        "external_storage_entitled": False,
    }
    with (
        patch("ui.routes.settings_cloud._relay_state", new=AsyncMock(return_value=(
            relay_status, public_url, tier, False, True, token_bound,
        ))),
        patch("ui.routes.settings_cloud._commercial_state", new=AsyncMock(return_value={})),
        patch("ui.routes.settings_cloud._check_permission", new=AsyncMock(return_value=None)),
        patch("ui.routes.settings_cloud._get_role", return_value="owner"),
        patch("ui.api_client.get_billing_catalog", new=AsyncMock(return_value={})),
        patch("ui.api_client.get_backup_status", new=AsyncMock(return_value={
            "db": {}, "next_db_utc": None, "public_url": public_url or "",
        })),
        patch("celerp.gateway.state.get_commercial_mode", return_value="celerp_direct"),
        patch("celerp.gateway.state.get_local_infra_state", return_value=neutral_infra),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.get(
                "/settings/cloud?tab=partner",
                cookies={"celerp_token": make_test_token(role="owner")},
            )
    assert r.status_code == 200
    assert 'id="partner-claim-card"' not in r.text
    assert 'href="/settings/cloud?tab=partner"' not in r.text


# -- unconnected Web Access recovery invariant -------------------------------

_UNCONNECTED_WEB_ACCESS_STATES = [
    (relay_status, disconnected, token_bound, entitlement_known)
    for relay_status in ("inactive", "active", "tos_required", "connecting", "error")
    for disconnected in (False, True)
    for token_bound in (False, True)
    # Unknown entitlement is the conservative state that exercises every
    # unconnected path, including a stale preserved credential.
    for entitlement_known in (False,)
    if not (
        not disconnected
        and (
            relay_status in ("active", "tos_required", "connecting", "error")
            or (token_bound and entitlement_known)
        )
    )
]


def test_unconnected_recovery_matrix_includes_stale_preserved_credential():
    """Keep the exact stale-token state in the route invariant permanently."""
    assert len(_UNCONNECTED_WEB_ACCESS_STATES) == 12
    assert ("inactive", False, True, False) in _UNCONNECTED_WEB_ACCESS_STATES


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize(
    "relay_status,disconnected,token_bound,entitlement_known",
    _UNCONNECTED_WEB_ACCESS_STATES,
)
async def test_unconnected_direct_owner_admin_always_has_subscription_recovery(
    role, relay_status, disconnected, token_bound, entitlement_known,
):
    """Every normal unconnected direct-customer Web Access view retains an
    explicit subscription recovery action, regardless of how it became
    unconnected. Partner adoption is available only as a separate tab and never
    replaces the normal Connect/Link-subscription surface."""
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    with (
        patch("ui.routes.settings_cloud._relay_state", new=AsyncMock(return_value=(
            relay_status, "", "", disconnected, token_bound, entitlement_known,
        ))),
        patch("ui.routes.settings_cloud._check_permission", new=AsyncMock(return_value=None)),
        patch("ui.routes.settings_cloud._get_role", return_value=role),
        patch("ui.api_client.get_billing_catalog", new=AsyncMock(return_value={})),
        patch("celerp.gateway.state.get_commercial_mode", return_value="celerp_direct"),
        patch("celerp.config.ensure_instance_id", return_value="instance-recovery-test"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.get(
                "/settings/cloud",
                cookies={"celerp_token": make_test_token(role=role)},
            )

    assert r.status_code == 200
    has_auto_connect = 'id="cloud-connect-btn"' in r.text
    has_link_subscription = 'hx-post="/settings/cloud-send-otp"' in r.text
    assert has_auto_connect or has_link_subscription

    # Partner claiming never displaces or co-renders inside the normal recovery
    # surface. Eligible owner/admin users reach it only through its own tab.
    assert 'id="partner-claim-card"' not in r.text
    assert 'href="/settings/cloud?tab=partner"' in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("entitlement_known", [False, True])
async def test_terminal_gateway_error_always_has_recovery(
    role, entitlement_known,
):
    """The full Web Access route propagates entitlement state into a terminal
    gateway error: retry is always present, Link Subscription is added only
    when account authority is unknown, and partner adoption stays isolated."""
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    tier = "cloud" if entitlement_known else ""
    neutral_infra = {
        "in_grace": False,
        "has_external_url": False,
        "external_db_entitled": False,
        "storage_in_grace": False,
        "has_external_storage": False,
        "external_storage_entitled": False,
    }
    with (
        patch("ui.routes.settings_cloud._relay_state", new=AsyncMock(return_value=(
            "error", "", tier, False, True, entitlement_known,
        ))),
        patch("ui.routes.settings_cloud._commercial_state", new=AsyncMock(return_value={})),
        patch("ui.routes.settings_cloud._check_permission", new=AsyncMock(return_value=None)),
        patch("ui.routes.settings_cloud._get_role", return_value=role),
        patch("ui.api_client.get_billing_catalog", new=AsyncMock(return_value={})),
        patch("ui.api_client.get_backup_status", new=AsyncMock(return_value={})),
        patch("celerp.gateway.state.get_commercial_mode", return_value="celerp_direct"),
        patch("celerp.gateway.state.get_local_infra_state", return_value=neutral_infra),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.get(
                "/settings/cloud",
                cookies={"celerp_token": make_test_token(role=role)},
            )

    assert r.status_code == 200
    assert "Connection failed" in r.text
    assert 'id="cloud-connect-btn"' in r.text
    assert 'hx-post="/settings/cloud-disconnect"' in r.text
    assert ("Link subscription" in r.text) is (not entitlement_known)
    assert 'id="partner-claim-card"' not in r.text
    assert 'href="/settings/cloud?tab=partner"' not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_inactive_known_paid_account_has_reconnect_not_partner_claim(role):
    """A paid account that remains inactive after status self-heal has retry
    rather than a Disconnect-only dead end, without reopening partner adoption."""
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    neutral_infra = {
        "in_grace": False,
        "has_external_url": False,
        "external_db_entitled": False,
        "storage_in_grace": False,
        "has_external_storage": False,
        "external_storage_entitled": False,
    }
    with (
        patch("ui.routes.settings_cloud._relay_state", new=AsyncMock(return_value=(
            "inactive", "", "cloud", False, True, True,
        ))),
        patch("ui.routes.settings_cloud._commercial_state", new=AsyncMock(return_value={})),
        patch("ui.routes.settings_cloud._check_permission", new=AsyncMock(return_value=None)),
        patch("ui.routes.settings_cloud._get_role", return_value=role),
        patch("ui.api_client.get_billing_catalog", new=AsyncMock(return_value={})),
        patch("ui.api_client.get_backup_status", new=AsyncMock(return_value={})),
        patch("celerp.gateway.state.get_commercial_mode", return_value="celerp_direct"),
        patch("celerp.gateway.state.get_local_infra_state", return_value=neutral_infra),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app), base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.get(
                "/settings/cloud",
                cookies={"celerp_token": make_test_token(role=role)},
            )

    assert r.status_code == 200
    assert "Initializing connection" in r.text
    assert 'id="cloud-connect-btn"' in r.text
    assert 'hx-post="/settings/cloud-disconnect"' in r.text
    assert 'id="partner-claim-card"' not in r.text
    assert 'href="/settings/cloud?tab=partner"' not in r.text
