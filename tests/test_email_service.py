# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp.services.email — gateway relay, SMTP fallback, silent skip."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_send_email_via_gateway_success():
    """Sends via gateway when gateway_token is set and client is connected."""
    mock_gw = MagicMock()
    mock_gw.send_message = AsyncMock(return_value=None)

    with (
        patch("celerp.config.settings.gateway_token", "tok123"),
        patch("celerp.gateway.client.get_client", return_value=mock_gw),
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>", "Hi")

    assert result is True
    mock_gw.send_message.assert_called_once()
    call_args = mock_gw.send_message.call_args
    assert call_args[0][0] == "email.send"
    payload = call_args[1]["payload"]
    assert payload["to"] == "user@example.com"
    assert payload["subject"] == "Hello"


@pytest.mark.asyncio
async def test_send_email_gateway_none_falls_back_to_smtp():
    """When gateway client returns None (not connected), falls back to SMTP."""
    with (
        patch("celerp.config.settings.gateway_token", "tok123"),
        patch("celerp.gateway.client.get_client", return_value=None),
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", "user"),
        patch("celerp.config.settings.smtp_password", "pass"),
        patch("celerp.config.settings.smtp_from", "noreply@example.com"),
        patch("celerp.config.settings.smtp_tls", True),
        patch("aiosmtplib.send", new=AsyncMock(return_value=None)) as mock_smtp,
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result is True
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_gateway_error_falls_back_to_smtp():
    """Gateway send_message raising an exception falls back to SMTP."""
    mock_gw = MagicMock()
    mock_gw.send_message = AsyncMock(side_effect=RuntimeError("ws closed"))

    with (
        patch("celerp.config.settings.gateway_token", "tok123"),
        patch("celerp.gateway.client.get_client", return_value=mock_gw),
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", ""),
        patch("celerp.config.settings.smtp_password", ""),
        patch("celerp.config.settings.smtp_from", "noreply@example.com"),
        patch("celerp.config.settings.smtp_tls", True),
        patch("aiosmtplib.send", new=AsyncMock(return_value=None)) as mock_smtp,
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result is True
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_smtp_only_success():
    """No gateway token, SMTP configured → sends via SMTP."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", "user"),
        patch("celerp.config.settings.smtp_password", "pass"),
        patch("celerp.config.settings.smtp_from", "noreply@example.com"),
        patch("celerp.config.settings.smtp_tls", True),
        patch("aiosmtplib.send", new=AsyncMock(return_value=None)) as mock_smtp,
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>", "Hi plain")

    assert result is True
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_smtp_error_returns_false():
    """SMTP send raising an exception returns False (does not raise)."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", "user"),
        patch("celerp.config.settings.smtp_password", "pass"),
        patch("celerp.config.settings.smtp_from", "noreply@example.com"),
        patch("celerp.config.settings.smtp_tls", True),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=ConnectionRefusedError("no smtp"))),
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result is False


@pytest.mark.asyncio
async def test_send_email_neither_configured_returns_false():
    """No gateway token, no SMTP host → returns False silently."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.smtp_host", ""),
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result is False


@pytest.mark.asyncio
async def test_smtp_from_header_uses_from_name():
    """SMTP From header must use smtp_from_name as display name."""
    captured = {}

    async def _mock_send(msg, **kwargs):
        captured["from"] = msg["From"]

    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", ""),
        patch("celerp.config.settings.smtp_password", ""),
        patch("celerp.config.settings.smtp_from", "noreply@acme.com"),
        patch("celerp.config.settings.smtp_from_name", "Acme ERP"),
        patch("celerp.config.settings.smtp_tls", True),
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
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", ""),
        patch("celerp.config.settings.smtp_password", ""),
        patch("celerp.config.settings.smtp_from", "noreply@acme.com"),
        patch("celerp.config.settings.smtp_from_name", 'Acme, "ERP"'),
        patch("celerp.config.settings.smtp_tls", True),
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
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", ""),
        patch("celerp.config.settings.smtp_password", ""),
        patch("celerp.config.settings.smtp_from", "noreply@acme.com"),
        patch("celerp.config.settings.smtp_from_name", "Acme ERP"),
        patch("celerp.config.settings.smtp_tls", True),
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
        patch("celerp.config.settings.smtp_host", "smtp.example.com"),
        patch("celerp.config.settings.smtp_port", 587),
        patch("celerp.config.settings.smtp_user", ""),
        patch("celerp.config.settings.smtp_password", ""),
        patch("celerp.config.settings.smtp_from", "noreply@acme.com"),
        patch("celerp.config.settings.smtp_from_name", "Acme ERP"),
        patch("celerp.config.settings.smtp_tls", True),
        patch("aiosmtplib.send", new=AsyncMock(side_effect=_mock_send)),
    ):
        from celerp.services import email as email_mod
        await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert captured["reply_to"] is None


@pytest.mark.asyncio
async def test_gateway_payload_includes_reply_to():
    """Gateway relay path must forward reply_to in the email.send payload."""
    mock_gw = MagicMock()
    mock_gw.send_message = AsyncMock(return_value=None)

    with (
        patch("celerp.config.settings.gateway_token", "tok123"),
        patch("celerp.gateway.client.get_client", return_value=mock_gw),
    ):
        from celerp.services import email as email_mod
        await email_mod.send_email(
            "user@example.com", "Hello", "<p>Hi</p>", reply_to="support@acme.com"
        )

    payload = mock_gw.send_message.call_args[1]["payload"]
    assert payload["reply_to"] == "support@acme.com"
