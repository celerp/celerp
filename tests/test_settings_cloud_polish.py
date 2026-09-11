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

import json
import pathlib
import re

from fasthtml.common import to_xml

from ui.routes.settings_cloud import (
    _backup_summary_card,
    _infra_db_section,
    _infra_storage_section,
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


# ── U1: Escape-to-blur on new/edited click-to-edit fields ───────────────────
#
# The codebase's established Escape-to-blur convention (ui/routes/accounting.py:442,
# ui/routes/inventory.py:471) is
# `"if(event.key==='Escape'){this.blur();event.preventDefault();}"`
# applied via `onkeydown=`. None of the infra/claim inputs below carried it before
# Pass 6, so a user editing a DB/S3/claim field could not back out with Escape,
# unlike every other editable field in the app (GDR 2j).

_ESC_BLUR = "if(event.key==='Escape'){this.blur();event.preventDefault();}"


def _find_tag_by_attr(html: str, attr: str, value: str) -> str:
    """Return the full opening tag containing attr="value", regardless of where
    that attribute falls in FastHTML's rendered attribute order or whether the
    tag itself spans a pretty-printed newline."""
    m = re.search(rf'<[a-zA-Z]+(?:(?!>)[\s\S])*?{re.escape(attr)}="{re.escape(value)}"(?:(?!>)[\s\S])*?>', html)
    assert m, f'no tag found with {attr}="{value}"'
    return m.group(0)


def test_db_fields_have_escape_to_blur():
    html = to_xml(_infra_db_section())
    for field_id in ("db_host", "db_port", "db_name", "db_user", "db_pass"):
        tag = _find_tag_by_attr(html, "id", field_id)
        assert f'onkeydown="{_ESC_BLUR}"' in tag, (
            f"{field_id} missing Escape-to-blur onkeydown"
        )


def test_storage_fields_have_escape_to_blur():
    html = to_xml(_infra_storage_section())
    for field_id in ("s3_endpoint", "s3_bucket", "s3_access_key", "s3_secret_key"):
        tag = _find_tag_by_attr(html, "id", field_id)
        assert f'onkeydown="{_ESC_BLUR}"' in tag, (
            f"{field_id} missing Escape-to-blur onkeydown"
        )


def test_claim_token_field_has_escape_to_blur():
    html = to_xml(_partner_claim_card(lang="en"))
    tag = _find_tag_by_attr(html, "id", "claim_token")
    assert f'onkeydown="{_ESC_BLUR}"' in tag


# ── B7 / Pass6 item 2: hx-disabled-elt on non-idempotent submit buttons ──────
#
# Two overlapping requests to a save/restore/test/review endpoint can race (the
# Restore button swaps current/backup URLs, so two requests can reverse the
# user's intended restore). KISS fix: hx-disabled-elt="this" on the button,
# reusing the existing spinner/indicator wiring already present on these forms.

def test_db_test_connection_button_disables_during_request():
    html = to_xml(_infra_db_section())
    m = re.search(r'<button[^>]*hx-post="/settings/cloud/test-db"[^>]*>', html)
    assert m, "DB Test Connection button not found"
    assert 'hx-disabled-elt="this"' in m.group(0)


def test_db_save_form_disables_submit_during_request():
    html = to_xml(_infra_db_section())
    m = re.search(r'<form[^>]*hx-post="/settings/cloud/save-infra"[^>]*>', html)
    assert m, "DB save-infra form not found"
    assert 'hx-disabled-elt="this"' in m.group(0)


def test_db_restore_button_disables_during_request(tmp_path, monkeypatch):
    """Highest-priority single-flight target: Restore swaps current/backup URLs,
    so two in-flight requests can reverse the user's intended restore."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://celerp:new@h:5432/celerp",
        "external_db_url_backup": "postgresql+asyncpg://celerp:old@old:5432/celerp",
    }))
    html = to_xml(_infra_db_section())
    m = re.search(r'<button[^>]*hx-post="/settings/cloud/restore-db"[^>]*>', html)
    assert m, "Restore previous DB settings button not found"
    assert 'hx-disabled-elt="this"' in m.group(0)


def test_storage_test_connection_button_disables_during_request():
    html = to_xml(_infra_storage_section())
    m = re.search(r'<button[^>]*hx-post="/settings/cloud/test-storage"[^>]*>', html)
    assert m, "Storage Test Connection button not found"
    assert 'hx-disabled-elt="this"' in m.group(0)


def test_storage_save_form_disables_submit_during_request():
    html = to_xml(_infra_storage_section())
    m = re.search(r'<form[^>]*hx-post="/settings/cloud/save-infra"[^>]*>', html)
    assert m, "Storage save-infra form not found"
    assert 'hx-disabled-elt="this"' in m.group(0)


def test_partner_claim_review_form_disables_submit_during_request():
    html = to_xml(_partner_claim_card(lang="en"))
    m = re.search(r'<form[^>]*hx-post="/settings/partner-claim/resolve"[^>]*>', html)
    assert m, "Partner claim review form not found"
    assert 'hx-disabled-elt="this"' in m.group(0)


# ── U6: standard claim form/error semantics ──────────────────────────────────

def test_claim_error_region_is_a_live_region():
    """The claim error paragraph is announced to assistive tech (role="alert"),
    matching the app's established error/flash semantics rather than a silent
    visual-only .text-error paragraph."""
    html = to_xml(_partner_claim_card(lang="en", error="Invalid claim code"))
    m = re.search(r'<p[^>]*class="text-error"[^>]*>', html)
    assert m, "claim error paragraph not found"
    assert 'role="alert"' in m.group(0)


def test_claim_review_button_present_without_error_state_change():
    """No error: the review form still renders the same single deliberate
    Review action (no wizard steps introduced)."""
    html = to_xml(_partner_claim_card(lang="en"))
    assert html.count("<form") == 1
    assert 'role="alert"' not in html  # nothing to announce yet
