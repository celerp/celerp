from pathlib import Path
import re


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text()
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{path}: regex matched {n} times")
    p.write_text(new)


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


path = "tests/test_routers/test_cloud_relay.py"

# The relay's historical reconnect flag is no longer part of the local API
# contract. A successful activation converges local state immediately.
regex_once(
    path,
    r'@pytest\.mark\.asyncio\nasync def test_cloud_activate_reconnect_flow\(client\):.*?(?=\n\n@pytest\.mark\.asyncio\nasync def test_cloud_activate_relay_unreachable)',
    r'''@pytest.mark.asyncio
async def test_cloud_activate_applies_authoritative_activation(client):
    """A proof/legacy activation result is persisted and reported connected."""
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
        "reconnect": True,  # advisory relay field is intentionally ignored locally
    }

    with (
        patch("httpx.AsyncClient") as mock_httpx,
        patch("celerp.gateway.client.get_client", return_value=None),
    ):
        mock_httpx.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        r = await client.post("/settings/cloud-activate", headers=_h(token))

    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is True
    assert data["public_url"] == "https://old.celerp.app"
    assert "instance_id" in data
    assert _s.gateway_token == "gw-authoritative"
    assert _s.celerp_public_url == "https://old.celerp.app"''',
)

# Send only the public challenge. The verifier remains local and is never placed
# in the email/relay proof request body.
replace_once(
    path,
    '''    # The relay receives exactly the entered email and this install's instance_id.\n    assert sent_payload == {"email": "user@example.com", "instance_id": ensure_instance_id()}\n    assert sent_payload["instance_id"] == data.get("instance_id")\n''',
    '''    assert sent_payload["email"] == "user@example.com"\n    assert sent_payload["instance_id"] == ensure_instance_id()\n    assert sent_payload["instance_id"] == data.get("instance_id")\n    challenge = sent_payload.get("activation_challenge", "")\n    assert len(challenge) == 64\n    int(challenge, 16)\n    assert "activation_verifier" not in sent_payload\n''',
)

# The local UI deadline must outlast the claim leg plus the bounded inline
# activation attempt. There is no background activation wait anymore.
regex_once(
    path,
    r'def test_ui_claim_deadline_exceeds_api_wait\(\):.*?(?=\n\n@pytest\.mark\.asyncio\nasync def test_ui_send_otp_uses_its_own_deadline)',
    r'''def test_ui_claim_deadline_exceeds_api_wait():
    """UI deadline outlasts the two bounded relay operations plus overhead."""
    from celerp.routers import health as health_router
    from ui import api_client

    assert api_client.CLAIM_TIMEOUT >= (
        health_router.RELAY_CLAIM_TIMEOUT
        + health_router.CLAIM_ACTIVATE_TIMEOUT
        + 1.0
    )''',
)

# A completed claim followed by a stalled activation returns linked and cancels
# the activation attempt. Nothing survives in the background to rotate/apply a
# credential later; retry/restart uses the durable verifier instead.
regex_once(
    path,
    r'@pytest\.mark\.asyncio\nasync def test_cloud_claim_answers_linked_while_activation_continues\(client\):.*?(?=\n\n@pytest\.mark\.asyncio\nasync def test_cloud_claim_relay_timeout_names_the_restart_path)',
    r'''@pytest.mark.asyncio
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
    assert calls == 2''',
)

# The old reconnect contract rotated a credential. Established reconnect now
# proves the existing API key via /auth/token and keeps that exact credential
# while the relay synchronises entitlement/public URL.
regex_once(
    path,
    r'@pytest\.mark\.asyncio\nasync def test_cloud_activate_reconnect_resyncs_via_relay\(client\):.*?(?=\n\n@pytest\.mark\.asyncio)',
    r'''@pytest.mark.asyncio
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
    assert applied.await_args.kwargs["public_url"] == "https://paid.celerp.app"''',
)
