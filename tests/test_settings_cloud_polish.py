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

from ui.routes.settings_cloud import _partner_claim_card, _partner_claim_preview

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
