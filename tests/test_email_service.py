# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp.services.email — relay HTTP first (verified), SMTP fallback, skip.

The relay path is a synchronous POST to the relay's /email/send; a 200 is the
delivery verification callers surface in the notification bell. The old
gateway-WS path fired an email.send frame the relay never handled, so these
tests pin the HTTP transport specifically.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

def _mock_httpx(status=200, detail=None, auth_status=200):
    """AsyncClient factory mock: /auth/token exchange plus /email/send."""
    send_resp = MagicMock()
    send_resp.status_code = status
    send_resp.json = MagicMock(return_value={"detail": detail} if detail else {"sent": True})
    auth_resp = MagicMock()
    auth_resp.status_code = auth_status
    auth_resp.json = MagicMock(return_value={"access_token": "jwt-abc"})

    async def _post(url, **kw):
        return auth_resp if url.endswith("/auth/token") else send_resp

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx), client


_SMTP_SETTINGS = {
    "celerp.config.settings.smtp_host": "smtp.example.com",
    "celerp.config.settings.smtp_port": 587,
    "celerp.config.settings.smtp_user": "user",
    "celerp.config.settings.smtp_password": "pass",
    "celerp.config.settings.smtp_from": "noreply@example.com",
    "celerp.config.settings.smtp_tls": True,
}

import contextlib


@contextlib.contextmanager
def _smtp_configured(overrides: dict | None = None):
    """Apply the standard SMTP settings patches (plus per-test overrides)."""
    with contextlib.ExitStack() as stack:
        for k, v in {**_SMTP_SETTINGS, **(overrides or {})}.items():
            stack.enter_context(patch(k, v))
        yield


@pytest.mark.asyncio
async def test_send_email_via_relay_success():
    """Cloud-connected: exchanges the gateway token for a bearer JWT, POSTs
    the relay's /email/send, and reports verified delivery. This auth works
    against the relay as deployed - no session required."""
    factory, client = _mock_httpx(200)
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.services import email as email_mod
        ok, detail = await email_mod.send_email(
            "user@example.com", "Hello", "<p>Hi</p>", "Hi",
            reply_to="support@acme.com", cc="a@x.com", bcc="b@x.com",
        )

    assert ok is True
    assert "relay" in detail
    assert client.post.call_count == 2
    auth_call, send_call = client.post.call_args_list
    assert auth_call[0][0] == "https://relay.test/auth/token"
    assert auth_call[1]["json"] == {"api_key": "api-key-123"}
    assert send_call[0][0] == "https://relay.test/email/send"
    assert send_call[1]["headers"]["Authorization"] == "Bearer jwt-abc"
    payload = send_call[1]["json"]
    assert payload["to"] == "user@example.com"
    assert payload["subject"] == "Hello"
    assert payload["reply_to"] == "support@acme.com"
    assert payload["cc"] == "a@x.com"
    assert payload["bcc"] == "b@x.com"


@pytest.mark.asyncio
async def test_send_email_relay_quota_error_surfaces_detail():
    """A relay refusal (quota/plan) with no SMTP fallback returns the reason,
    which lands in the failure notification."""
    factory, _ = _mock_httpx(402, detail="Monthly email quota (500) reached. Upgrade your plan for more.")
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
        patch("celerp.config.settings.smtp_host", ""),
    ):
        from celerp.services import email as email_mod
        ok, detail = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert ok is False
    assert "quota" in detail


@pytest.mark.asyncio
async def test_send_email_relay_unreachable_falls_back_to_smtp():
    """Relay network failure falls back to SMTP when configured."""
    factory, client = _mock_httpx(200)
    client.post.side_effect = ConnectionError("relay down")
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
        _smtp_configured(),
        patch("aiosmtplib.send", new=AsyncMock(return_value=None)) as mock_smtp,
    ):
        from celerp.services import email as email_mod
        ok, detail = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert ok is True
    assert "SMTP" in detail
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_relay_auth_rejected_falls_back_to_smtp():
    """A relay that refuses the token exchange (e.g. revoked key) → SMTP."""
    factory, _ = _mock_httpx(200, auth_status=401)
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
        _smtp_configured(),
        patch("aiosmtplib.send", new=AsyncMock(return_value=None)) as mock_smtp,
    ):
        from celerp.services import email as email_mod
        ok, _ = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert ok is True
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_smtp_only_success():
    """No gateway token, SMTP configured → sends via SMTP."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        _smtp_configured(),
        patch("aiosmtplib.send", new=AsyncMock(return_value=None)) as mock_smtp,
    ):
        from celerp.services import email as email_mod
        ok, detail = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>", "Hi plain")

    assert ok is True
    assert "SMTP" in detail
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_smtp_error_returns_false():
    """SMTP send raising an exception returns (False, reason) - never raises."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        _smtp_configured(),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=ConnectionRefusedError("no smtp"))),
    ):
        from celerp.services import email as email_mod
        ok, detail = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert ok is False
    assert "SMTP" in detail


@pytest.mark.asyncio
async def test_send_email_neither_configured_returns_false():
    """No gateway token, no SMTP host → (False, actionable reason)."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.smtp_host", ""),
    ):
        from celerp.services import email as email_mod
        ok, detail = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert ok is False
    assert "relay" in detail or "SMTP" in detail


@pytest.mark.asyncio
async def test_smtp_from_header_uses_from_name():
    """SMTP From header must use smtp_from_name as display name."""
    captured = {}

    async def _mock_send(msg, **kwargs):
        captured["from"] = msg["From"]

    with (
        patch("celerp.config.settings.gateway_token", ""),
        _smtp_configured({"celerp.config.settings.smtp_from": "noreply@acme.com",
                          "celerp.config.settings.smtp_from_name": "Acme ERP"}),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=_mock_send)),
    ):
        from celerp.services import email as email_mod
        await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert "Acme ERP" in captured["from"]
    assert "noreply@acme.com" in captured["from"]


@pytest.mark.asyncio
async def test_smtp_from_header_special_chars_in_name():
    """smtp_from_name with commas/quotes must produce a valid RFC 2822 From header."""
    captured = {}

    async def _mock_send(msg, **kwargs):
        captured["from"] = msg["From"]

    with (
        patch("celerp.config.settings.gateway_token", ""),
        _smtp_configured({"celerp.config.settings.smtp_from": "noreply@acme.com",
                          "celerp.config.settings.smtp_from_name": 'Acme, "ERP"'}),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=_mock_send)),
    ):
        from celerp.services import email as email_mod
        await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    # formataddr must quote the name — result must contain the address
    assert "noreply@acme.com" in captured["from"]
    # Must not be bare unquoted (would be malformed)
    assert captured["from"] != 'Acme, "ERP" <noreply@acme.com>'


@pytest.mark.asyncio
async def test_smtp_reply_to_header_set_when_provided():
    """reply_to must appear as Reply-To header in SMTP message."""
    captured = {}

    async def _mock_send(msg, **kwargs):
        captured["reply_to"] = msg.get("Reply-To")

    with (
        patch("celerp.config.settings.gateway_token", ""),
        _smtp_configured(),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=_mock_send)),
    ):
        from celerp.services import email as email_mod
        await email_mod.send_email(
            "user@example.com", "Hello", "<p>Hi</p>", reply_to="support@acme.com"
        )

    assert captured["reply_to"] == "support@acme.com"


@pytest.mark.asyncio
async def test_smtp_no_reply_to_when_not_provided():
    """Reply-To header must be absent when reply_to is not passed."""
    captured = {}

    async def _mock_send(msg, **kwargs):
        captured["reply_to"] = msg.get("Reply-To")

    with (
        patch("celerp.config.settings.gateway_token", ""),
        _smtp_configured(),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=_mock_send)),
    ):
        from celerp.services import email as email_mod
        await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert captured["reply_to"] is None
