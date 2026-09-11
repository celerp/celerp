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
import pathlib

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
    from celerp.gateway.state import safe_support_url
    assert safe_support_url(" https://partner.example.com/x") == ""
    assert safe_support_url("https://partner.example.com/x\n") == ""
    assert safe_support_url("https://part\tner.example.com/x") == ""
    assert safe_support_url("https://user:pass@partner.example.com/x") == ""
    assert safe_support_url("//partner.example.com/x") == ""
    assert safe_support_url("https://" + "a" * 4000 + ".example.com") == ""
    assert safe_support_url(None) == ""
    assert safe_support_url(42) == ""
    clean = "https://partner.example.com/support"
    assert safe_support_url(clean) == clean


def test_safe_support_url_public_shared():
    """The validator is exported under one public name and is the same object
    every URL surface uses: ingress/handoff (state), health identity, and the
    settings claim preview. DRY: one validator, not a per-surface copy."""
    from celerp.gateway import state as _state
    from celerp.routers import health as _health
    from ui.routes import settings_cloud as _sc

    assert hasattr(_state, "safe_support_url"), "public validator not exported"
    assert not hasattr(_state, "_safe_support_url"), "private name still present after promotion"
    # health imports the public name (module-level or function-level).
    src = pathlib.Path(_health.__file__).read_text()
    assert "safe_support_url" in src and "_safe_support_url" not in src
    # the settings preview routes support_url through the same shared validator.
    sc_src = pathlib.Path(_sc.__file__).read_text()
    assert "safe_support_url" in sc_src


def test_safe_support_email_validates_address():
    """safe_support_email returns a clean address unchanged and rejects
    non-addresses, control/whitespace characters, and header-injection payloads
    (returning ''), so a mailto: can never be built from a hostile value."""
    from celerp.gateway.state import safe_support_email
    assert safe_support_email("help@partner.example.com") == "help@partner.example.com"
    assert safe_support_email("not-an-email") == ""
    assert safe_support_email("a@b@example.com") == ""
    assert safe_support_email("user@exa mple.com") == ""
    assert safe_support_email("user@example.com\r\nBcc: x@y.com") == ""
    assert safe_support_email("@nolocal.com") == ""
    assert safe_support_email("nolocal@") == ""
    assert safe_support_email(None) == ""
    assert safe_support_email(42) == ""
    assert safe_support_email("x" * 400 + "@example.com") == ""


def test_health_identity_validates_support_email():
    """The health identity build routes support_email through safe_support_email:
    a hostile value is dropped to empty rather than carried into a mailto."""
    from celerp.routers.health import _partner_identity
    identity = _partner_identity({
        "display_name": "A partner",
        "partner_id": "pid-1",
        "support_email": "user@example.com\r\nBcc: x@y.com",
        "support_url": "https://partner.example.com/support",
    })
    assert identity is not None
    assert identity["support_email"] == "", "hostile support_email was not dropped"
    assert identity["support_url"] == "https://partner.example.com/support"


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


# -- regression guard: build_commercial_handoff has exactly one presentation caller ---

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# ui/routes/commercial.py is the legitimate low-level mint seam: it resolves the
# destination and, for a direct celerp.com checkout, mints the click-time handoff
# token before redirecting. Every other ui/routes module must reach the resolved
# destination through commercial_cta/subscribe_url/topup_url (authenticated) or
# build_public_acquisition_url (pre-auth/external), never the raw resolver, or a
# direct install's CTA renders a named checkout URL with no handoff token and the
# relay's 403 (B1).
_ALLOWED_DIRECT_CALLERS = {"commercial.py"}


def test_no_ui_route_calls_build_commercial_handoff_directly_except_commercial_py():
    """Every ui/routes/*.py file except commercial.py must be free of a direct
    build_commercial_handoff( call. A caller needing the resolved destination uses
    the semantic CTA helper (commercial_cta) or the thin direct-route helpers
    (subscribe_url/topup_url) so the visible label always matches the destination."""
    routes_dir = _REPO_ROOT / "ui" / "routes"
    offenders = []
    for path in sorted(routes_dir.glob("*.py")):
        if path.name in _ALLOWED_DIRECT_CALLERS:
            continue
        text = path.read_text()
        if "build_commercial_handoff(" in text:
            offenders.append(path.name)
    assert offenders == [], (
        f"direct build_commercial_handoff() call(s) outside the allowed low-level "
        f"seam: {offenders}"
    )


def test_no_default_module_ui_route_calls_build_commercial_handoff_directly():
    """default_modules/*/ui_routes.py (authenticated, in-app UI) must resolve
    commercial CTAs through subscribe_url/topup_url, never build_commercial_handoff
    directly - mirrors the ui/routes guard above for module-contributed UI."""
    modules_dir = _REPO_ROOT / "default_modules"
    offenders = []
    for path in sorted(modules_dir.glob("*/*/ui_routes.py")):
        text = path.read_text()
        if "build_commercial_handoff(" in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert offenders == [], (
        f"direct build_commercial_handoff() call(s) in module UI routes: {offenders}"
    )


def test_no_backend_api_module_calls_build_commercial_handoff_directly():
    """Backend/API error-message call sites (session_gate, modules/api, and the
    default AI module's routes.py) must resolve their acquisition URL through
    build_public_acquisition_url - the pre-auth-safe resolver - never through
    build_commercial_handoff, which can emit a named instance_id with no handoff
    token an unauthenticated/external context could ever redeem (B1)."""
    targets = [
        _REPO_ROOT / "celerp" / "session_gate.py",
        _REPO_ROOT / "celerp" / "modules" / "api.py",
        _REPO_ROOT / "default_modules" / "celerp-ai" / "celerp_ai" / "routes.py",
    ]
    offenders = []
    for path in targets:
        text = path.read_text()
        if "build_commercial_handoff(" in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert offenders == [], f"direct build_commercial_handoff() call(s): {offenders}"
