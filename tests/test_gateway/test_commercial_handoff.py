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


# -- support_url trust boundary at the resolver egress (BLOCKER 1) ------------

_UNSAFE_SUPPORT_URLS = [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "vbscript:msgbox(1)",
    "blob:https://evil.example.com/x",
    "http://partner.example.com/support",   # not https
    "//partner.example.com/support",        # protocol-relative, no scheme
    "https:evil.example.com",               # no netloc
    "/relative/path",                       # relative
    "partner.example.com/support",          # bare, no scheme
]


@pytest.mark.parametrize("bad", _UNSAFE_SUPPORT_URLS)
def test_handoff_partner_rejects_schemes(bad):
    """A partner-managed install whose support_url is a non-https or
    non-canonical scheme never emits that URL; it falls to the Enterprise
    route instead."""
    _partner(support_url=bad)
    url = build_commercial_handoff(_IID, "subscribe", "cloud")
    assert url != bad
    assert "/enterprise" in url
    assert "javascript:" not in url
    assert "data:" not in url


def test_handoff_partner_valid_https():
    """A valid https support_url is returned unchanged (regression guard against
    over-rejection)."""
    _partner(support_url="https://partner.example.com/support")
    assert build_commercial_handoff(_IID, "subscribe", "cloud") == \
        "https://partner.example.com/support"


def test_safe_support_url_rejects_whitespace_and_creds():
    """Non-canonical values urlparse silently strips (leading/embedded
    whitespace, control chars, embedded userinfo) and protocol-relative or
    oversized URLs are rejected; a clean https URL is returned canonical."""
    from celerp.gateway.state import _safe_support_url
    assert _safe_support_url(" https://partner.example.com/x") == ""
    assert _safe_support_url("https://partner.example.com/x\n") == ""
    assert _safe_support_url("https://part\tner.example.com/x") == ""
    assert _safe_support_url("https://user:pass@partner.example.com/x") == ""
    assert _safe_support_url("//partner.example.com/x") == ""
    assert _safe_support_url("https://" + "a" * 4000 + ".example.com") == ""
    assert _safe_support_url(None) == ""
    assert _safe_support_url(42) == ""
    clean = "https://partner.example.com/support"
    assert _safe_support_url(clean) == clean


def test_handoff_partner_rejects_whitespace_credentials_at_egress():
    """A whitespace- or credentials-bearing support_url stored in the context
    never reaches the resolver's returned href."""
    _partner(support_url="https://user:pass@partner.example.com/support")
    url = build_commercial_handoff(_IID, "subscribe", "cloud")
    assert "user:pass@" not in url
    assert "/enterprise" in url


# -- unknown/unhandled mode fails closed (E1, BLOCKER) -----------------------

def test_handoff_unknown_mode_fails_closed():
    """A commercial_mode the resolver does not special-case must not fall into
    the direct subscribe branch; it routes to Enterprise, never a direct
    checkout."""
    import celerp.gateway.state as gw_state
    gw_state._commercial_context = {"commercial_mode": "reseller"}
    for sku in ("cloud", "ai", "", "zzz"):
        url = build_commercial_handoff(_IID, "subscribe", sku)
        assert "/subscribe" not in url, f"unknown mode leaked direct checkout for sku={sku!r}: {url}"
        assert "/enterprise" in url


# -- intent routing (topup) --------------------------------------------------

def test_handoff_direct_topup_returns_topup_url():
    """celerp_direct + intent=topup routes to the /subscribe/topup URL."""
    url = build_commercial_handoff(_IID, "topup", "ai")
    assert url == build_subscribe_url(_IID, topup=True)
    assert "/subscribe/topup" in url


def test_handoff_partner_topup_not_direct():
    """partner_managed + intent=topup routes through partner support/Enterprise,
    never a direct /subscribe/topup URL."""
    _partner(support_url="")
    url = build_commercial_handoff(_IID, "topup", "ai")
    assert "/subscribe/topup" not in url
    assert "/subscribe" not in url
    assert "/enterprise" in url
