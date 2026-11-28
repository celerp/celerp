# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""In a real browser, a partner-managed install shows the Celerp Enterprise logo in
the sign-in card and the sidebar, the image loads and fits its box, and the partner
link ending brings the standard logo back on the next page."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser

_PARTNER = {"version": 1, "schema_version": 1, "commercial_mode": "partner_managed",
            "implementation": {"partner_id": "p-1", "display_name": "Partner Co"}}
_DIRECT = {"version": 2, "schema_version": 1, "commercial_mode": "celerp_direct"}


def _store(ctx: dict | None) -> None:
    """Persist the commercial context the way the API does on a self-hosted install."""
    from celerp.config import _update_cloud_config

    def _mutate(cloud: dict) -> None:
        if ctx is None:
            cloud.pop("commercial_context_json", None)
        else:
            cloud["commercial_context_json"] = json.dumps(ctx, separators=(",", ":"))
    _update_cloud_config(_mutate)


@pytest.fixture
def partner_install():
    _store(_PARTNER)
    yield
    _store(None)


def _shown_logo(page, selector: str, box: str) -> str:
    """The logo's src, after checking it loaded and sits inside its container."""
    img = page.locator(selector)
    img.wait_for()
    page.wait_for_function("(el) => el.complete && el.naturalWidth > 0", arg=img.element_handle())
    inner, outer = img.bounding_box(), page.locator(box).bounding_box()
    assert inner["x"] >= outer["x"] - 0.5
    assert inner["x"] + inner["width"] <= outer["x"] + outer["width"] + 0.5
    return img.get_attribute("src")


def test_sign_in_card_shows_the_enterprise_logo_then_the_standard_one(unauthed_page, partner_install):
    unauthed_page.goto("/login")
    assert _shown_logo(unauthed_page, "img.auth-logo", ".auth-card") == "/static/logo-enterprise.png"
    assert unauthed_page.locator("link[rel=icon][type='image/png']").get_attribute(
        "href") == "/static/favicon-enterprise-32x32.png"
    _store(_DIRECT)
    unauthed_page.goto("/login")
    assert _shown_logo(unauthed_page, "img.auth-logo", ".auth-card") == "/static/logo.png"


def test_sidebar_shows_the_enterprise_logo(page, ui_server, partner_install):
    page.goto(f"{ui_server}/dashboard")
    assert _shown_logo(page, "img.sidebar-logo", "#sidebar") == "/static/logo-enterprise.png"
