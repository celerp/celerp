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


@pytest.mark.asyncio
async def test_an_email_is_refused_while_its_caller_holds_a_transaction_or_the_company_lock(session):
    """No send waits on its transport while the caller holds a transaction, and with it every
    lock the transaction took: every writer of the company would wait on the network."""
    import uuid

    from sqlalchemy import text

    from celerp.services import email as email_mod
    from celerp.services.company_lock import lock_company

    async with SmtpStub() as stub:
        with smtp_settings(stub.port):
            await session.execute(text("SELECT 1"))
            with pytest.raises(RuntimeError, match="transaction is open"):
                await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")
            await session.rollback()
            await lock_company(session, uuid.uuid4())
            with pytest.raises(RuntimeError, match="transaction is open"):
                await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")
            await session.rollback()
            assert stub.messages == []
            assert (await email_mod.send_email("user@example.com", "Hello", "<p>Hi</p>")).ok
    assert len(stub.messages) == 1


def _attempt_record(monkeypatch, *, age, state="unconfirmed", settled=True):
    """A stored attempt that began ``age`` ago under relay account "account"."""
    from datetime import datetime, timezone

    from celerp.services import email
    monkeypatch.setattr(email, "relay_identity", lambda: "account")
    record = email.attempt_record(email.Attempt("op-1", {"to": "a@test.test", "subject": "s", "body_html": "b"}), None)
    record = {**record, "at": (datetime.now(timezone.utc) - age).isoformat()}
    if settled:
        record = {**email.settled_record(record, email.EmailResult(False, "timeout", unconfirmed=True)), "state": state}
    return record


@pytest.mark.parametrize("settled", [True, False], ids=["answered_unconfirmed", "cut_off_while_sending"])
def test_an_unconfirmed_attempt_is_replayed_only_inside_the_replay_window(monkeypatch, settled):
    """The relay answers for an operation id for a limited time: every caller of next_step gets
    "unverified", never "replay", for an attempt that began longer ago than REPLAY_WINDOW."""
    from datetime import timedelta

    from celerp.services import email
    margin = timedelta(minutes=10)
    assert email.next_step(_attempt_record(monkeypatch, age=email.REPLAY_WINDOW - margin, settled=settled)) == "replay"
    assert email.next_step(_attempt_record(monkeypatch, age=email.REPLAY_WINDOW + margin, settled=settled)) == "unverified"
    assert email.next_step(_attempt_record(monkeypatch, age=timedelta(days=30), settled=settled)) == "unverified"
    unreadable = {**_attempt_record(monkeypatch, age=margin, settled=settled), "at": "never", "began": "never"}
    assert email.next_step(unreadable) == "unverified"
    assert email.next_step(unreadable, again=True) == "new"


def test_the_replay_window_runs_from_when_the_attempt_began_not_from_its_answer(monkeypatch):
    from datetime import datetime, timezone

    from celerp.services import email
    record = _attempt_record(monkeypatch, age=email.REPLAY_WINDOW * 2)
    assert datetime.fromisoformat(record["at"]) > datetime.now(timezone.utc) - email.REPLAY_WINDOW
    assert email.next_step(record) == "unverified"


def test_a_replay_keeps_when_the_attempt_first_began_so_replays_cannot_roll_the_window(monkeypatch):
    from celerp.services import email
    message = {"to": "a@test.test", "subject": "s", "body_html": "b"}
    first = _attempt_record(monkeypatch, age=email.REPLAY_WINDOW * 0.75)
    assert email.next_step(first) == "replay"
    replayed = email.attempt_record(email.Attempt("op-1", message), first)
    assert replayed["state"] == "sending" and replayed["began"] == first["began"]
    cut_off = email.settled_record(replayed, email.EmailResult(False, "timeout", unconfirmed=True))
    assert cut_off["began"] == first["began"]
    # the same replay half a window later is past the window of the first attempt
    later = {**cut_off, "began": first["began"]}
    monkeypatch.setattr(email, "REPLAY_WINDOW", email.REPLAY_WINDOW * 0.5)
    assert email.next_step(later) == "unverified"
    # a new email (another operation id) starts its own window
    assert "began" not in email.attempt_record(email.Attempt("op-2", message), first)


def test_an_unconfirmed_attempt_is_never_replayed_through_another_relay_account(monkeypatch):
    from datetime import timedelta

    from celerp.config import settings
    from celerp.services import email
    record = _attempt_record(monkeypatch, age=timedelta(minutes=10))
    assert email.next_step(record) == "replay"
    for other in ("another account", None):
        monkeypatch.setattr(email, "relay_identity", lambda: other)
        assert email.next_step(record) == "unverified"
    monkeypatch.undo()
    monkeypatch.setattr(settings, "gateway_token", "token-a")
    one = email.relay_identity()
    assert one and "token-a" not in one
    monkeypatch.setattr(settings, "gateway_http_url", "https://other.example.test")
    two = email.relay_identity()
    monkeypatch.setattr(settings, "gateway_token", "token-b")
    assert len({one, two, email.relay_identity()}) == 3
    monkeypatch.setattr(settings, "gateway_token", "")
    assert email.relay_identity() is None


class _Session:
    """A session that records when its transaction ends, for the order of a durable send."""
    def __init__(self, log):
        self.log = log

    def expire_all(self):
        self.log.append("expire")

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")


async def test_a_durable_send_confirms_each_recipient_just_before_their_own_email(monkeypatch):
    """Any caller of send_durably gets the check: each attempt's recipient is read afresh
    immediately before that attempt, the read's transaction is ended before the transport is
    called, and an attempt that is not confirmed (or whose check fails) is never sent."""
    import inspect

    from celerp.services import email
    log: list = []
    a, b, c = (email.Attempt(f"op-{n}", {"to": f"{n}@test.test", "subject": "s", "body_html": "b"}) for n in "abc")

    async def send(to, subject, body_html, body_text="", **kw):
        log.append(("send", to))
        return email.EmailResult(True, "sent")
    monkeypatch.setattr(email, "send_email", send)

    async def begin(session):
        log.append("begin")
        return [a, b, c]

    async def current(session, attempt):
        log.append(("current", attempt.message["to"]))
        if attempt is c:
            raise RuntimeError("the read failed")
        return attempt is a

    async def settle(session, results):
        log.append("settle")
        return results

    results = await email.send_durably(_Session(log), begin, current, settle)
    assert log == ["begin", "commit",
                   "expire", ("current", "a@test.test"), "rollback", ("send", "a@test.test"),
                   "expire", ("current", "b@test.test"), "rollback",
                   "expire", ("current", "c@test.test"), "rollback",
                   "settle", "commit"]
    assert results[0].ok
    for refused in results[1:]:
        assert not refused.ok and not refused.unconfirmed and refused.reason == email.RECIPIENT_UNCONFIRMED
    assert inspect.signature(email.send_durably).parameters["current"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_a_transport_that_raises_leaves_the_attempt_unconfirmed_never_sent(monkeypatch):
    """A send that raised may still have gone out: its outcome is unknown, so it is
    neither reported sent nor a definite failure the caller may send again."""
    from celerp.services import email

    async def send(to, subject, body_html, body_text="", **kw):
        raise RuntimeError("the connection dropped after the message was handed over")
    monkeypatch.setattr(email, "send_email", send)
    (result,) = await email.transmit([email.Attempt("op-1", {"to": "a@test.test", "subject": "s", "body_html": "b"})])
    assert not result.ok and result.unconfirmed and result.reason is None


def test_a_replay_whose_recipient_is_not_confirmed_stays_unconfirmed_and_a_first_send_fails(monkeypatch):
    from celerp.services import email
    refused = email.EmailResult(False, "recipient not confirmed before sending", reason=email.RECIPIENT_UNCONFIRMED)
    attempt = email.Attempt("op-1", {"to": "a@test.test", "subject": "s", "body_html": "b"})
    first = email.settled_record(email.attempt_record(attempt, None), refused)
    assert first["state"] == "failed" and first["reason"] == email.RECIPIENT_UNCONFIRMED
    cut_off = _attempt_record(monkeypatch, age=email.REPLAY_WINDOW * 0.5)
    assert email.settled_record(email.attempt_record(attempt, cut_off), refused)["state"] == "unconfirmed"


def test_only_the_email_primitive_decides_a_replay():
    """The window and the relay account are read by next_step alone: every other shipped file
    asks it, so a new caller cannot replay past either."""
    import re

    from shipped_sources import shipped_sources
    owner = "celerp/services/email.py"
    # A caller that keeps its own send record stores the identity there for next_step to read.
    writers = {"default_modules/celerp-subscriptions/celerp_subscriptions/billing.py"}
    found = {}
    for rel, path in shipped_sources():
        if rel != owner and (hits := set(re.findall(r"REPLAY_WINDOW|\brelay_identity\b|\"deduplicated\"", path.read_text()))):
            found[rel] = hits
    assert found == {w: {"relay_identity"} for w in writers}


def test_an_emails_stored_form_keeps_text_past_ascii_and_its_digest_tells_it_apart():
    """Thai text is stored as itself (a size limit counts each character once) and two emails
    that differ only past ASCII have two digests, so a replay of one never matches the other."""
    from celerp.services.email import payload_digest, payload_json
    one, other = {"to": "a@example.com", "body_text": "ค่าเช่า"}, {"to": "a@example.com", "body_text": "ค่าไฟ"}
    assert "ค่าเช่า" in payload_json(one)
    assert payload_digest(one) != payload_digest(other)
