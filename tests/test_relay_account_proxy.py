# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the /settings/account-status proxy's relay authentication.

Once an instance holds a gateway token, the status proxy exchanges it for a
bearer JWT (the connectors-catalog idiom) so the relay can return the full
account record to the instance that owns it. Without a token, or when the
exchange fails, the proxy falls back to the unauthenticated GET.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_httpx(account_payload=None, auth_status=200):
    """AsyncClient factory mock: /auth/token exchange plus GET /auth/account."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json = MagicMock(return_value=account_payload or {"email": "o@shop.example"})
    auth_resp = MagicMock()
    auth_resp.status_code = auth_status
    auth_resp.json = MagicMock(return_value={"access_token": "jwt-abc"})

    async def _post(url, **kw):
        return auth_resp

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    client.get = AsyncMock(return_value=get_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx), client


@pytest.mark.asyncio
async def test_account_status_proxy_sends_bearer_when_activated():
    """With a gateway token, the proxy exchanges it and sends the JWT on the
    account GET, so the relay can serve the unmasked record."""
    factory, client = _mock_httpx({"email": "o@shop.example", "email_verified": True})
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api()

    assert data["email"] == "o@shop.example"
    auth_call = client.post.call_args_list[0]
    assert auth_call[0][0] == "https://relay.test/auth/token"
    assert auth_call[1]["json"] == {"api_key": "api-key-123"}
    get_call = client.get.call_args_list[0]
    assert get_call[0][0] == "https://relay.test/auth/account"
    assert get_call[1]["headers"]["Authorization"] == "Bearer jwt-abc"


@pytest.mark.asyncio
async def test_account_status_proxy_falls_back_when_exchange_fails():
    """A failed token exchange degrades to the unauthenticated GET - polling
    keeps working on the masked record instead of erroring."""
    factory, client = _mock_httpx({"email": "o***@shop.example"}, auth_status=401)
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api()

    assert "error" not in data
    assert data["email"] == "o***@shop.example"
    get_call = client.get.call_args_list[0]
    assert "headers" not in get_call[1] or "Authorization" not in (get_call[1].get("headers") or {})


@pytest.mark.asyncio
async def test_account_status_proxy_skips_exchange_without_token():
    """No gateway token (not yet activated): one bare GET, no exchange."""
    factory, client = _mock_httpx({"claim_offer": True})
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api()

    assert data == {"claim_offer": True}
    assert client.post.await_count == 0


@pytest.mark.asyncio
async def test_cloud_claim_sends_only_instance_id_no_extra_proof():
    """The claim proxy sends X-Instance-ID and the email/OTP payload only -
    the OTP is relay's entire authority to bind or move a subscription, so
    no other credential is exchanged or attached here."""
    claim_resp = MagicMock()
    claim_resp.status_code = 200
    claim_resp.json = MagicMock(return_value={"tier": "cloud", "status": "active"})
    client = MagicMock()
    client.post = AsyncMock(return_value=claim_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    with (
        patch("celerp.config.ensure_instance_id", return_value="i-1"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
        patch("celerp.gateway.state.activate_payload", return_value={}),
    ):
        from celerp.routers.health import cloud_claim_api
        await cloud_claim_api({"email": "o@shop.example", "otp_code": "111222"})

    claim_call = next(c for c in client.post.call_args_list if c[0][0].endswith("/billing/claim"))
    assert "Authorization" not in claim_call[1]["headers"]
    assert claim_call[1]["headers"]["X-Instance-ID"] == "i-1"
    assert not any(c[0][0].endswith("/auth/token") for c in client.post.call_args_list)


@pytest.mark.asyncio
async def test_activate_404_message_points_to_link_subscription_field():
    """A 404 from relay /auth/activate means no subscription is bound to this
    instance yet. The message must not repeat itself or tell the user to
    call an API endpoint directly (/billing/claim) - it should point at the
    on-page Link Subscription field instead."""
    resp = MagicMock()
    resp.status_code = 404
    resp.json = MagicMock(return_value={"detail": "No subscription found for this instance_id."})
    resp.text = "not used"

    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    with (
        patch("celerp.config.settings.gateway_instance_id", "bc25a5d4-9b2e-465c-be50-3e491914795e"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import cloud_activate_api
        data = await cloud_activate_api()

    assert data["instance_id"] == "bc25a5d4-9b2e-465c-be50-3e491914795e"
    assert data["error"] == (
        "No active subscription found for this instance "
        "(bc25a5d4-9b2e-465c-be50-3e491914795e). Complete checkout first, "
        "or if you need to move your subscription to this instance, use "
        "the Link Subscription field below."
    )
    assert "/billing/claim" not in data["error"]


@pytest.mark.asyncio
async def test_account_methods_proxy_reports_free_email_quota():
    """The methods proxy passes through the relay's advertised free email
    quota, defaulting to 0 when absent or unreachable."""
    factory, _ = _mock_httpx()
    methods_resp = MagicMock()
    methods_resp.status_code = 200
    methods_resp.json = MagicMock(return_value={"google": True, "free_email_quota": 10})
    ctx = factory.return_value
    client = await ctx.__aenter__()
    client.get = AsyncMock(return_value=methods_resp)
    with (
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api()
    assert data["free_email_quota"] == 10

    client.get = AsyncMock(side_effect=ConnectionError("down"))
    with (
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api()
    assert data["free_email_quota"] == 0
