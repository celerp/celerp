# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Email service — relay HTTP first (verified), SMTP fallback, skip if neither configured."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Awaitable, Callable, NamedTuple

from celerp.output.branding import EMAIL_BRAND_LINE as _BRAND_LINE, brand_url as _brand_url

log = logging.getLogger(__name__)

_BRAND_URL = _brand_url("email")


class EmailResult(NamedTuple):
    """How a send ended. ``ok``: a transport accepted the message. ``detail``: the
    transport's own words, for logs and receipts. ``unconfirmed``: the message may have
    gone out, so it must not be sent again as a new email. ``reason``: the catalog key
    that words a failure for the reader (None when sent or unconfirmed)."""
    ok: bool
    detail: str
    unconfirmed: bool = False
    reason: str | None = None


def relay_identity() -> str | None:
    """A digest naming the relay account this installation sends through, never its token;
    None with no relay. An operation id is deduplicated only by the account that took it."""
    from celerp.config import settings
    if not settings.gateway_token:
        return None
    from celerp.gateway.state import relay_http_url
    return hashlib.sha256(f"{relay_http_url()}\n{settings.gateway_token}".encode()).hexdigest()


# The longest a send waits for its transport, and the longest a send can take with recording its
# outcome: an attempt recorded as sending less than SEND_TIMEOUT ago may still be with the
# transport (attempt_state).
TRANSPORT_TIMEOUT = timedelta(minutes=4)
SEND_TIMEOUT = TRANSPORT_TIMEOUT + timedelta(minutes=1)
# How long after an attempt began the same operation id may be sent again: shorter than the
# time the relay answers for an operation id, so a replay is never taken as a new email.
REPLAY_WINDOW = timedelta(hours=12)


class Attempt(NamedTuple):
    """One email of a durable send: its operation id and its send_email arguments."""
    operation_id: str
    message: dict


def payload_json(message: dict) -> str:
    """The stored form of an email's send_email arguments: every character past ASCII written
    as itself, so a size limit counts it once."""
    return json.dumps(message, sort_keys=True, ensure_ascii=False)


def payload_digest(message: dict) -> str:
    """The digest of an email's stored form, which a replay of its attempt must match."""
    return hashlib.sha256(payload_json(message).encode()).hexdigest()


def attempt_record(attempt: Attempt, previous: dict | None) -> dict:
    """The stored record of ``attempt`` before it goes out: kept and committed first, so a run
    stopped at any point after leaves the attempt sending, never forgotten. ``previous`` is the
    record it replaces (None for a first send): a replay of the same operation id keeps when
    that attempt first began, so repeated replays never move the replay window."""
    began = (previous.get("began") or previous.get("at")
             if previous and previous.get("operation_id") == attempt.operation_id else None)
    return {"state": "sending", "at": datetime.now(timezone.utc).isoformat(), "operation_id": attempt.operation_id,
            "digest": payload_digest(attempt.message), "relay": relay_identity(), **({"began": began} if began else {})}


def settled_record(record: dict, result: EmailResult) -> dict:
    """``record`` with the outcome of its send: sent, unconfirmed (it may have gone out) or failed.
    ``began`` keeps when the attempt first went out, which bounds its replay (next_step)."""
    state = "sent" if result.ok else "unconfirmed" if result.unconfirmed else "failed"
    return {**record, "state": state, "at": datetime.now(timezone.utc).isoformat(),
            "began": record.get("began") or record.get("at"),
            **({"reason": result.reason, "detail": result.detail} if state == "failed" else {})}


def _stamp(at) -> datetime | None:
    """A record's stored time (one with no zone is read as UTC); None when unreadable."""
    try:
        when = datetime.fromisoformat(str(at))
    except (ValueError, OverflowError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def attempt_state(record: dict | None, now: datetime) -> str | None:
    """How a stored attempt reads at ``now``: "sending" while it may still be with the
    transport, "unconfirmed" once it may have gone out unanswered (its run was cut off),
    otherwise as recorded; None when there is no record."""
    if not record or record.get("state") is None:
        return None
    if record["state"] != "sending":
        return record["state"]
    began = _stamp(record.get("at"))
    return "sending" if began is not None and began + SEND_TIMEOUT > now else "unconfirmed"


def next_step(record: dict | None, *, again: bool = False) -> str:
    """What a run does with a stored attempt: "new" sends a new email (none was recorded, or
    the transport refused it), "replay" sends the same attempt under its operation id (the
    relay that took it deduplicates, so it can never arrive twice), "stored" answers as stored
    (sent, or still with another run's transport) and "unverified" answers that it may have
    gone out: never sent again through a transport that could deliver it twice, unless the
    person confirmed it may be sent again (``again``). A replay is the same relay account
    the attempt was recorded under, inside REPLAY_WINDOW of when it began; past either, the
    relay may no longer know the operation id, so the attempt is unverified."""
    now = datetime.now(timezone.utc)
    state = attempt_state(record, now)
    if state in (None, "failed"):
        return "new"
    if state != "unconfirmed":
        return "stored"
    if again:
        return "new"
    relay, began = relay_identity(), _stamp(record.get("began") or record.get("at"))
    if relay is None or record.get("relay") != relay or began is None:
        return "unverified"
    return "replay" if abs(now - began) < REPLAY_WINDOW else "unverified"


async def transmit(attempts: list[Attempt]) -> list[EmailResult]:
    """Send each attempt under its operation id, failing closed on an unknown outcome; a send
    cut off or past TRANSPORT_TIMEOUT is unconfirmed, as it may have gone out."""
    results = []
    for attempt in attempts:
        try:
            results.append(await asyncio.wait_for(send_email(
                **attempt.message, operation_id=attempt.operation_id, fail_closed_on_uncertain=True),
                TRANSPORT_TIMEOUT.total_seconds()))
        except Exception as exc:
            log.warning("Sending email failed (%s)", type(exc).__name__)
            results.append(EmailResult(False, "delivery outcome unknown", unconfirmed=True))
    return results


async def send_durably(session, begin: Callable[..., Awaitable[list[Attempt]]],
                       settle: Callable[..., Awaitable[object]]):
    """The one lifecycle of an email a later run must not send twice. ``begin(session)``
    takes the caller's locks, records each attempt it sends (attempt_record) and returns
    them; that is committed, releasing every lock, before anything goes out, so no writer of
    the company waits on the transport. ``settle(session, results)`` then takes the locks
    again, records each outcome on the attempt it read back (settled_record) and returns
    the answer, which is committed."""
    attempts = await begin(session)
    await session.commit()
    results = await transmit(attempts) if attempts else []
    answer = await settle(session, results)
    await session.commit()
    return answer


def _with_footer(body_html: str, body_text: str) -> tuple[str, str]:
    """Append the brand footer to both MIME parts so they stay in sync."""
    footer_html = (
        '<p style="margin-top:24px;font-size:12px;color:#6b7280;">'
        f'<a href="{_BRAND_URL}" style="color:#6b7280;">{_BRAND_LINE}</a></p>'
    )
    if "</body>" in body_html:
        body_html = body_html.replace("</body>", footer_html + "</body>", 1)
    else:
        body_html = body_html + footer_html
    if body_text:
        body_text = f"{body_text}\n\n--\n{_BRAND_LINE}\n{_BRAND_URL}\n"
    return body_html, body_text


async def send_email(
    to: str,
    subject: str,
    body_html: str,
    body_text: str = "",
    reply_to: str = "",
    cc: str = "",
    bcc: str = "",
    from_name: str = "",
    branded: bool = True,
    operation_id: str | None = None,
    fail_closed_on_uncertain: bool = False,
) -> EmailResult:
    """Send a transactional email via the relay or SMTP fallback. Never raises on a
    transport failure; raises RuntimeError when called with a database transaction open
    (refuse_held_transaction), as every lock it holds would wait on the transport.

    ok is True only when a transport actually accepted the message - the relay path
    is a synchronous HTTP call to the relay's /email/send (which submits to the
    provider before answering 200), so callers can surface real delivery feedback.
    An SMTP send is unconfirmed only when it was cut off after the message was handed
    over with no answer from the server; a failure before that (connect, TLS, sign-in,
    sender or recipients refused) or an answer refusing the message is a plain failure.
    ``fail_closed_on_uncertain``: a relay send whose outcome is unknown ends there as
    unconfirmed instead of falling back to SMTP, which could send it twice.
    """
    from celerp.config import settings
    from celerp.services.company_lock import refuse_held_transaction

    refuse_held_transaction("an email is sent")
    if branded:
        body_html, body_text = _with_footer(body_html, body_text)

    detail = ""
    # 1. Relay (preferred when cloud-connected): synchronous and verified.
    #    The old path pushed an email.send frame down the gateway WS, which
    #    the relay never handled - emails silently vanished. The gateway token
    #    is the instance API key; exchanging it for a bearer JWT is the auth
    #    every relay REST endpoint accepts.
    if settings.gateway_token:
        from celerp.gateway.state import fetch_relay_bearer, relay_http_url
        base = relay_http_url()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                bearer = await fetch_relay_bearer(client)
                resp = await client.post(
                    f"{base}/email/send",
                    headers={"Authorization": f"Bearer {bearer}"},
                    json={
                        "to": to,
                        "subject": subject,
                        "body_html": body_html,
                        "body_text": body_text,
                        "reply_to": reply_to,
                        "cc": cc,
                        "bcc": bcc,
                        "from_name": from_name,
                        **({"operation_id": operation_id} if operation_id else {}),
                    },
                )
            if resp.status_code == 200:
                return EmailResult(True, "delivered via Celerp relay")
            try:
                detail = str(resp.json().get("detail") or f"relay error {resp.status_code}")
            except Exception:
                detail = f"relay error {resp.status_code}"
            log.warning("Relay email to %s failed: %s", to, detail)
            if fail_closed_on_uncertain:
                uncertain = resp.status_code >= 500 or resp.status_code == 409
                return EmailResult(False, detail, uncertain, None if uncertain else "email.err_relay_refused")
        except Exception as exc:
            detail = f"relay send failed: {exc}"
            log.warning("Relay email to %s failed: %s", to, detail)
            if fail_closed_on_uncertain:
                return EmailResult(False, detail, unconfirmed=True)

    # 2. SMTP fallback
    if settings.smtp_host:
        import aiosmtplib

        class _Transfer(aiosmtplib.SMTP):
            """Notes when the message itself starts going to the server."""
            handed_over = False

            async def data(self, message, /, **kwargs):
                self.handed_over = True
                return await super().data(message, **kwargs)

        from_addr = settings.smtp_from or settings.smtp_user
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((settings.smtp_from_name, from_addr))
        msg["To"] = to
        if reply_to:
            msg["Reply-To"] = reply_to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        smtp = None
        try:
            smtp = _Transfer(hostname=settings.smtp_host, port=settings.smtp_port,
                             username=settings.smtp_user or None, password=settings.smtp_password or None,
                             use_tls=settings.smtp_tls)
            async with smtp:
                await smtp.send_message(msg)
            return EmailResult(True, "delivered via SMTP")
        except Exception as exc:
            log.debug("SMTP email failed: %s", exc)
            failure = f"SMTP send failed: {exc}"
            if smtp is not None and smtp.handed_over and not isinstance(exc, aiosmtplib.SMTPResponseException):
                return EmailResult(False, failure, unconfirmed=True)
            if isinstance(exc, aiosmtplib.SMTPAuthenticationError):
                reason = "email.err_smtp_login"
            elif isinstance(exc, (aiosmtplib.SMTPSenderRefused, aiosmtplib.SMTPRecipientRefused,
                                  aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPDataError)):
                reason = "email.err_smtp_refused"
            else:
                reason = "email.err_smtp_unreachable"
            return EmailResult(False, failure, reason=reason)

    # 3. Neither configured (or the relay refused and no SMTP fallback exists)
    log.debug("Email not sent: to=%s subject=%s detail=%s", to, subject, detail)
    if detail:
        return EmailResult(False, detail, reason="email.err_relay_refused")
    return EmailResult(False, "no email transport configured (connect the Celerp relay or set up SMTP)",
                       reason="email.err_no_transport")
