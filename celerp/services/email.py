# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Email service — relay HTTP first (verified), SMTP fallback, skip if neither configured."""

from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from celerp.output.branding import EMAIL_BRAND_LINE as _BRAND_LINE, brand_url as _brand_url

log = logging.getLogger(__name__)

_BRAND_URL = _brand_url("email")


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
) -> tuple[bool, str]:
    """Send a transactional email via the relay or SMTP fallback.

    Returns (ok, detail). ok is True only when a transport actually accepted
    the message - the relay path is a synchronous HTTP call to the relay's
    /email/send (which submits to the provider before answering 200), so
    callers can surface real delivery feedback. Never raises.
    """
    from celerp.config import settings

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
            detail = f"relay send failed: {exc}"
            log.warning("Relay email to %s failed: %s", to, detail)

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
