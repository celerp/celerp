# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Partner-claim card polish: component DOM assertions.

Covers the two presentation fixes that render without a server:
* the support email in the claim preview is a real ``mailto:`` anchor, not plain text;
* the ``.text-error`` class the claim/settings error paragraphs use is actually
  defined in the stylesheet, and the claim card emits a ``P.text-error`` on error.

Red at merge-base (origin/main): ``_partner_claim_preview`` renders the support
email in a plain ``Div`` with no anchor, and ``app.css`` defines no ``.text-error``
rule, so both assertions fail there.
"""
from __future__ import annotations

import pathlib
import re

from fasthtml.common import to_xml

from ui.routes.settings_cloud import (
    _backup_summary_card,
    _partner_claim_card,
    _partner_claim_preview,
)

_APP_CSS = pathlib.Path(__file__).resolve().parents[1] / "ui" / "static" / "app.css"


def test_partner_claim_preview_support_mailto():
    """The resolved-partner preview renders the support email as a mailto anchor."""
    html = to_xml(_partner_claim_preview(
        {"display_name": "A partner", "support_email": "support@example.test"},
        "tok-abc",
        lang="en",
    ))
    assert 'href="mailto:support@example.test"' in html
    # The anchor wraps the email text, not a bare Div.
    assert re.search(r'<a[^>]*href="mailto:support@example\.test"[^>]*>support@example\.test</a>', html)


def test_partner_claim_preview_support_link_noopener():
    """The support-URL link opens in a new tab with rel="noopener noreferrer"
    set, so the partner-controlled page cannot reach back into this window via
    window.opener nor read the referrer."""
    html = to_xml(_partner_claim_preview(
        {"display_name": "A partner", "support_url": "https://partner.example.test/help"},
        "tok-abc",
        lang="en",
    ))
    m = re.search(r'<a[^>]*href="https://partner\.example\.test/help"[^>]*>', html)
    assert m, "support_url anchor not found"
    assert 'rel="noopener noreferrer"' in m.group(0)


def test_claim_preview_rejects_unsafe_support_url():
    """An unsafe support_url (non-https, embedded creds, control chars) is
    omitted entirely: no anchor is rendered, and the raw value never reaches
    an href. A valid https url still renders with rel="noopener noreferrer"."""
    for bad in ("javascript:alert(1)", "http://insecure.example.test",
                "https://user:pass@partner.example.test/x", "data:text/html,x"):
        html = to_xml(_partner_claim_preview(
            {"display_name": "A partner", "support_url": bad}, "tok", lang="en"))
        assert bad not in html, f"raw unsafe url {bad!r} reached the DOM"
        assert "cloud.partner_support" not in html  # no support-link button
        assert 'target="_blank"' not in html, f"an outbound link rendered for {bad!r}"


def test_claim_preview_rejects_unsafe_email():
    """An unsafe support_email (non-address, control chars, header-injection
    attempt) is omitted: no mailto anchor is rendered."""
    for bad in ("not-an-email", "a@b@c.test", "user@exa mple.test",
                "user@example.test\r\nBcc: victim@x.test", "@nolocal.test"):
        html = to_xml(_partner_claim_preview(
            {"display_name": "A partner", "support_email": bad}, "tok", lang="en"))
        assert "mailto:" not in html, f"a mailto anchor rendered for {bad!r}"


def test_claim_preview_valid_email_renders_mailto():
    """A well-formed support_email renders as a mailto anchor (regression guard
    against over-rejection)."""
    html = to_xml(_partner_claim_preview(
        {"display_name": "A partner", "support_email": "help@partner.example.test"},
        "tok", lang="en"))
    assert 'href="mailto:help@partner.example.test"' in html


def test_text_error_style_defined():
    """The .text-error class used by the error paragraphs is defined with a color
    that resolves to a declared custom property (not a phantom variable), and the
    claim card emits a P.text-error when rendered with an error."""
    css = _APP_CSS.read_text(encoding="utf-8")
    m = re.search(r"\.text-error\s*\{[^}]*\}", css)
    assert m, ".text-error rule is missing from app.css"
    rule = m.group(0)
    color = re.search(r"color:\s*([^;]+);", rule)
    assert color, ".text-error defines no color"

    # A var() reference must resolve: either the custom property is declared in the
    # stylesheet, or the reference supplies a literal fallback. A bare var() on an
    # undeclared property renders nothing, which is the bug this guards against.
    var_ref = re.search(r"var\(\s*(--[\w-]+)\s*(,[^)]+)?\)", color.group(1))
    if var_ref and not var_ref.group(2):
        prop = var_ref.group(1)
        assert re.search(rf"{re.escape(prop)}\s*:", css), (
            f".text-error uses undeclared custom property {prop} with no fallback"
        )

    html = to_xml(_partner_claim_card(lang="en", error="Invalid claim code"))
    assert 'class="text-error"' in html
    assert "Invalid claim code" in html


# ── U5: no empty backup-summary card ─────────────────────────────────────────

def test_backup_summary_card_empty_renders_nothing():
    """When there is nothing to show (not connected, or no backup data yet),
    _backup_summary_card returns None so no empty .settings-card renders -
    pre-U5 it returned a blank Div, leaving a visible empty box on the page."""
    assert _backup_summary_card(gw_ok=False, backup_data=None) is None
    assert _backup_summary_card(gw_ok=True, backup_data=None) is None
    assert _backup_summary_card(gw_ok=False, backup_data={"db": {}}) is None


def test_backup_summary_card_populated_still_renders():
    """Positive control: a populated call still renders the real card (no
    over-suppression from the U5 fix)."""
    backup_data = {"db": {"last_run": None, "ok": None}, "next_db_utc": None}
    html = to_xml(_backup_summary_card(gw_ok=True, backup_data=backup_data))
    assert "settings-card" in html
