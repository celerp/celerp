# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Call-site routing for every presentation surface B1 rewires off the raw
build_commercial_handoff() resolver.

Authenticated in-app surfaces resolve through commercial_cta (when the visible
label must track the destination) or the thin subscribe_url/topup_url helpers
(when the caller's label is already correct). Pre-auth and external/backend
surfaces resolve through build_public_acquisition_url, which never emits a
named instance_id - a direct install's anonymous celerp.com/subscribe URL
carries no instance_id, so the website never sees a named checkout it cannot
verify without a handoff token.

Red at merge-base: every call site below still calls build_commercial_handoff
directly, so a celerp_direct install renders a raw named checkout URL with no
handoff token, and cloud's 403 on an unproven instance_id breaks the click.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from fasthtml.common import to_xml

import celerp.gateway.state as gw_state

_IID = "inst-abc"


@pytest.fixture(autouse=True)
def reset_commercial_context():
    gw_state._commercial_context = {}
    yield
    gw_state._commercial_context = {}


def _partner(support_url: str = "https://partner.example.com/support",
             support_email: str = "") -> None:
    implementation = {"display_name": "Partner Co"}
    if support_url:
        implementation["support_url"] = support_url
    if support_email:
        implementation["support_email"] = support_email
    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": implementation,
    }


# ── settings_cloud._direct_plans (authenticated, celerp_direct only) ──────────

def test_direct_plans_cloud_plan_points_at_mint_route():
    from ui.routes.settings_cloud import _direct_plans
    html = to_xml(_direct_plans(_IID, lang="en"))
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
    assert "celerp.com/subscribe" not in html


def test_direct_plans_ai_plan_points_at_mint_route():
    from ui.routes.settings_cloud import _direct_plans
    html = to_xml(_direct_plans(_IID, lang="en"))
    assert "/commercial/checkout?intent=subscribe&amp;sku=ai" in html


# ── settings_cloud._partner_offer (partner-managed contact CTA) ───────────────

def test_partner_offer_uses_support_url_when_present():
    from ui.routes.settings_cloud import _partner_offer
    _partner(support_url="https://partner.example.com/support")
    html = to_xml(_partner_offer(_IID, lang="en"))
    assert "https://partner.example.com/support" in html
    assert "celerp.com/subscribe" not in html


def test_partner_offer_falls_back_to_support_email():
    from ui.routes.settings_cloud import _partner_offer
    _partner(support_url="", support_email="help@partner.example.com")
    html = to_xml(_partner_offer(_IID, lang="en"))
    assert "mailto:help@partner.example.com" in html


def test_partner_offer_no_contact_falls_back_to_enterprise_labelled_contact_celerp():
    from ui.routes.settings_cloud import _partner_offer
    from ui.i18n import t
    _partner(support_url="", support_email="")
    html = to_xml(_partner_offer(_IID, lang="en"))
    assert "/enterprise" in html
    assert t("cloud.contact_celerp", "en") in html


# ── modules_page._license_upsell ───────────────────────────────────────────────

def test_license_upsell_points_at_mint_route():
    from ui.routes.modules_page import _license_upsell
    html = to_xml(_license_upsell(lang="en"))
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
    assert "celerp.com/subscribe" not in html


# ── settings._cloud_relay_unconnected ──────────────────────────────────────────

def test_cloud_relay_unconnected_points_at_mint_route():
    from ui.routes.settings import _cloud_relay_unconnected
    html = to_xml(_cloud_relay_unconnected(_IID))
    assert "/commercial/checkout?intent=subscribe" in html
    assert "celerp.com/subscribe" not in html


# ── settings_connectors._entitlement_cta ───────────────────────────────────────

def test_entitlement_cta_points_at_mint_route():
    from ui.routes.settings_connectors import _entitlement_cta
    html = to_xml(_entitlement_cta(lang="en"))
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
    assert "celerp.com/subscribe" not in html


# ── setup._cloud_form (authenticated post-account step) ────────────────────────

def test_setup_cloud_form_points_at_mint_route():
    from ui.routes.setup import _cloud_form
    html = to_xml(_cloud_form())
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
    assert "celerp.com/subscribe" not in html


def test_setup_cloud_form_partner_mode_routes_to_partner_support():
    """Partner mode resolves the setup CTA through commercial_cta, so its href and
    label track a partner-managed destination: the partner support URL with the
    partner-support label, never the direct mint route and never a $29 figure."""
    from ui.routes.setup import _cloud_form
    from ui.i18n import t
    _partner(support_url="https://partner.example.com/support")
    html = to_xml(_cloud_form())
    assert "https://partner.example.com/support" in html
    assert t("cloud.partner_support", "en") in html
    assert "/commercial/checkout" not in html
    assert "$29" not in html


# ── auth._direct_connection_gate (PRE-AUTH) ─────────────────────────────────────

def test_direct_connection_gate_uses_public_acquisition_no_instance_id():
    from ui.routes.auth import _direct_connection_gate
    html = to_xml(_direct_connection_gate("user@example.com", "pw"))
    assert "celerp.com/subscribe" in html
    assert "instance_id=" not in html


def test_direct_connection_gate_partner_uses_support_url():
    from ui.routes.auth import _direct_connection_gate
    _partner(support_url="https://partner.example.com/support")
    html = to_xml(_direct_connection_gate("user@example.com", "pw"))
    assert "https://partner.example.com/support" in html
    assert "celerp.com/subscribe" not in html


def test_direct_connection_gate_unknown_mode_fails_closed_to_enterprise():
    from ui.routes.auth import _direct_connection_gate
    gw_state._commercial_context = {"commercial_mode": "something_unexpected"}
    html = to_xml(_direct_connection_gate("user@example.com", "pw"))
    assert "/enterprise" in html
    assert "celerp.com/subscribe" not in html


# ── #2: CTA labels track their destination (no direct label on partner) ────────
#
# A partner-managed install must never show a direct-Celerp label ("Start free
# trial" / price / "Get Connect") on a CTA whose click opens partner support or
# Enterprise. These assert the (label, href) PAIR on the shared upgrade_banner
# funnel, one surface that bypasses it (connector entitlement CTA), and the
# pre-auth connection gate, across every commercial mode.


def _direct() -> None:
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}


def _anchor(html: str) -> str:
    """The single CTA anchor's opening tag + text (last <a> in the fragment)."""
    import re
    m = re.findall(r"<a\b[^>]*>.*?</a>", html, flags=re.S)
    assert m, f"no anchor in {html!r}"
    return m[-1]


# upgrade_banner - the shared funnel every gated settings tab renders through.

def test_upgrade_banner_direct_shows_trial_label_and_mint_route():
    from ui.components.cloud_gate import upgrade_banner
    from ui.i18n import t
    _direct()
    html = to_xml(upgrade_banner("Encrypted Backup", "desc", plan="cloud", lang="en"))
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
    assert t("cloud.start_trial", "en") in html


def test_upgrade_banner_partner_url_shows_support_label_not_trial():
    from ui.components.cloud_gate import upgrade_banner
    from ui.i18n import t
    _partner(support_url="https://partner.example.com/support")
    tag = _anchor(to_xml(upgrade_banner("Encrypted Backup", "desc", plan="cloud", lang="en")))
    assert 'href="https://partner.example.com/support"' in tag
    assert t("cloud.partner_support", "en") in tag
    assert t("cloud.start_trial", "en") not in tag
    assert "/commercial/checkout" not in tag


def test_upgrade_banner_partner_email_shows_support_label():
    from ui.components.cloud_gate import upgrade_banner
    from ui.i18n import t
    _partner(support_url="", support_email="help@partner.example.com")
    tag = _anchor(to_xml(upgrade_banner("Encrypted Backup", "desc", plan="cloud", lang="en")))
    assert 'href="mailto:help@partner.example.com"' in tag
    assert t("cloud.partner_support", "en") in tag


def test_upgrade_banner_partner_neither_fails_closed_to_contact_celerp():
    from ui.components.cloud_gate import upgrade_banner
    from ui.i18n import t
    _partner(support_url="", support_email="")
    tag = _anchor(to_xml(upgrade_banner("Encrypted Backup", "desc", plan="cloud", lang="en")))
    assert "/enterprise" in tag
    assert t("cloud.contact_celerp", "en") in tag
    assert t("cloud.start_trial", "en") not in tag


def test_upgrade_banner_unknown_mode_fails_closed_to_contact_celerp():
    from ui.components.cloud_gate import upgrade_banner
    from ui.i18n import t
    gw_state._commercial_context = {"commercial_mode": "something_unexpected"}
    tag = _anchor(to_xml(upgrade_banner("Encrypted Backup", "desc", plan="cloud", lang="en")))
    assert "/enterprise" in tag
    assert t("cloud.contact_celerp", "en") in tag
    assert "/commercial/checkout" not in tag


# _entitlement_cta - a surface that bypasses upgrade_banner.

def test_entitlement_cta_direct_shows_trial_label():
    from ui.routes.settings_connectors import _entitlement_cta
    from ui.i18n import t
    _direct()
    html = to_xml(_entitlement_cta(lang="en"))
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
    assert t("connectors.start_trial", "en") in html


def test_entitlement_cta_partner_url_shows_support_label_not_trial():
    from ui.routes.settings_connectors import _entitlement_cta
    from ui.i18n import t
    _partner(support_url="https://partner.example.com/support")
    tag = _anchor(to_xml(_entitlement_cta(lang="en")))
    assert 'href="https://partner.example.com/support"' in tag
    assert t("cloud.partner_support", "en") in tag
    assert t("connectors.start_trial", "en") not in tag
    assert "/commercial/checkout" not in tag


def test_entitlement_cta_unknown_mode_fails_closed():
    from ui.routes.settings_connectors import _entitlement_cta
    from ui.i18n import t
    gw_state._commercial_context = {"commercial_mode": "something_unexpected"}
    tag = _anchor(to_xml(_entitlement_cta(lang="en")))
    assert "/enterprise" in tag
    assert t("cloud.contact_celerp", "en") in tag


# _direct_connection_gate - the pre-auth surface. href already tracks the mode
# (build_public_acquisition_url); #2 fixes only its LABEL under partner/unknown.

def test_direct_connection_gate_partner_label_is_support_not_direct():
    from ui.routes.auth import _direct_connection_gate
    from ui.i18n import t
    _partner(support_url="https://partner.example.com/support")
    tag = _anchor(to_xml(_direct_connection_gate("user@example.com", "pw")))
    assert 'href="https://partner.example.com/support"' in tag
    assert t("cloud.partner_support", "en") in tag
    assert t("btn.get_connect", "en") not in tag


def test_direct_connection_gate_unknown_label_is_contact_celerp():
    from ui.routes.auth import _direct_connection_gate
    from ui.i18n import t
    gw_state._commercial_context = {"commercial_mode": "something_unexpected"}
    tag = _anchor(to_xml(_direct_connection_gate("user@example.com", "pw")))
    assert "/enterprise" in tag
    assert t("cloud.contact_celerp", "en") in tag
    assert t("btn.get_connect", "en") not in tag


# _cloud_relay_unconnected - the Celerp Connect tab CTA. Its Subscribe button must
# resolve label+href together so a partner-managed or unknown install never shows
# the direct "Subscribe" label over a partner-support / Enterprise destination.
# Red at merge-base: the label is always t("settings.subscribe") and the href is
# always subscribe_url(""), regardless of commercial mode.


def test_cloud_relay_unconnected_direct_shows_subscribe_label_and_mint_route():
    from ui.routes.settings import _cloud_relay_unconnected
    from ui.i18n import t
    _direct()
    tag = _anchor(to_xml(_cloud_relay_unconnected(_IID)))
    assert "/commercial/checkout?intent=subscribe" in tag
    assert t("settings.subscribe", "en") in tag


def test_cloud_relay_unconnected_partner_url_shows_support_label_not_subscribe():
    from ui.routes.settings import _cloud_relay_unconnected
    from ui.i18n import t
    _partner(support_url="https://partner.example.com/support")
    tag = _anchor(to_xml(_cloud_relay_unconnected(_IID)))
    assert 'href="https://partner.example.com/support"' in tag
    assert t("cloud.partner_support", "en") in tag
    assert t("settings.subscribe", "en") not in tag
    assert "/commercial/checkout" not in tag


def test_cloud_relay_unconnected_partner_email_shows_support_label():
    from ui.routes.settings import _cloud_relay_unconnected
    from ui.i18n import t
    _partner(support_url="", support_email="help@partner.example.com")
    tag = _anchor(to_xml(_cloud_relay_unconnected(_IID)))
    assert 'href="mailto:help@partner.example.com"' in tag
    assert t("cloud.partner_support", "en") in tag
    assert t("settings.subscribe", "en") not in tag


def test_cloud_relay_unconnected_partner_neither_fails_closed_to_contact_celerp():
    from ui.routes.settings import _cloud_relay_unconnected
    from ui.i18n import t
    _partner(support_url="", support_email="")
    tag = _anchor(to_xml(_cloud_relay_unconnected(_IID)))
    assert "/enterprise" in tag
    assert t("cloud.contact_celerp", "en") in tag
    assert t("settings.subscribe", "en") not in tag
    assert "/commercial/checkout" not in tag


def test_cloud_relay_unconnected_unknown_mode_fails_closed_to_contact_celerp():
    from ui.routes.settings import _cloud_relay_unconnected
    from ui.i18n import t
    gw_state._commercial_context = {"commercial_mode": "something_unexpected"}
    tag = _anchor(to_xml(_cloud_relay_unconnected(_IID)))
    assert "/enterprise" in tag
    assert t("cloud.contact_celerp", "en") in tag
    assert t("settings.subscribe", "en") not in tag
    assert "/commercial/checkout" not in tag
