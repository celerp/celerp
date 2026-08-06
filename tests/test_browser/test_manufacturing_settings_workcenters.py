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

    # Inline save: every field change posts the whole form, no Save button, no reload.
    # Success surfaces through the shared corner toast, not an inline banner.
    page.fill("input[name=hours_per_day]", "10")
    page.check("input[name=require_issued_before_complete]")
    page.check("input[name=auto_create_work_orders]")
    page.check("input[name=auto_complete_work_orders]")
    page.wait_for_selector(".toast--success", timeout=8000)

    # No reload wiped the form: the values stay in place.
    assert page.locator("input[name=hours_per_day]").input_value() == "10"
    assert page.locator("input[name=require_issued_before_complete]").is_checked()
    assert page.locator("input[name=auto_complete_work_orders]").is_checked()
    page.screenshot(path=str(SHOTS / "manufacturing-settings.png"), full_page=True)

    # Persisted server-side; the last change-triggered save settles the full form.
    import time as _t
    deadline = _t.time() + 8
    mfg = {}
    while _t.time() < deadline:
        mfg = api.get("/companies/me").json()["settings"]["manufacturing"]
        if mfg.get("auto_complete_work_orders") is True:
            break
    assert mfg["hours_per_day"] == 10
    assert mfg["require_issued_before_complete"] is True
    assert mfg["auto_create_work_orders"] is True
    assert mfg["auto_complete_work_orders"] is True


def test_auto_complete_hidden_until_auto_create(page, ui_server, fresh_company):
    # Auto-complete on invoice posting only applies when auto-create is on, so the
    # dependent toggle stays hidden until auto-create is checked. fresh_company
    # isolates from the shared company's persisted auto_create state.
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("input[name=auto_create_work_orders]", timeout=10000)

    auto_create = page.locator("input[name=auto_create_work_orders]")
    auto_complete = page.locator("input[name=auto_complete_work_orders]")

    assert auto_create.is_checked() is False
    assert auto_complete.is_hidden()

    # Turning auto-create on reveals the dependent auto-complete option.
    auto_create.check()
    assert auto_complete.is_visible()

    # Turning it back off hides the dependent option again.
    auto_create.uncheck()
    assert auto_complete.is_hidden()


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
