# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Connect account route regression tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _installation_owner_context():
    with patch(
        "celerp.routers.health.is_install_owner",
        new=AsyncMock(return_value=True),
    ):
        yield


def _mock_httpx(account_payload=None, auth_status=200):
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
async def test_account_status_returns_full_record_when_connected():
    factory, client = _mock_httpx({"email": "o@shop.example", "email_verified": True})
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["email"] == "o@shop.example"
    auth_call = client.post.call_args_list[0]
    assert auth_call[0][0] == "https://relay.test/auth/token"
    assert auth_call[1]["json"] == {"api_key": "api-key-123"}
    get_call = client.get.call_args_list[0]
    assert get_call[0][0] == "https://relay.test/auth/account"
    assert get_call[1]["headers"]["Authorization"] == "Bearer jwt-abc"


@pytest.mark.asyncio
async def test_account_status_preserves_masked_record_on_auth_failure():
    factory, client = _mock_httpx({"email": "o***@shop.example"}, auth_status=401)
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api(user=MagicMock(id="root"), session=MagicMock())

    assert "error" not in data
    assert data["email"] == "o***@shop.example"
    get_call = client.get.call_args_list[0]
    assert "headers" not in get_call[1] or "Authorization" not in (get_call[1].get("headers") or {})


@pytest.mark.asyncio
async def test_account_status_works_before_connection():
    factory, client = _mock_httpx({"claim_offer": True})
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api(user=MagicMock(id="root"), session=MagicMock())

    assert data == {"claim_offer": True}
    assert client.post.await_count == 0


@pytest.mark.asyncio
async def test_cloud_claim_uses_existing_connection():
    claim_resp = MagicMock()
    claim_resp.status_code = 200
    claim_resp.json.return_value = {"claimed": True}
    act_resp = MagicMock()
    act_resp.status_code = 200
    act_resp.json.return_value = {"gateway_token": "fresh-key"}
    seen = []

    async def post(url, **kwargs):
        seen.append((url, kwargs))
        return claim_resp if "billing/claim" in url else act_resp

    client = MagicMock()
    client.post = AsyncMock(side_effect=post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    with (
        patch("httpx.AsyncClient", factory),
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value="incumbent-key"),
        ),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("incumbent-jwt", "canonical-iid")),
        ),
        patch(
            "celerp.config.ensure_connect_identity",
            return_value=("canonical-iid", "approved-proof"),
        ),
        patch("celerp.config.set_cloud_disconnected"),
        patch(
            "celerp.routers.health._apply_gateway_token_api",
            new=AsyncMock(return_value=True),
        ),
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        from celerp.routers.health import cloud_claim_api
        data = await cloud_claim_api({
            "email": "paid@example.com",
            "otp_code": "123456",
        })

    assert data["connected"] is True
    claim_call = next(call for call in seen if "billing/claim" in call[0])
    assert claim_call[1]["headers"]["Authorization"] == "Bearer incumbent-jwt"
    assert claim_call[1]["headers"]["X-Instance-ID"] == "canonical-iid"

@pytest.mark.asyncio
async def test_cloud_claim_skips_bearer_without_gateway_token():
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
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.ensure_connect_identity", return_value=("i-1", "verifier")),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
        patch("celerp.gateway.state.activate_payload", return_value={}),
    ):
        from celerp.routers.health import cloud_claim_api
        await cloud_claim_api({"email": "o@shop.example", "otp_code": "111222"})

    claim_call = next(c for c in client.post.call_args_list if c[0][0].endswith("/billing/claim"))
    assert "Authorization" not in claim_call[1]["headers"]
    assert not any(c[0][0].endswith("/auth/token") for c in client.post.call_args_list)


@pytest.mark.asyncio
async def test_activate_404_message_points_to_link_subscription_field():
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
        patch("celerp.config.settings.activation_verifier", "test-verifier"),
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
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert data["free_email_quota"] == 10

    client.get = AsyncMock(side_effect=ConnectionError("down"))
    with (
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert data["free_email_quota"] == 0


@pytest.mark.asyncio
async def test_account_methods_returns_account_switch_url_when_connected():
    def _get_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/methods"):
            resp.json = MagicMock(return_value={"google": True, "free_email_quota": 5})
        elif url.endswith("/auth/google/start-url"):
            assert kw["headers"]["Authorization"] == "Bearer jwt-abc"
            resp.json = MagicMock(return_value={
                "url": "https://accounts.google.com/o/oauth2/v2/auth?state=signed"})
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert data["google"] is True
    assert data["google_start_url"] == (
        "https://accounts.google.com/o/oauth2/v2/auth?state=signed")
    assert any(c[0][0].endswith("/auth/token") for c in client.post.call_args_list)


@pytest.mark.asyncio
async def test_account_methods_returns_signup_url_on_fresh_install():
    factory, client = _mock_httpx()
    methods_resp = MagicMock()
    methods_resp.status_code = 200
    methods_resp.json = MagicMock(return_value={"google": True, "free_email_quota": 0})
    client.get = AsyncMock(return_value=methods_resp)
    activate_resp = MagicMock()
    activate_resp.status_code = 404
    client.post = AsyncMock(return_value=activate_resp)
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.config.ensure_activation_verifier", return_value="local-secret-verifier"),
        patch("celerp.config.activation_challenge", return_value="a" * 64),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert data["google_start_url"] == (
        "https://relay.test/auth/google/start?instance_id=i-77"
        "&activation_challenge=" + "a" * 64
    )
    assert "local-secret-verifier" not in data["google_start_url"]
    assert not any(c[0][0].endswith("/auth/token") for c in client.post.call_args_list)


@pytest.mark.asyncio
async def test_account_methods_has_usable_fallback_url():
    def _get_router(url, **kw):
        resp = MagicMock()
        if url.endswith("/auth/methods"):
            resp.status_code = 200
            resp.json = MagicMock(return_value={"google": True, "free_email_quota": 0})
        else:
            resp.status_code = 404
            resp.json = MagicMock(return_value={"detail": "no such endpoint"})
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert data["google"] is True
    assert data["google_start_url"] == "https://relay.test/auth/google/start?instance_id=i-77"


@pytest.mark.asyncio
async def test_account_methods_uses_persisted_connection_state():
    activate_calls = []

    def _get_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/methods"):
            resp.json = MagicMock(return_value={"google": True, "free_email_quota": 5})
        elif url.endswith("/auth/google/start-url"):
            assert kw["headers"]["Authorization"] == "Bearer jwt-abc"
            resp.json = MagicMock(return_value={
                "url": "https://accounts.google.com/o/oauth2/v2/auth?state=signed"})
        return resp

    async def _post_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/activate"):
            activate_calls.append(url)
            resp.json = MagicMock(return_value={"gateway_token": "rotated-key"})
        elif url.endswith("/auth/token"):
            assert kw["json"] == {"api_key": "stored-key"}
            resp.json = MagicMock(return_value={"access_token": "jwt-abc"})
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    client.post = AsyncMock(side_effect=_post_router)
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.read_config",
              return_value={"cloud": {"token": "stored-key"}}),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert activate_calls == []  # no rotation, so the stored credential is not orphaned
    assert data["google_start_url"] == (
        "https://accounts.google.com/o/oauth2/v2/auth?state=signed")


@pytest.mark.asyncio
async def test_account_methods_fresh_install_uses_local_setup_state():
    posts = []

    def _get_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/methods"):
            resp.json = MagicMock(return_value={"google": True, "free_email_quota": 0})
        return resp

    async def _post_router(url, **kw):
        posts.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={})
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    client.post = AsyncMock(side_effect=_post_router)
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.read_config", return_value={"cloud": {}}),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.config.ensure_activation_verifier",
              return_value="local-secret-verifier"),
        patch("celerp.config.activation_challenge", return_value="a" * 64),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())
    assert posts == []  # no /auth/activate, no /auth/token
    assert data["google_start_url"] == (
        "https://relay.test/auth/google/start?instance_id=i-77"
        "&activation_challenge=" + "a" * 64
    )



@pytest.mark.asyncio
async def test_account_methods_stale_state_uses_recovery_flow():
    def _get_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/methods"):
            resp.json = MagicMock(return_value={
                "google": True, "free_email_quota": 0,
                "secure_activation": True,
            })
        return resp

    token_resp = MagicMock()
    token_resp.status_code = 401

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    client.post = AsyncMock(return_value=token_resp)
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.read_config",
              return_value={"cloud": {"token": "stale-key"}}),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.config.ensure_activation_verifier",
              return_value="recovery-verifier"),
        patch("celerp.config.activation_challenge", return_value="b" * 64),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["google"] is True
    assert data["google_start_url"] == (
        "https://relay.test/auth/google/start?instance_id=i-77"
        "&activation_challenge=" + "b" * 64
    )
    assert "recovery-verifier" not in data["google_start_url"]


@pytest.mark.asyncio
async def test_account_methods_auth_failure_does_not_weaken_flow():
    methods = MagicMock()
    methods.status_code = 200
    methods.json = MagicMock(return_value={
        "google": True, "free_email_quota": 0, "secure_activation": True})
    token_resp = MagicMock()
    token_resp.status_code = 500

    factory, client = _mock_httpx()
    client.get = AsyncMock(return_value=methods)
    client.post = AsyncMock(return_value=token_resp)
    with (
        patch("celerp.config.settings.gateway_token", "stored-key"),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["google"] is False
    assert data["google_start_url"] == ""
    assert len(client.post.call_args_list) == 1
    assert client.post.call_args_list[0][0][0].endswith("/auth/token")


@pytest.mark.asyncio
async def test_account_methods_uses_single_timeout_budget():
    methods = MagicMock()
    methods.status_code = 200
    methods.json = MagicMock(return_value={
        "google": True, "free_email_quota": 0, "secure_activation": True})
    token = MagicMock()
    token.status_code = 200
    token.json = MagicMock(return_value={"access_token": "jwt-abc"})
    start = MagicMock()
    start.status_code = 200
    start.json = MagicMock(return_value={"url": "https://accounts.google.test/start"})

    async def _slow_get(url, **kw):
        await asyncio.sleep(0.03)
        return methods if url.endswith("/auth/methods") else start

    async def _slow_post(url, **kw):
        await asyncio.sleep(0.03)
        return token

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_slow_get)
    client.post = AsyncMock(side_effect=_slow_post)
    with (
        patch("celerp.config.settings.gateway_token", "stored-key"),
        patch("celerp.config.ensure_instance_id", return_value="i-77"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("celerp.routers.health.RELAY_ACCOUNT_METHODS_TIMEOUT", 0.05),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["google"] is False
    assert data["google_start_url"] == ""


def test_magic_link_timeout_hierarchy_is_outer_to_inner():
    """UI must outlive local proxy, which must outlive the cloud's bounded
    five-second provider call, so callers never report failure first."""
    from celerp.routers import health
    from ui import api_client
    assert api_client.ACCOUNT_SIGNUP_TIMEOUT > health.RELAY_ACCOUNT_SIGNUP_TIMEOUT > 5.0
    assert api_client.ACCOUNT_METHODS_TIMEOUT > health.RELAY_ACCOUNT_METHODS_TIMEOUT
    assert api_client.ACCOUNT_METHODS_TIMEOUT >= (
        health.RELAY_ACCOUNT_METHODS_TIMEOUT + 6.0
    )



@pytest.mark.asyncio
async def test_account_status_identity_mismatch_keeps_local_destination():
    factory, client = _mock_httpx({"email": "l***@shop.example"})
    with (
        patch("celerp.config.settings.gateway_token", "foreign-key"),
        patch("celerp.config.ensure_instance_id", return_value="local-iid"),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("foreign-jwt", "foreign-iid")),
        ),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_status_api
        data = await account_status_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["email"] == "l***@shop.example"
    get_call = client.get.call_args_list[0]
    assert get_call[1]["params"] == {"instance_id": "local-iid"}
    assert "Authorization" not in get_call[1]["headers"]


@pytest.mark.asyncio
async def test_account_methods_identity_mismatch_uses_local_recovery():
    def _get_router(url, **_kw):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={
            "google": True, "free_email_quota": 0, "secure_activation": True,
        })
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    with (
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value="foreign-key"),
        ),
        patch("celerp.config.ensure_instance_id", return_value="local-iid"),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("foreign-jwt", "foreign-iid")),
        ),
        patch("celerp.config.ensure_activation_verifier", return_value="local-verifier"),
        patch("celerp.config.activation_challenge", return_value="local-challenge"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["google"] is True
    assert data["google_start_url"] == (
        "https://relay.test/auth/google/start?instance_id=local-iid"
        "&activation_challenge=local-challenge"
    )
    assert [call[0][0] for call in client.get.call_args_list] == [
        "https://relay.test/auth/methods"
    ]


@pytest.mark.asyncio
async def test_account_methods_matching_identity_uses_account_switch_flow():
    def _get_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/methods"):
            resp.json = MagicMock(return_value={
                "google": True, "free_email_quota": 5, "secure_activation": True,
            })
        else:
            assert url.endswith("/auth/google/start-url")
            assert kw["params"] == {"instance_id": "local-iid"}
            assert kw["headers"]["Authorization"] == "Bearer local-jwt"
            resp.json = MagicMock(return_value={
                "url": "https://accounts.google.test/start"
            })
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    live = MagicMock()
    live.ownership_conflict = False
    with (
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value="local-key"),
        ),
        patch("celerp.config.settings.activation_verifier", "stale-verifier"),
        patch("celerp.config.ensure_instance_id", return_value="local-iid"),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("local-jwt", "local-iid")),
        ),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.config.activation_challenge",
              side_effect=AssertionError("ordinary account switch must not rotate credentials")),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["google_start_url"] == "https://accounts.google.test/start"


@pytest.mark.asyncio
async def test_account_methods_takeover_carries_fresh_activation_challenge():
    def _get_router(url, **kw):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/auth/methods"):
            resp.json = MagicMock(return_value={
                "google": True, "free_email_quota": 0, "secure_activation": True,
            })
        else:
            assert url.endswith("/auth/google/start-url")
            assert kw["params"] == {
                "instance_id": "local-iid",
                "activation_challenge": "fresh-challenge",
            }
            assert kw["headers"]["Authorization"] == "Bearer local-jwt"
            resp.json = MagicMock(return_value={
                "url": "https://accounts.google.test/takeover"
            })
        return resp

    factory, client = _mock_httpx()
    client.get = AsyncMock(side_effect=_get_router)
    live = MagicMock()
    live.ownership_conflict = True
    with (
        patch(
            "celerp.services.cloud_entitlement.stored_api_key",
            new=AsyncMock(return_value="local-key"),
        ),
        patch("celerp.config.settings.activation_verifier", "fresh-verifier"),
        patch("celerp.config.ensure_instance_id", return_value="local-iid"),
        patch("celerp.config.activation_challenge",
              return_value="fresh-challenge"),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("local-jwt", "local-iid")),
        ),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.routers.health import account_methods_api
        data = await account_methods_api(user=MagicMock(id="root"), session=MagicMock())

    assert data["google_start_url"] == "https://accounts.google.test/takeover"


@pytest.mark.asyncio
async def test_authenticated_request_suppresses_foreign_key_while_local_verifier_pending():
    from celerp.config import settings
    from celerp.services.cloud_entitlement import authenticated_request

    client = MagicMock()
    client.request = AsyncMock()

    async def _run(_timeout, operation):
        return await operation(client)

    with (
        patch.object(settings, "gateway_token", "foreign-key"),
        patch.object(settings, "gateway_instance_id", "local-iid"),
        patch.object(settings, "activation_verifier", "pending-verifier"),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("foreign-jwt", "foreign-iid")),
        ),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("celerp.gateway.state.with_relay_client", new=_run),
    ):
        result = await authenticated_request("GET", "/billing/subscription")

    assert result is None
    client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticated_request_allows_incumbent_recovery_without_pending_verifier():
    from celerp.config import settings
    from celerp.services.cloud_entitlement import authenticated_request

    response = MagicMock()
    client = MagicMock()
    client.request = AsyncMock(return_value=response)

    async def _run(_timeout, operation):
        return await operation(client)

    with (
        patch.object(settings, "gateway_token", "incumbent-key"),
        patch.object(settings, "gateway_instance_id", "local-iid"),
        patch.object(settings, "activation_verifier", ""),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("incumbent-jwt", "canonical-iid")),
        ),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("celerp.gateway.state.with_relay_client", new=_run),
    ):
        result = await authenticated_request("GET", "/billing/subscription")

    assert result is response
    client.request.assert_awaited_once_with(
        "GET",
        "https://relay.test/billing/subscription",
        json=None,
        params=None,
        headers={"Authorization": "Bearer incumbent-jwt"},
    )


@pytest.mark.asyncio
async def test_authenticated_request_accepts_local_key_while_local_verifier_pending():
    from celerp.config import settings
    from celerp.services.cloud_entitlement import authenticated_request

    response = MagicMock()
    client = MagicMock()
    client.request = AsyncMock(return_value=response)

    async def _run(_timeout, operation):
        return await operation(client)

    with (
        patch.object(settings, "gateway_token", "local-key"),
        patch.object(settings, "gateway_instance_id", "local-iid"),
        patch.object(settings, "activation_verifier", "pending-verifier"),
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("local-jwt", "local-iid")),
        ),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("celerp.gateway.state.with_relay_client", new=_run),
    ):
        result = await authenticated_request("GET", "/billing/subscription")

    assert result is response
    client.request.assert_awaited_once()
