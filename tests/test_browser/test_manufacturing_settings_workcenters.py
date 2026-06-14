# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests + screenshots: Manufacturing Settings and Work Centers (P6)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def test_manufacturing_settings_page(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    page.goto(f"{ui_server}/settings/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("input[name=hours_per_day]", timeout=10000)
    page.fill("input[name=hours_per_day]", "10")
    page.check("input[name=require_issued_before_complete]")
    page.click("button:has-text('Save')")
    page.wait_for_selector("input[name=hours_per_day]", timeout=8000)

    # Persisted + reflected back in the form.
    assert page.locator("input[name=hours_per_day]").input_value() == "10"
    assert page.locator("input[name=require_issued_before_complete]").is_checked()
    page.screenshot(path=str(SHOTS / "manufacturing-settings.png"), full_page=True)

    mfg = api.get("/companies/me").json()["settings"]["manufacturing"]
    assert mfg["hours_per_day"] == 10 and mfg["require_issued_before_complete"] is True


def test_work_centers_crud(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    page.goto(f"{ui_server}/manufacturing/work-centers", wait_until="domcontentloaded")
    page.wait_for_selector("#wc-table", timeout=10000)

    # Add a work center, then rename it via double-click edit.
    page.click("button:has-text('Add work center')")
    page.wait_for_selector("#wc-table:has-text('New work center')", timeout=8000)
    page.locator("#wc-table .editable-cell").first.dblclick()
    inp = page.locator("#wc-table input[name=value]")
    inp.wait_for(state="visible", timeout=5000)
    inp.fill("Polishing Bench")
    inp.dispatch_event("blur")
    page.wait_for_selector("#wc-table:has-text('Polishing Bench')", timeout=8000)
    page.screenshot(path=str(SHOTS / "work-centers.png"), full_page=True)

    centers = api.get("/manufacturing/work-centers").json()["items"]
    assert any(w["name"] == "Polishing Bench" for w in centers)

    # Delete it (auto-accept the hx_confirm dialog).
    page.on("dialog", lambda d: d.accept())
    page.locator("#wc-table button:has-text('Delete')").first.click()
    page.wait_for_selector("#wc-table:has-text('No work centers yet')", timeout=8000)
    assert api.get("/manufacturing/work-centers").json()["items"] == []
