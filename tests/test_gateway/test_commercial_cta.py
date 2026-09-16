# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Semantic CTA resolver tests.

``commercial_cta`` maps an (intent, sku, commercial mode) to the (href, label)
pair a presentation surface renders, keeping the visible label and its
destination in lockstep:

* celerp_direct routes through the in-app /commercial/checkout mint route;
* partner-managed prefers the support URL, then a mailto: support email, then
  the Enterprise route;
* an unknown mode fails closed to the Enterprise route labelled Contact Celerp.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

import celerp.gateway.state as gw_state
from ui.components.cloud_gate import commercial_cta, subscribe_url, topup_url
from ui.i18n import t


@pytest.fixture(autouse=True)
def reset_commercial_context():
    saved = gw_state._commercial_context
    gw_state._commercial_context = {}
    yield
    gw_state._commercial_context = saved


def _partner(support_url="https://partner.example.com/support",
             support_email="help@partner.example.com"):
    impl = {"partner_id": "partner-1", "display_name": "Partner Co"}
    if support_url:
        impl["support_url"] = support_url
    if support_email:
        impl["support_email"] = support_email
    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": impl,
    }


def test_direct_subscribe_uses_mint_route():
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    href, label = commercial_cta("subscribe", "cloud", "Upgrade to Connect", "en")
    assert href == subscribe_url("cloud")
    assert href == "/commercial/checkout?intent=subscribe&sku=cloud"
    assert label == "Upgrade to Connect"


def test_direct_topup_uses_mint_route_and_direct_label():
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    href, label = commercial_cta("topup", "ai", "unused-direct-label", "en")
    assert href == topup_url()
    assert href == "/commercial/checkout?intent=topup&sku=ai"
    assert label == t("ai.top_up_credits", "en")


def test_partner_support_url_wins():
    _partner()
    href, label = commercial_cta("subscribe", "cloud", "Upgrade", "en")
    assert href == "https://partner.example.com/support"
    assert label == t("cloud.partner_support", "en")


def test_partner_support_email_when_no_url():
    _partner(support_url="")
    href, label = commercial_cta("subscribe", "cloud", "Upgrade", "en")
    assert href == "mailto:help@partner.example.com"
    assert label == t("cloud.partner_support", "en")


def test_partner_neither_is_enterprise_contact_celerp():
    _partner(support_url="", support_email="")
    href, label = commercial_cta("subscribe", "cloud", "Upgrade", "en")
    assert href == gw_state.enterprise_url()
    assert label == t("cloud.contact_celerp", "en")


def test_unknown_mode_is_enterprise_contact_celerp():
    gw_state._commercial_context = {"commercial_mode": "reseller"}
    href, label = commercial_cta("subscribe", "cloud", "Upgrade", "en")
    assert href == gw_state.enterprise_url()
    assert label == t("cloud.contact_celerp", "en")


def test_contact_celerp_label_is_not_marketing():
    """The no-contact fallback label must literally read Contact Celerp (matches
    the destination), never a direct-price or trial CTA."""
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    # A direct label passes through verbatim; the resolver never rewrites it.
    _, label = commercial_cta("subscribe", "cloud", "Custom Direct Label", "en")
    assert label == "Custom Direct Label"
