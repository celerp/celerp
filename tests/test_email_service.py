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

from smtp_support import SmtpStub, smtp_settings

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
        ok, detail, *_ = await email_mod.send_email(
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
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result.ok is False
    assert "quota" in result.detail
    assert result.reason == "email.err_relay_refused"


@pytest.mark.asyncio
async def test_send_email_relay_unreachable_falls_back_to_smtp():
    """Relay network failure falls back to SMTP when configured."""
    factory, client = _mock_httpx(200)
    client.post.side_effect = ConnectionError("relay down")
    async with SmtpStub() as stub:
        with (
            smtp_settings(stub.port, gateway_token="api-key-123"),
            patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
            patch("httpx.AsyncClient", factory),
        ):
            from celerp.services import email as email_mod
            result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result.ok is True
    assert "SMTP" in result.detail
    assert len(stub.messages) == 1


@pytest.mark.asyncio
async def test_send_email_relay_auth_rejected_falls_back_to_smtp():
    """A relay that refuses the token exchange (e.g. revoked key) → SMTP."""
    factory, _ = _mock_httpx(200, auth_status=401)
    async with SmtpStub() as stub:
        with (
            smtp_settings(stub.port, gateway_token="api-key-123"),
            patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
            patch("httpx.AsyncClient", factory),
        ):
            from celerp.services import email as email_mod
            result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result.ok is True
    assert len(stub.messages) == 1


@pytest.mark.asyncio
async def test_send_email_smtp_only_success():
    """No gateway token, SMTP configured → sends via SMTP."""
    async with SmtpStub() as stub:
        with smtp_settings(stub.port):
            from celerp.services import email as email_mod
            result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>", "Hi plain")

    assert result.ok is True
    assert "SMTP" in result.detail
    assert [m["To"] for m in stub.messages] == ["user@example.com"]


@pytest.mark.asyncio
async def test_send_email_smtp_error_returns_false():
    """An SMTP server that cannot be reached is a plain failure - never raises."""
    async with SmtpStub() as stub:
        port = stub.port
    with smtp_settings(port):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result.ok is False and result.unconfirmed is False
    assert "SMTP" in result.detail
    assert result.reason == "email.err_smtp_unreachable"


@pytest.mark.asyncio
async def test_send_email_neither_configured_returns_false():
    """No gateway token, no SMTP host → (False, actionable reason)."""
    with (
        patch("celerp.config.settings.gateway_token", ""),
        patch("celerp.config.settings.smtp_host", ""),
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")

    assert result.ok is False
    assert result.reason == "email.err_no_transport"


async def _sent_over_smtp(settings: dict | None = None, **kwargs):
    """Send one message through a local SMTP server; return the result and what it received."""
    async with SmtpStub() as stub:
        with smtp_settings(stub.port, **(settings or {})):
            from celerp.services import email as email_mod
            result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>", **kwargs)
    return result, stub.messages


@pytest.mark.asyncio
async def test_smtp_from_header_uses_from_name():
    """SMTP From header must use smtp_from_name as display name."""
    _, [msg] = await _sent_over_smtp({"smtp_from": "noreply@acme.com", "smtp_from_name": "Acme ERP"})
    assert "Acme ERP" in msg["From"]
    assert "noreply@acme.com" in msg["From"]


@pytest.mark.asyncio
async def test_smtp_from_header_special_chars_in_name():
    """smtp_from_name with commas/quotes must produce a valid RFC 2822 From header."""
    _, [msg] = await _sent_over_smtp({"smtp_from": "noreply@acme.com", "smtp_from_name": 'Acme, "ERP"'})
    # formataddr must quote the name - result must contain the address
    assert "noreply@acme.com" in msg["From"]
    # Must not be bare unquoted (would be malformed)
    assert msg["From"] != 'Acme, "ERP" <noreply@acme.com>'


@pytest.mark.asyncio
async def test_smtp_reply_to_header_set_when_provided():
    """reply_to must appear as Reply-To header in SMTP message."""
    _, [msg] = await _sent_over_smtp(reply_to="support@acme.com")
    assert msg["Reply-To"] == "support@acme.com"


@pytest.mark.asyncio
async def test_smtp_no_reply_to_when_not_provided():
    """Reply-To header must be absent when reply_to is not passed."""
    _, [msg] = await _sent_over_smtp()
    assert msg["Reply-To"] is None


# ── Brand footer ──────────────────────────────────────────────────────────────

def test_with_footer_appends_to_both_parts():
    """Footer lands inside <body> when present and mirrors into the text part."""
    from celerp.services.email import _BRAND_LINE, _BRAND_URL, _with_footer

    html, text = _with_footer("<html><body><p>Hi</p></body></html>", "Hi")
    assert _BRAND_LINE in html
    assert html.count("</body>") == 1
    assert html.index(_BRAND_LINE) < html.index("</body>")
    assert _BRAND_LINE in text and _BRAND_URL in text


def test_with_footer_plain_fragment_and_empty_text():
    """No <body> tag → footer is appended; empty text part stays empty."""
    from celerp.services.email import _BRAND_LINE, _with_footer

    html, text = _with_footer("<p>Hi</p>", "")
    assert _BRAND_LINE in html
    assert text == ""



@pytest.mark.asyncio
async def test_send_email_brands_by_default_and_not_when_opted_out():
    """The footer rides every send unless branded=False (security emails)."""
    from celerp.services.email import _BRAND_LINE

    for branded, expected in ((True, True), (False, False)):
        result, [msg] = await _sent_over_smtp(body_text="Hi plain", branded=branded)
        html = msg.get_payload(1).get_payload(decode=True).decode()
        assert result.ok is True
        assert (_BRAND_LINE in html) is expected


@pytest.mark.asyncio
async def test_durable_send_identity_forwarded_to_transport():
    factory, client = _mock_httpx(200)
    key = "a2f95c3b-0e6b-47a4-b817-3357e78bfbc0"
    with (
        patch("celerp.config.settings.gateway_token", "api-key-123"),
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("httpx.AsyncClient", factory),
    ):
        from celerp.services import email as email_mod
        result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>",
                                            operation_id=key, fail_closed_on_uncertain=True)
    assert result.ok
    assert client.post.call_args_list[-1].kwargs["json"]["operation_id"] == key


@pytest.mark.asyncio
async def test_subscription_send_never_falls_through_after_ambiguous_relay_error():
    factory, _ = _mock_httpx(503, detail="The email service did not confirm this send")
    async with SmtpStub() as stub:
        with (
            smtp_settings(stub.port, gateway_token="api-key-123"),
            patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
            patch("httpx.AsyncClient", factory),
        ):
            from celerp.services import email as email_mod
            result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>",
                                                fail_closed_on_uncertain=True)
    assert not result.ok and result.unconfirmed
    assert stub.messages == []


@pytest.mark.asyncio
async def test_subscription_smtp_ambiguous_failure_is_not_retriable_as_definite_failure():
    """Cut off after the message was handed over, with no reply: it may have gone out."""
    async with SmtpStub(drop_after_message=True) as stub:
        with smtp_settings(stub.port):
            from celerp.services import email as email_mod
            result = await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>",
                                                fail_closed_on_uncertain=True)
    assert not result.ok and result.unconfirmed and result.reason is None
