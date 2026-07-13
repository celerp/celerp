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
    pay_url: str | None = None,
    amount_due=None,
) -> tuple[str, str]:
    """Return (body_html, body_text).

    The recipient sees exactly what the sender composed, followed by the
    document amount and (when a view link is shared) a View button - the same
    structure previewed in the send modal. When the instance takes online
    payments, a Pay button leads and the View button becomes the quiet
    secondary action.

    ``amount_due`` is the remaining balance (net of applied payments/credits);
    the Pay button always charges and displays this, never the face total. An
    "Amount due" line is added when it differs from the total.
    """
    amount = fmt_amount(total, currency)
    try:
        _due = float(amount_due) if amount_due is not None else float(total or 0)
    except (TypeError, ValueError):
        _due = 0.0
    due = fmt_amount(_due, currency)
    show_due_line = pay_url and due != amount
    intro = (message or "").strip() or f"Please find attached {doc_type_label} #{doc_number}."

    intro_html = "".join(
        f"<p>{_esc(line)}</p>" for line in intro.splitlines() if line.strip()
    ) or f"<p>{_esc(intro)}</p>"

    _btn = (
        "display:inline-block;padding:11px 22px;color:#fff;text-decoration:none;"
        "border-radius:6px;font-weight:600;"
    )
    buttons = []
    if pay_url:
        buttons.append(
            f'<a href="{_esc(pay_url)}" style="{_btn}background:{_BTN_COLOR};'
            f'margin-right:8px;">Pay {_esc(due)}</a>'
        )
    if view_url:
        view_bg = "#555" if pay_url else _BTN_COLOR
        buttons.append(
            f'<a href="{_esc(view_url)}" style="{_btn}background:{view_bg};">'
            f"View {_esc(doc_type_label)}</a>"
        )
    button_html = f'<p style="margin:24px 0;">{"".join(buttons)}</p>' if buttons else ""
    due_html = (
        f'<p style="margin:16px 0;">Amount due: <strong>{_esc(due)}</strong></p>'
        if show_due_line else ""
    )
    body_html = (
        f"<p>Hi {_esc(contact_name)},</p>"
        f"{intro_html}"
        f'<p style="margin:16px 0;">Amount: <strong>{_esc(amount)}</strong></p>'
        f"{due_html}"
        f"{button_html}"
    )

    due_text = f"Amount due: {due}\n" if show_due_line else ""
    link_lines = ""
    if pay_url:
        link_lines += f"Pay online: {pay_url}\n"
    if view_url:
        link_lines += f"View it online: {view_url}\n"
    body_text = (
        f"Hi {contact_name},\n\n"
        f"{intro}\n\n"
        f"Amount: {amount}\n"
        f"{due_text}\n"
        f"{link_lines}"
    ).rstrip() + "\n"

    return body_html, body_text
