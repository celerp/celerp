# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the boot-time partner deployment association seam.

A partner-packaged install associates with its partner through an explicit relay
HTTP call (POST /partners/deployments/associate) before the gateway starts. The
credential is a one-time authorization input: it is preserved until a positive
response, erased in one write with the relay-issued identity on success, and
never logged. A persisted nonce makes the association idempotent across retries
and restarts.

These tests drive associate_partner_deployment() directly, stubbing the network
with a fake httpx client so no live socket is needed, except the e2e test which
drives real httpx over an ephemeral loopback server.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import threading

import httpx
import pytest

from celerp.gateway import bootstrap


_CREDENTIAL = "partner-deploy-cred-9f3a-secret"
_API_KEY = "gw-live-key-issued-by-relay"
_INSTANCE_ID = "inst-relay-created-01"


class _FakeResponse:
    """Minimal stand-in for httpx.Response with just what bootstrap reads."""

    def __init__(self, status_code: int, body, *, json_raises: bool = False) -> None:
        self.status_code = status_code
        self._body = body
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise json.JSONDecodeError("malformed", "", 0)
        return self._body


class _FakeAsyncClient:
    """Async context manager posing as httpx.AsyncClient. Records posts and returns
    a queued response, or raises a queued transport error."""

    def __init__(self, response=None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posts.append((url, json or {}))
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """Point config at a fresh tmp file and reset the deployment settings the
    bootstrap reads/writes, restoring the originals afterward."""
    from celerp.config import settings

    cfg_file = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_file))

    # Retry delays must not slow the suite down (mirrors the _try_auto_activate
    # test convention in tests/test_instance_identity.py).
    _real_sleep = asyncio.sleep

    async def _no_sleep(_delay):
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    saved = {
        k: getattr(settings, k)
        for k in (
            "deployment_credential",
            "deployment_associated",
            "cloud_disconnected",
            "deployment_nonce",
            "gateway_token",
            "gateway_instance_id",
            "gateway_http_url",
        )
    }
    settings.deployment_credential = _CREDENTIAL
    settings.deployment_associated = False
    settings.cloud_disconnected = False
    settings.deployment_nonce = ""
    settings.gateway_token = ""
    settings.gateway_instance_id = ""
    settings.gateway_http_url = "https://relay.test"
    yield settings, cfg_file
    for k, v in saved.items():
        setattr(settings, k, v)


def _install_fake_client(monkeypatch, *, response=None, raises=None):
    """Patch httpx.AsyncClient so bootstrap uses a fake and return the instance so
    the test can inspect the recorded posts."""
    fake = _FakeAsyncClient(response=response, raises=raises)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


# -- success -----------------------------------------------------------------

async def test_bootstrap_success_persists_identity_and_erases_credential(monkeypatch):
    """A 200 with instance_id + api_key persists gateway_token=api_key, instance_id,
    marks associated, erases the credential, and persists to config."""
    from celerp.config import read_config, settings

    _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )

    result = await bootstrap.associate_partner_deployment()

    assert result is True
    assert settings.gateway_token == _API_KEY
    assert settings.gateway_instance_id == _INSTANCE_ID
    assert settings.deployment_associated is True
    assert settings.deployment_credential == ""
    cloud = read_config().get("cloud", {})
    assert cloud.get("token") == _API_KEY
    assert cloud.get("instance_id") == _INSTANCE_ID
    assert cloud.get("deployment_associated") is True
    assert "deployment_credential" not in cloud


async def test_bootstrap_credential_preserved_until_positive_response(monkeypatch):
    """Before the success write the credential is intact; it is erased only as part
    of the same positive-response write, never earlier."""
    from celerp.config import settings

    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )
    assert settings.deployment_credential == _CREDENTIAL

    await bootstrap.associate_partner_deployment()

    # The credential was sent in the request body (the authorization) and only
    # cleared after the 200 was persisted.
    _url, body = fake.posts[0]
    assert body["deployment_credential"] == _CREDENTIAL
    assert settings.deployment_credential == ""


# -- failure paths -----------------------------------------------------------

@pytest.mark.parametrize("status", [403, 409, 500])
async def test_bootstrap_403_preserves_credential(monkeypatch, status):
    """A non-200 (invalid/ineligible credential, owned instance, or server error)
    leaves the credential and association untouched and returns False."""
    from celerp.config import settings

    _install_fake_client(monkeypatch, response=_FakeResponse(status, {"detail": "no"}))

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert settings.deployment_credential == _CREDENTIAL
    assert settings.deployment_associated is False
    assert settings.gateway_token == ""


async def test_bootstrap_network_failure_preserves_credential(monkeypatch):
    """A transport failure with no response preserves the credential and returns
    False so the next boot can retry."""
    from celerp.config import settings

    _install_fake_client(monkeypatch, raises=httpx.ConnectError("no route"))

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert settings.deployment_credential == _CREDENTIAL
    assert settings.deployment_associated is False


async def test_bootstrap_aborts_if_nonce_persist_fails(monkeypatch):
    """A nonce-persist failure aborts before any network call and returns False
    without raising (lifespan boot is never crashed)."""
    from celerp.config import settings

    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )

    def _boom() -> str:
        raise OSError("disk full")

    monkeypatch.setattr("celerp.gateway.bootstrap.ensure_deployment_nonce", _boom)

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert fake.posts == []  # no network call was made
    assert settings.deployment_credential == _CREDENTIAL
    assert settings.deployment_associated is False


async def test_bootstrap_survives_write_config_failure_after_200(monkeypatch):
    """A durable-persist failure after a 200 returns False without raising; the
    credential is preserved for the idempotent next-boot retry."""
    from celerp.config import settings

    _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )

    def _boom(gateway_token, instance_id) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("celerp.gateway.bootstrap.record_deployment_association", _boom)

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert settings.deployment_credential == _CREDENTIAL
    assert settings.deployment_associated is False


# -- response validation -----------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        {"instance_id": "", "api_key": _API_KEY},
        {"instance_id": _INSTANCE_ID, "api_key": ""},
        {"api_key": _API_KEY},
        {"instance_id": _INSTANCE_ID},
        {},
    ],
)
async def test_bootstrap_blank_identity_treated_as_failure(monkeypatch, body):
    """A 200 with a missing or empty instance_id/api_key is a failure: no blank
    identity is persisted and the credential is preserved."""
    from celerp.config import settings

    _install_fake_client(monkeypatch, response=_FakeResponse(200, body))

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert settings.deployment_credential == _CREDENTIAL
    assert settings.gateway_token == ""


async def test_bootstrap_malformed_response_treated_as_failure(monkeypatch):
    """A 200 whose body is not JSON is a failure; the credential is preserved."""
    from celerp.config import settings

    _install_fake_client(
        monkeypatch, response=_FakeResponse(200, None, json_raises=True)
    )

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert settings.deployment_credential == _CREDENTIAL
    assert settings.deployment_associated is False


# -- no-op guards ------------------------------------------------------------

async def test_bootstrap_noop_for_direct_install(monkeypatch):
    """A direct install (no credential) makes no association call and changes no
    settings."""
    from celerp.config import settings

    settings.deployment_credential = ""
    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert fake.posts == []
    assert settings.gateway_token == ""
    assert settings.deployment_associated is False


async def test_bootstrap_noop_when_already_associated(monkeypatch):
    """An already-associated install never re-associates, even with a credential
    still present in memory."""
    from celerp.config import settings

    settings.deployment_associated = True
    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert fake.posts == []


async def test_bootstrap_noop_when_cloud_disconnected(monkeypatch):
    """A sticky Cloud disconnect suppresses the association call."""
    from celerp.config import settings

    settings.cloud_disconnected = True
    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )

    result = await bootstrap.associate_partner_deployment()

    assert result is False
    assert fake.posts == []


# -- idempotency -------------------------------------------------------------

async def test_bootstrap_idempotent_same_nonce_across_retries(monkeypatch):
    """A lost first response then a retry reuses the SAME persisted nonce, so the
    relay resolves both attempts to the same association."""
    from celerp.config import settings

    # First attempt: transport failure, credential preserved, nonce persisted.
    _install_fake_client(monkeypatch, raises=httpx.ConnectError("lost"))
    await bootstrap.associate_partner_deployment()
    first_nonce = settings.deployment_nonce
    assert first_nonce

    # Retry: succeeds; the body carries the same nonce.
    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )
    await bootstrap.associate_partner_deployment()
    _url, body = fake.posts[0]
    assert body["deployment_nonce"] == first_nonce


async def test_bootstrap_reuses_persisted_nonce_after_restart(monkeypatch):
    """The nonce persisted on one boot is reloaded on the next and reused, so a
    restart does not create a duplicate association."""
    from celerp.config import load_cloud_config, settings

    # First boot fails at the network but persists the nonce.
    _install_fake_client(monkeypatch, raises=httpx.ConnectError("lost"))
    await bootstrap.associate_partner_deployment()
    nonce = settings.deployment_nonce
    assert nonce

    # Simulate a restart: clear in-memory nonce, reload from config.
    settings.deployment_nonce = ""
    load_cloud_config()
    assert settings.deployment_nonce == nonce


# -- secrets -----------------------------------------------------------------

async def test_bootstrap_credential_never_logged(monkeypatch, caplog):
    """Neither the credential nor the returned api_key appears in any log record,
    including the caught-transport-exception path."""
    _install_fake_client(monkeypatch, raises=httpx.ConnectError(_CREDENTIAL))
    with caplog.at_level(logging.DEBUG):
        await bootstrap.associate_partner_deployment()
    for record in caplog.records:
        msg = record.getMessage()
        assert _CREDENTIAL not in msg
        assert _API_KEY not in msg
        for arg in (record.args or ()):
            assert _CREDENTIAL not in str(arg)
            assert _API_KEY not in str(arg)


async def test_bootstrap_credential_only_in_body_never_in_url(monkeypatch):
    """The credential travels only in the JSON body, never in the request URL."""
    fake = _install_fake_client(
        monkeypatch,
        response=_FakeResponse(200, {"instance_id": _INSTANCE_ID, "api_key": _API_KEY}),
    )
    await bootstrap.associate_partner_deployment()
    url, body = fake.posts[0]
    assert _CREDENTIAL not in url
    assert url.endswith("/partners/deployments/associate")
    assert body["deployment_credential"] == _CREDENTIAL


# -- e2e over a real socket --------------------------------------------------

async def test_bootstrap_e2e_against_local_relay_stub(monkeypatch):
    """End to end over a real loopback socket: the association persists the relay
    identity, and a second call with the committed nonce resolves to the same
    instance_id (idempotent same-nonce rotation)."""
    from celerp.config import settings

    seen_nonces: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence the stub server
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            nonce = payload.get("deployment_nonce", "")
            seen_nonces.append(nonce)
            # Same nonce -> same instance, fresh api_key (idempotent rotation).
            api_key = f"key-{len(seen_nonces)}"
            body = json.dumps(
                {"instance_id": _INSTANCE_ID, "api_key": api_key}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings.gateway_http_url = f"http://127.0.0.1:{port}"

        result = await bootstrap.associate_partner_deployment()
        assert result is True
        assert settings.gateway_instance_id == _INSTANCE_ID
        committed_nonce = seen_nonces[0]
        assert committed_nonce

        # Second call with the SAME nonce the relay already committed (this models
        # a lost-response retry: the first success clears deployment_nonce, so the
        # committed value is restored explicitly here to stand in for the nonce a
        # real retry would resend from disk) - confirms the relay resolves it to
        # the same instance_id rather than creating a second one.
        settings.deployment_associated = False
        settings.deployment_credential = _CREDENTIAL
        settings.deployment_nonce = committed_nonce
        result2 = await bootstrap.associate_partner_deployment()
        assert result2 is True
        assert settings.gateway_instance_id == _INSTANCE_ID
        assert seen_nonces[1] == committed_nonce
    finally:
        server.shutdown()
        thread.join(timeout=5)


# -- handshake no longer carries the credential ------------------------------

def _make_gateway_client(*, credential: str = "", associated: bool = False):
    """Build a GatewayClient with the deployment settings the constructor reads."""
    from celerp.config import settings
    from celerp.gateway.client import GatewayClient

    settings.deployment_credential = credential
    settings.deployment_associated = associated
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


@pytest.mark.parametrize("credential", ["", _CREDENTIAL])
def test_hello_payload_never_carries_deployment_credential(credential):
    """The hello payload carries only the four base keys for both a direct install
    and a partner install: the credential is never sent through the handshake."""
    client = _make_gateway_client(credential=credential)
    payload = client._build_hello_payload(tos_version="v2025", app_version="9.9.9")
    assert payload == {
        "gateway_token": "test-gateway-token",
        "instance_id": "test-instance-id",
        "tos_version": "v2025",
        "version": "9.9.9",
    }


async def test_hello_ack_does_not_record_association(monkeypatch):
    """An ordinary hello_ack records no deployment association and sets no
    association state: the handshake never consumes the credential."""
    from celerp.config import settings

    calls = []
    monkeypatch.setattr(
        "celerp.gateway.client.record_deployment_association",
        lambda *a, **k: calls.append(True),
        raising=False,
    )
    settings.deployment_associated = False
    client = _make_gateway_client(credential=_CREDENTIAL)
    # A partner install builds its hello, then receives an ordinary hello_ack:
    # the handshake must not record any association from that ack.
    client._build_hello_payload(tos_version="", app_version="1.0.0")
    await client._dispatch(
        {"type": "hello_ack", "payload": {"instance_id": "test-instance-id"}}
    )
    assert calls == []
    assert settings.deployment_associated is False


async def test_hello_ack_still_updates_canonical_instance_id():
    """Retained-behavior guard: hello_ack still adopts the relay's canonical
    instance_id after the overload is removed (green at merge-base by design)."""
    from celerp.gateway import state as gw_state

    client = _make_gateway_client(credential="")
    await client._dispatch(
        {"type": "hello_ack", "payload": {"instance_id": "canonical-relay-id"}}
    )
    assert client._instance_id == "canonical-relay-id"
    assert gw_state.get_instance_id() == "canonical-relay-id"
