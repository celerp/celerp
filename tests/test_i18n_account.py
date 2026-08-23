# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Celerp-account surface.

The account panels (signup, waiting, signed-in) build every user-facing label,
placeholder, and button by calling ``t()`` at render time with the request
language, so a request in a non-English language gets translated output. These
tests register a sentinel language ``xx`` and render each panel directly with
``lang="xx"``, asserting the sentinel text reaches the rendered markup. They are
red against any tree that resolves these strings at import time or hardcodes
English.

Conversion mechanisms covered:
  - a direct panel label / input placeholder / button resolved at render time
    (``account.title``, ``account.email_placeholder``, ``btn.continue_with_email``);
  - the module-level value->key benefit map (``_GATE_BENEFIT_KEYS``) resolved at
    render time via ``t()`` (``account.gate_buy_benefit``);
  - interpolated labels filling ``{param}`` at render time
    (``account.signed_in_as`` / ``account.plan``);
  - the Google-open manual-fallback hint and link on the waiting panel
    (``account.google_open_hint`` / ``account.google_open_link``).
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.account import account_panel, _waiting_panel, _signed_in_panel

# Sentinel catalog: one unmistakable value per key the account panels render on
# the paths exercised below. {param} placeholders are preserved so the
# render-time interpolation is exercised too.
_XX = {
    "account.title": "XX_ACCT_TITLE",
    "account.email_placeholder": "XX_EMAIL_PH",
    "btn.continue_with_email": "XX_CONT_EMAIL",
    "account.gate_buy_benefit": "XX_BUY_BENEFIT",
    "account.signed_in_as": "XX_SIGNED_IN {email}",
    "account.plan": "XX_PLAN {tier}",
    "account.google_open_hint": "XX_GOOGLE_HINT",
    "account.google_open_link": "XX_GOOGLE_LINK",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language and reset both the registry and the context
    language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_signup_panel_labels_translate():
    """The signup panel's title, email placeholder, and continue button all
    resolve at render time in the request language."""
    html = to_xml(account_panel("xx", intent="signup",
                                panel_id="celerp-account-panel"))
    assert "XX_ACCT_TITLE" in html
    assert "XX_EMAIL_PH" in html
    assert "XX_CONT_EMAIL" in html


def test_gate_benefit_map_resolves_at_render():
    """The staged-action benefit line comes from the module-level value->key map
    (_GATE_BENEFIT_KEYS) resolved via t() at render time, not frozen at import."""
    html = to_xml(account_panel("xx", intent="signup",
                                panel_id="account-gate-panel",
                                next_action="buy:some-module:once"))
    assert "XX_BUY_BENEFIT" in html


def test_signed_in_panel_interpolates_at_render():
    """The signed-in panel fills {email} and {tier} at render time."""
    html = to_xml(_signed_in_panel(
        "xx", "celerp-account-panel",
        {"email": "buyer@example.com", "tier": "cloud"}))
    assert "XX_SIGNED_IN buyer@example.com" in html
    assert "XX_PLAN" in html


def test_waiting_panel_google_hint_translates():
    """The Google manual-fallback hint and link on the waiting panel resolve at
    render time when a safe authorize URL is present."""
    html = to_xml(_waiting_panel(
        "xx", "celerp-account-panel", "google",
        authorize_url="https://accounts.google.com/o/oauth2/auth?client_id=x"))
    assert "XX_GOOGLE_HINT" in html
    assert "XX_GOOGLE_LINK" in html
