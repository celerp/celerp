# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The one document-email composer used by every send path."""

from __future__ import annotations

from celerp_docs.doc_email import compose_doc_email, fmt_amount


def test_amount_uses_currency_precision():
    # Two-dp currencies never lose the trailing zero (486.0 -> 486.00).
    assert fmt_amount(486.0, "USD") == "USD 486.00"
    assert fmt_amount(1234.5, "EUR") == "EUR 1,234.50"
    # Zero-dp currency has no decimals.
    assert fmt_amount(1200, "JPY") == "JPY 1,200"


def test_body_carries_message_amount_and_button():
    html, text = compose_doc_email(
        doc_type_label="Invoice", doc_number="58/2890", sender_name="ACME",
        contact_name="Nikolai", total=486.0, currency="USD",
        message="Please find attached Invoice #58/2890.",
        view_url="https://x.celerp.com/accept?token=abc",
    )
    # Everything the recipient sees, in order: message, amount, view button.
    assert "Please find attached Invoice #58/2890." in html
    assert "Amount: <strong>USD 486.00</strong>" in html
    assert 'href="https://x.celerp.com/accept?token=abc"' in html
    assert ">View Invoice<" in html
    # No reply-to line (mail is sent from noreply@).
    assert "Reply to this email" not in html
    assert "Reply to this email" not in text
    assert "USD 486.00" in text


def test_no_view_button_when_link_absent():
    html, text = compose_doc_email(
        doc_type_label="Invoice", doc_number="1", sender_name="ACME",
        contact_name="there", total=10, currency="USD",
        message=None, view_url=None,
    )
    assert "View Invoice" not in html
    assert "accept?token" not in html
    # A blank message falls back to a sensible default.
    assert "Please find attached Invoice #1." in html


def test_message_is_escaped():
    html, _ = compose_doc_email(
        doc_type_label="Invoice", doc_number="1", sender_name="ACME",
        contact_name="there", total=10, currency="USD",
        message="<script>alert(1)</script>", view_url=None,
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
