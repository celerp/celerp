# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Email service — gateway relay first, SMTP fallback, silent skip if neither configured."""

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
) -> bool:
    """Send a transactional email via gateway relay or SMTP fallback.

    Returns True on success, False if no transport is configured or on error.
    Never raises.
    """
    from celerp.config import settings

    # 1. Gateway relay (preferred when connected to cloud)
    if settings.gateway_token:
        try:
            from celerp.gateway.client import get_client
            gw = get_client()
            if gw is not None:
                await gw.send_message(
                    "email.send",
                    payload={
                        "to": to,
                        "subject": subject,
                        "body_html": body_html,
                        "body_text": body_text,
                        "reply_to": reply_to,
                    },
                )
                return True
        except Exception as exc:
            log.debug("Gateway email failed, falling back to SMTP: %s", exc)

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
            return True
        except Exception as exc:
            log.debug("SMTP email failed: %s", exc)
            return False

    # 3. Neither configured
    log.debug("Email not sent (no gateway token or SMTP configured): to=%s subject=%s", to, subject)
    return False
