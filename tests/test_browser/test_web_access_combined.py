# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test of the combined Web Access page.

Exercises the three commercial surfaces coexisting after the stacked-PR merge:
the partner-claim card, the grace notice, and the Team-infrastructure tab, all
on one /settings/cloud page for an owner on a connected install that is in grace
with team infrastructure. This is the BLOCKER-level proof (P6) that the merge
dropped none of the surfaces and that the claim-card polish (labelled input,
real spinner) rendered.

Red at merge-base (origin/main): the commercial Web Access surface does not exist,
so the status tab renders no #partner-claim-card, no .flash--warning grace banner
and no ?tab=infrastructure anchor, and ?tab=infrastructure renders no external-DB
section - every assertion fails.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_CARD = "#partner-claim-card"


def _grace_db_state() -> dict:
    """A packaged-db state that is in grace with an external DB configured.

    Drives both _grace_notice (in_grace -> flash--warning banner) and
    _has_team_features (in_grace -> the Team-infrastructure tab is offered).
    """
    return {
        "db_mode": "external",
        "has_external_url": True,
        "external_db_entitled": False,
        "in_grace": True,
        "grace_period_ends": "2099-01-01T00:00:00+00:00",
        "storage_mode": "local",
        "has_external_storage": False,
        "external_storage_entitled": True,
        "storage_in_grace": False,
    }


def test_web_access_combined_sections(page, ui_server, monkeypatch):
    """Owner on a connected install in grace sees the claim card, grace banner and
    infrastructure tab together on the status tab; the infrastructure tab renders
    the external-DB section with the grace banner above it."""
    import ui.routes.settings_cloud as sc
    import celerp.gateway.state as gw_state

    async def _fake_relay_state(token):
        # relay_status "connecting" (or token_bound) makes gw_ok True, so the page
        # shows the connected tab set rather than the value-prop landing.
        return ("connecting", "https://demo.celerp.com", "team", False, True)

    monkeypatch.setattr(sc, "_relay_state", _fake_relay_state)
    monkeypatch.setattr(gw_state, "get_packaged_db_state", _grace_db_state)
    monkeypatch.setattr(gw_state, "get_commercial_mode", lambda: "direct")

    # ── Status tab: the three surfaces coexist ────────────────────────────────
    page.goto(f"{ui_server}/settings/cloud", wait_until="domcontentloaded")
    page.wait_for_selector(_CARD, timeout=8000)

    # Partner-claim card with a labelled input (placeholder-only -> real <label>).
    assert page.locator(f'{_CARD} input[name="claim_token"]').count() == 1
    assert page.locator(f'{_CARD} input#claim_token').count() == 1
    assert page.locator(f'{_CARD} label[for="claim_token"]').count() == 1
    # Real rendering spinner (opacity-only .htmx-indicator -> .spinner animation).
    assert page.locator(f'{_CARD} #partner-claim-spinner.spinner').count() == 1

    # Grace notice (flash--warning banner) on the status tab.
    assert page.locator(".flash.flash--warning").count() >= 1

    # Team-infrastructure tab link present alongside the status content.
    assert page.locator('a[href*="tab=infrastructure"]').count() == 1

    # ── Infrastructure tab: external-DB section + grace banner above it ────────
    page.goto(f"{ui_server}/settings/cloud?tab=infrastructure", wait_until="domcontentloaded")
    page.wait_for_selector("input#db_host", timeout=8000)
    assert page.locator("input#db_host").count() == 1
    assert page.locator(".flash.flash--warning").count() >= 1
