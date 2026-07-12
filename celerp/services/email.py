# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Email service — relay HTTP first (verified), SMTP fallback, skip if neither configured."""

from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger(__name__)


async def send_email(
    to: str,
    subject: str,
    body_html: str,
    body_text: str = "",
    reply_to: str = "",
    cc: str = "",
    bcc: str = "",
) -> tuple[bool, str]:
    """Send a transactional email via the relay or SMTP fallback.

    Returns (ok, detail). ok is True only when a transport actually accepted
    the message - the relay path is a synchronous HTTP call to the relay's
    /email/send (which submits to the provider before answering 200), so
    callers can surface real delivery feedback. Never raises.
    """
    from celerp.config import settings

    detail = ""
    # 1. Relay (preferred when cloud-connected): synchronous and verified.
    #    The old path pushed an email.send frame down the gateway WS, which
    #    the relay never handled - emails silently vanished.
    if settings.gateway_token:
        from celerp.gateway.state import relay_http_url, relay_session_headers
        headers = relay_session_headers()
        if headers.get("X-Session-Token"):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{relay_http_url()}/email/send",
                        headers=headers,
                        json={
                            "to": to,
                            "subject": subject,
                            "body_html": body_html,
                            "body_text": body_text,
                            "reply_to": reply_to,
                            "cc": cc,
                            "bcc": bcc,
                        },
                    )
                if resp.status_code == 200:
                    return True, "delivered via Celerp relay"
                try:
                    detail = str(resp.json().get("detail") or f"relay error {resp.status_code}")
                except Exception:
                    detail = f"relay error {resp.status_code}"
                log.warning("Relay email to %s failed: %s", to, detail)
            except Exception as exc:
                detail = f"relay unreachable: {exc}"
                log.warning("Relay email to %s failed: %s", to, detail)
        else:
            detail = "no active cloud session"

    # 2. SMTP fallback
    if settings.smtp_host:
        try:
            import aiosmtplib

            from_addr = settings.smtp_from or settings.smtp_user
            from_header = formataddr((settings.smtp_from_name, from_addr))

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_header
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

            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                use_tls=settings.smtp_tls,
            )
            return True, "delivered via SMTP"
        except Exception as exc:
            log.debug("SMTP email failed: %s", exc)
            return False, detail or f"SMTP send failed: {exc}"

    # 3. Neither configured (or the relay refused and no SMTP fallback exists)
    log.debug("Email not sent: to=%s subject=%s detail=%s", to, subject, detail)
    return False, detail or "no email transport configured (connect the Celerp relay or set up SMTP)"
