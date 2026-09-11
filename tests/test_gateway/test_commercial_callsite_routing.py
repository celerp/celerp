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


def test_setup_cloud_form_partner_mode_still_uses_mint_route():
    """Partner mode resolves through the same click-time mint route: /commercial/
    checkout applies the commercial policy at click, so no separate partner-aware
    href is needed here (the label already omits the direct price)."""
    from ui.routes.setup import _cloud_form
    _partner(support_url="https://partner.example.com/support")
    html = to_xml(_cloud_form())
    assert "/commercial/checkout?intent=subscribe&amp;sku=cloud" in html
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
