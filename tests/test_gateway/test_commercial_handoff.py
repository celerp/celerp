# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the central commercial-handoff resolver.

`build_commercial_handoff` is the single policy point every core-app commercial
CTA resolves through. It reads the install's commercial mode and returns the
correct destination: the direct subscribe URL for celerp_direct plans, the
Enterprise route for Team acquisition, and the partner's support URL (or the
Enterprise fallback) for a partner-managed install, never a direct checkout.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

import celerp.gateway.state as gw_state
from celerp.gateway.state import build_commercial_handoff, build_subscribe_url

_IID = "inst-123"


@pytest.fixture(autouse=True)
def reset_commercial_context():
    gw_state._commercial_context = {}
    yield
    gw_state._commercial_context = {}


def _partner(support_url: str = "https://partner.example.com/support") -> None:
    implementation = {"display_name": "Partner Co"}
    if support_url:
        implementation["support_url"] = support_url
    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": implementation,
        "offer": {
            "display_name": "Managed Plan",
            "retail_amount": 4900,
            "currency": "USD",
        },
    }


def test_commercial_handoff_direct_cloud_unchanged():
    """celerp_direct Connect CTA is byte-identical to today's direct URL."""
    assert build_commercial_handoff(_IID, "subscribe", "cloud") == \
        build_subscribe_url(_IID, extra="plan=cloud")


def test_commercial_handoff_direct_ai_unchanged():
    """celerp_direct AI CTA is byte-identical to today's direct URL."""
    assert build_commercial_handoff(_IID, "subscribe", "ai") == \
        build_subscribe_url(_IID, extra="plan=ai")


def test_commercial_handoff_team_routes_enterprise():
    """A Team sku on a direct install routes to Enterprise, never a direct
    plan=team checkout (#4)."""
    url = build_commercial_handoff(_IID, "subscribe", "team")
    assert "/enterprise" in url
    assert "plan=team" not in url
    assert "/subscribe" not in url


def test_commercial_handoff_unknown_sku_generic():
    """An unknown or empty sku on a direct install falls back to the generic
    subscribe URL and never raises."""
    assert build_commercial_handoff(_IID, "subscribe", "zzz") == build_subscribe_url(_IID)
    assert build_commercial_handoff(_IID, "subscribe", "") == build_subscribe_url(_IID)


def test_commercial_handoff_partner_managed_uses_support_url():
    """A partner-managed install with a support URL routes CTAs to the partner
    (#12)."""
    _partner(support_url="https://partner.example.com/support")
    assert build_commercial_handoff(_IID, "subscribe", "cloud") == \
        "https://partner.example.com/support"


def test_commercial_handoff_partner_managed_fallback_enterprise():
    """A partner-managed install with no support URL falls back to Enterprise,
    never a direct checkout (#16)."""
    _partner(support_url="")
    url = build_commercial_handoff(_IID, "subscribe", "cloud")
    assert "/enterprise" in url
    assert "/subscribe" not in url


def test_commercial_handoff_never_direct_checkout_for_partner_managed():
    """Under partner_managed, no sku ever yields a direct Celerp checkout URL
    (#16)."""
    _partner(support_url="")
    for sku in ("cloud", "ai", "team", ""):
        url = build_commercial_handoff(_IID, "subscribe", sku)
        assert "/subscribe" not in url, f"sku={sku!r} leaked a direct checkout: {url}"
