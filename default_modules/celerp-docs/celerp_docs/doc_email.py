# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Transactional email body for a sent document or quotation.

One composer for every send path so the wording, money formatting, and the
view-link button never drift between documents and lists. The relay wraps
this body and appends the standard footer, so this module emits neither a
footer nor a reply-to line (mail is sent from noreply@celerp.com).
"""
from __future__ import annotations

import html

from celerp.services.money import currency_dp

# Brand green, matching the app's primary action colour.
_BTN_COLOR = "#0ea57a"


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def fmt_amount(total, currency: str) -> str:
    """Amount at the currency's own precision: 'USD 486.00', 'JPY 1,200'."""
    try:
        return f"{currency} {float(total):,.{currency_dp(currency)}f}"
    except (TypeError, ValueError):
        return f"{currency} {total}"


def compose_doc_email(
    *,
    doc_type_label: str,
    doc_number: str,
    sender_name: str,
    contact_name: str,
    total,
    currency: str,
    message: str | None,
    view_url: str | None,
) -> tuple[str, str]:
    """Return (body_html, body_text).

    The recipient sees exactly what the sender composed, followed by the
    document amount and (when a view link is shared) a View button - the same
    structure previewed in the send modal.
    """
    amount = fmt_amount(total, currency)
    intro = (message or "").strip() or f"Please find attached {doc_type_label} #{doc_number}."

    intro_html = "".join(
        f"<p>{_esc(line)}</p>" for line in intro.splitlines() if line.strip()
    ) or f"<p>{_esc(intro)}</p>"

    button_html = (
        f'<p style="margin:24px 0;">'
        f'<a href="{_esc(view_url)}" style="display:inline-block;padding:11px 22px;'
        f'background:{_BTN_COLOR};color:#fff;text-decoration:none;border-radius:6px;'
        f'font-weight:600;">View {_esc(doc_type_label)}</a></p>'
        if view_url else ""
    )
    body_html = (
        f"<p>Hi {_esc(contact_name)},</p>"
        f"{intro_html}"
        f'<p style="margin:16px 0;">Amount: <strong>{_esc(amount)}</strong></p>'
        f"{button_html}"
    )

    view_text = f"View it online: {view_url}\n" if view_url else ""
    body_text = (
        f"Hi {contact_name},\n\n"
        f"{intro}\n\n"
        f"Amount: {amount}\n\n"
        f"{view_text}"
    ).rstrip() + "\n"

    return body_html, body_text
