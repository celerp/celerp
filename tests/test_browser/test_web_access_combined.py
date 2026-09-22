# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test of the combined connected Web Access page.

A connected owner in Team-infrastructure grace keeps the grace notice and
infrastructure controls, while the pre-connection Implementation partner action
is absent from both the status surface and tab bar.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.browser

_CARD = "#partner-claim-card"


def _grace_infra_state() -> dict:
    """A local-infra state that is in grace with an external DB configured.

    Drives both _grace_notice (in_grace -> flash--warning banner) and
    _has_team_features (in_grace -> the Team-infrastructure tab is offered).
    Matches the get_local_infra_state() shape (the getter both consume).
    """
    return {
        "has_external_url": True,
        "external_db_entitled": False,
        "in_grace": True,
        "grace_period_ends": "2099-01-01T00:00:00+00:00",
        "has_external_storage": False,
        "external_storage_entitled": True,
        "storage_in_grace": False,
    }


def test_web_access_combined_sections(page, ui_server, monkeypatch):
    """Connected Team/grace state keeps its normal controls and never offers partner claiming."""
    import ui.routes.settings_cloud as sc
    import celerp.gateway.state as gw_state

    async def _fake_relay_state(token):
        # relay_status "connecting" (or token_bound) makes gw_ok True, so the page
        # shows the connected tab set rather than the value-prop landing.
        return ("connecting", "https://demo.celerp.com", "team", False, True, True, True)

    monkeypatch.setattr(sc, "_relay_state", _fake_relay_state)
    monkeypatch.setattr(gw_state, "get_local_infra_state", _grace_infra_state)
    monkeypatch.setattr(gw_state, "get_commercial_mode", lambda: "celerp_direct")

    # ── Status tab: connected controls remain, partner adoption does not ─────
    page.goto(f"{ui_server}/settings/cloud", wait_until="domcontentloaded")
    page.wait_for_selector('a[href*="tab=infrastructure"]', timeout=8000)
    assert page.locator(_CARD).count() == 0
    assert page.locator('a[href="/settings/cloud?tab=partner"]').count() == 0

    # Grace notice (flash--warning banner) on the status tab.
    assert page.locator(".flash.flash--warning").count() >= 1

    # Team-infrastructure tab link present alongside the status content.
    assert page.locator('a[href*="tab=infrastructure"]').count() == 1

    # ── Infrastructure tab: external-DB section + grace banner above it ────────
    page.goto(f"{ui_server}/settings/cloud?tab=infrastructure", wait_until="domcontentloaded")
    page.wait_for_selector("input#db_host", timeout=8000)
    assert page.locator("input#db_host").count() == 1
    assert page.locator(".flash.flash--warning").count() >= 1


@pytest.mark.parametrize(
    ("state", "expect_link", "expect_plans", "expect_partner_tab"),
    [
        (("inactive", "", "", True, True, False, None), True, True, True),
        (("inactive", "", "", False, False, False, None), True, True, False),
        (("inactive", "", "", False, True, False, None), True, True, False),
        (("error", "", "", False, True, False, None), True, True, False),
        (("inactive", "", "cloud", False, True, True, True), False, False, False),
        (("inactive", "", "cloud", False, True, True, False), True, True, False),
    ],
)
def test_web_access_unusable_states_always_offer_subscription_recovery(
    page, ui_server, monkeypatch, state, expect_link, expect_plans, expect_partner_tab
):
    """No non-serving owner state may strand the user without an explicit recovery action."""
    import ui.api_client as api
    import ui.routes.settings_cloud as sc
    import celerp.gateway.state as gw_state

    async def _fake_relay_state(token):
        return state

    async def _empty_commercial_state(request):
        return {}

    monkeypatch.setattr(sc, "_relay_state", _fake_relay_state)
    monkeypatch.setattr(sc, "_commercial_state", _empty_commercial_state)
    monkeypatch.setattr(api, "get_backup_status", AsyncMock(return_value={}))
    monkeypatch.setattr(gw_state, "get_local_infra_state", lambda: {})
    monkeypatch.setattr(gw_state, "get_commercial_mode", lambda: "celerp_direct")

    page.goto(f"{ui_server}/settings/cloud", wait_until="domcontentloaded")
    page.wait_for_selector("#cloud-connect-btn", timeout=8000)

    assert page.locator("#cloud-connect-btn").count() == 1
    assert page.locator(_CARD).count() == 0

    link_recovery = page.locator(
        '[hx-post="/settings/cloud-send-otp"], '
        '[hx-get^="/account/panel?intent=claim"]'
    )
    assert (link_recovery.count() > 0) is expect_link
    assert (page.locator(".cloud-plans").count() > 0) is expect_plans
    assert (
        page.locator('a[href="/settings/cloud?tab=partner"]').count() > 0
    ) is expect_partner_tab
