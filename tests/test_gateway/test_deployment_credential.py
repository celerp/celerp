# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the reusable partner deployment credential in the gateway handshake.

The deployment credential is a one-time registration input, distinct from the
live session credential (gateway_token). It is sent only on the first `hello`
when set and not yet associated, never re-sent once associated, never logged,
and removed from bootstrap state once a successful `hello_ack` records the
association. These tests drive the client without a live WebSocket by calling
_build_hello_payload() and _dispatch() directly.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp.gateway.client import GatewayClient


_CREDENTIAL = "partner-deploy-cred-9f3a-secret"


def _make_client(*, credential: str = "", associated: bool = False) -> GatewayClient:
    """Build a GatewayClient with the deployment settings the constructor reads.

    The constructor reads settings.deployment_credential / deployment_associated,
    so these are set on the shared Settings object before construction and
    restored by the autouse fixture.
    """
    from celerp.config import settings

    settings.deployment_credential = credential
    settings.deployment_associated = associated
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


@pytest.fixture(autouse=True)
def _reset_deployment_settings():
    from celerp.config import settings

    orig_cred = getattr(settings, "deployment_credential", "")
    orig_assoc = getattr(settings, "deployment_associated", False)
    yield
    settings.deployment_credential = orig_cred
    settings.deployment_associated = orig_assoc


# ── credential included on first hello ─────────────────────────────────────────

def test_deployment_credential_sent_on_first_hello():
    """When a credential is set and the install is not associated, the first
    hello payload carries deployment_credential exactly once."""
    client = _make_client(credential=_CREDENTIAL)
    payload = client._build_hello_payload(tos_version="", app_version="1.2.3")
    assert payload["deployment_credential"] == _CREDENTIAL
    # The four base keys are still present and unchanged.
    assert payload["gateway_token"] == "test-gateway-token"
    assert payload["instance_id"] == "test-instance-id"
    assert payload["tos_version"] == ""
    assert payload["version"] == "1.2.3"


def test_deployment_credential_suppressed_when_associated():
    """An already-associated install never re-sends the credential (adversarial #6),
    even if the credential value is still present in memory/config/env."""
    client = _make_client(credential=_CREDENTIAL, associated=True)
    payload = client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert "deployment_credential" not in payload


def test_deployment_credential_not_resent_on_reconnect():
    """The credential is sent once per registration attempt. A second hello in
    the same session (reconnect) does not re-send it once the in-session flag is
    set."""
    client = _make_client(credential=_CREDENTIAL)
    first = client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert first["deployment_credential"] == _CREDENTIAL
    second = client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert "deployment_credential" not in second


def test_hello_payload_omits_credential_for_direct_install():
    """A direct install (no credential) gets a byte-identical payload to today:
    the four base keys and no deployment_credential key."""
    client = _make_client(credential="")
    payload = client._build_hello_payload(tos_version="v2025", app_version="9.9.9")
    assert payload == {
        "gateway_token": "test-gateway-token",
        "instance_id": "test-instance-id",
        "tos_version": "v2025",
        "version": "9.9.9",
    }


def test_hello_payload_omits_whitespace_only_credential():
    """A whitespace-only credential is treated as absent (input validation)."""
    client = _make_client(credential="   ")
    payload = client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert "deployment_credential" not in payload


# ── association recorded on hello_ack ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_hello_ack_records_association_and_clears_credential(monkeypatch):
    """A successful hello_ack after a credentialled hello records the association
    (removing the credential from bootstrap state) exactly once. A later ack is a
    no-op once the association is recorded."""
    calls = []

    def _fake_record():
        calls.append(True)

    monkeypatch.setattr(
        "celerp.gateway.client.record_deployment_association", _fake_record, raising=False,
    )

    client = _make_client(credential=_CREDENTIAL)
    # Simulate the credentialled hello having been sent.
    client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert client._credential_sent is True

    await client._dispatch({"type": "hello_ack", "payload": {"instance_id": "test-instance-id"}})
    assert calls == [True]
    assert client._association_recorded is True

    # A later ack in the same session is a no-op (already associated).
    await client._dispatch({"type": "hello_ack", "payload": {"instance_id": "test-instance-id"}})
    assert calls == [True]


@pytest.mark.asyncio
async def test_hello_ack_no_association_when_credential_not_sent(monkeypatch):
    """A direct install (no credential sent) records no association on hello_ack."""
    calls = []
    monkeypatch.setattr(
        "celerp.gateway.client.record_deployment_association",
        lambda: calls.append(True), raising=False,
    )
    client = _make_client(credential="")
    client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert client._credential_sent is False
    await client._dispatch({"type": "hello_ack", "payload": {"instance_id": "test-instance-id"}})
    assert calls == []
    assert client._association_recorded is False


@pytest.mark.asyncio
async def test_hello_ack_association_survives_record_failure(monkeypatch):
    """If record_deployment_association raises (filesystem error), the association
    still holds in memory so the credential is not re-sent this session."""
    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(
        "celerp.gateway.client.record_deployment_association", _boom, raising=False,
    )
    client = _make_client(credential=_CREDENTIAL)
    client._build_hello_payload(tos_version="", app_version="1.0.0")
    await client._dispatch({"type": "hello_ack", "payload": {"instance_id": "test-instance-id"}})
    # In-memory marker set despite the write failure.
    assert client._association_recorded is True
    # The credential is not re-offered this session.
    payload = client._build_hello_payload(tos_version="", app_version="1.0.0")
    assert "deployment_credential" not in payload


# ── credential never logged (adversarial #9) ───────────────────────────────────

@pytest.mark.asyncio
async def test_deployment_credential_never_logged(caplog):
    """The credential string appears in the built hello frame exactly once and in
    no captured log record across the handshake/dispatch path (adversarial #9)."""
    client = _make_client(credential=_CREDENTIAL)
    with caplog.at_level(logging.DEBUG):
        payload = client._build_hello_payload(tos_version="", app_version="1.0.0")
        # Present exactly once, in the frame it is meant to travel in.
        assert payload["deployment_credential"] == _CREDENTIAL
        await client._dispatch({
            "type": "hello_ack",
            "payload": {"instance_id": "test-instance-id", "session_token": "s"},
        })
    for record in caplog.records:
        assert _CREDENTIAL not in record.getMessage()
        for arg in (record.args or ()):
            assert _CREDENTIAL not in str(arg)
