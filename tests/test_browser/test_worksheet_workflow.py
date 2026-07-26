# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production workflow table: station dropdown, edit, sort/filter, reorder, ref, remove."""
from __future__ import annotations

import base64
import time as _t
from pathlib import Path

import pytest

from .inline_edit import set_cell, settled

pytestmark = pytest.mark.browser

SCOPE = "#workflow-section"
SHOTS = Path("context/reviews/worksheet")

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _steps(api, item):
    return (api.get(f"/items/{item}").json().get("workflow") or {}).get("steps") or []


def _wait(api, item, pred, timeout=8.0):
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if pred(_steps(api, item)):
            return _steps(api, item)
        _t.sleep(0.25)
    raise AssertionError(f"workflow never satisfied predicate; last = {_steps(api, item)}")


def _settled(page):
    return settled(page, SCOPE)


def _set_cell(page, col, value, **kw):
    return set_cell(page, col, value, scope=SCOPE, **kw)


def _pick_station(page, col, value):
    """Double-click the station cell and choose a work center from the combobox."""
    _settled(page)
    page.locator(f'td[data-col="{col}"]').scroll_into_view_if_needed()
    page.dblclick(f'td[data-col="{col}"]')
    box = page.locator("#workflow-section .combobox-input").last
    box.wait_for(state="visible", timeout=4000)
    box.click()
    box.fill(value)
    page.wait_for_selector(".combobox-list.open", timeout=8000)
    page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty):visible").first.click()
    _settled(page)


def test_workflow_station_edit_sortfilter_reorder_ref(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1280, "height": 1500})  # room for the station dropdown below the fold
    item = api.post("/items", json={"sku": "WF-RING", "name": "Workflow Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    # Seed work centers so the Station dropdown has real options.
    api.post("/manufacturing/work-centers", json={"name": "Casting"})
    api.post("/manufacturing/work-centers", json={"name": "Bench"})

    page.goto(f"{ui_server}/inventory/{item}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#workflow-section:has-text('Production workflow')", timeout=10000)

    # Add a step.
    page.click("#workflow-section button:has-text('+ Add step')")
    page.wait_for_selector('td[data-col="workflow__0__station"]', timeout=8000)
    _wait(api, item, lambda s: len(s) == 1)

    # Station via the work-center dropdown.
    _pick_station(page, "workflow__0__station", "Casting")
    _wait(api, item, lambda s: s[0].get("station") == "Casting")

    # Instructions is a multi-line text box: line breaks are preserved.
    _set_cell(page, "workflow__0__instructions", "Pour the melt\nThen cool", kind="textarea")
    _wait(api, item, lambda s: s[0].get("instructions") == "Pour the melt\nThen cool")
    _set_cell(page, "workflow__0__time_value", "12")
    _wait(api, item, lambda s: float(s[0].get("time_value") or 0) == 12.0)

    # Changing the unit must NOT reload the page: a JS marker survives the edit.
    page.evaluate("window.__wfReloadMarker = 'alive'")
    _set_cell(page, "workflow__0__time_unit", "hr", kind="select")
    _wait(api, item, lambda s: float(s[0].get("time_minutes") or 0) == 720.0)
    assert page.evaluate("window.__wfReloadMarker") == "alive", "unit change reloaded the page"

    # Excel sort + filter controls are present on the columns (same components as the rest).
    page.wait_for_selector('#workflow-section th[data-sort="3"]', timeout=8000)        # Time sortable
    page.wait_for_selector('#workflow-section .colfilter[data-col="1"]', timeout=8000)  # Station funnel
    page.locator('#workflow-section .colfilter[data-col="1"]').click()                # opens the filter popup
    page.wait_for_selector(".colfilter-pop", timeout=8000)
    page.keyboard.press("Escape")

    # Second step, then drag-reorder it above the first.
    page.click("#workflow-section button:has-text('+ Add step')")
    page.wait_for_selector('td[data-col="workflow__1__instructions"]', timeout=8000)
    _set_cell(page, "workflow__1__instructions", "Polish", kind="textarea")
    _wait(api, item, lambda s: len(s) == 2 and s[1].get("instructions") == "Polish")
    order_before = [s["id"] for s in _steps(api, item)]
    page.locator("#workflow-section .wf-drag").nth(1).drag_to(page.locator("#workflow-section tr.wf-row").nth(0))
    _wait(api, item, lambda s: [x["id"] for x in s] == list(reversed(order_before)))
    assert _steps(api, item)[0]["instructions"] == "Polish"

    # Drop a reference image onto the first step.
    page.evaluate(
        """([b64]) => {
            const bin = atob(b64); const arr = new Uint8Array(bin.length);
            for (let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
            const file = new File([arr], 'ref.png', {type:'image/png'});
            const dt = new DataTransfer(); dt.items.add(file);
            const cell = document.querySelector('#workflow-section tr.wf-row .wf-ref-drop');
            cell.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles:true, cancelable:true}));
        }""",
        [base64.b64encode(_PNG).decode()],
    )
    _wait(api, item, lambda s: bool(s[0].get("ref_file_id")), timeout=10)
    page.wait_for_selector("#workflow-section .wf-ref-img", timeout=8000)
    page.screenshot(path=str(SHOTS / "workflow.png"), full_page=True)

    # Remove the second step.
    page.locator("#workflow-section tr.wf-row button[title='Remove step']").nth(1).click()
    _wait(api, item, lambda s: len(s) == 1)
