# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production workflow table on the Manufacturing tab: add / edit / unit / wait / reorder / ref."""
from __future__ import annotations

import base64
import time as _t
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

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


def _set_cell(page, col, value, is_select=False):
    """Double-click a workflow cell, edit it, commit. The editor swaps the cell to an
    input/select named 'value' (the td drops data-col), so locate it within the section."""
    for attempt in range(2):
        try:
            page.dblclick(f'td[data-col="{col}"]')
            if is_select:
                sel = page.locator('#workflow-section select[name=value]').last
                sel.wait_for(state="visible", timeout=4000)
                sel.select_option(value)
            else:
                inp = page.locator('#workflow-section input[name=value]').last
                inp.wait_for(state="visible", timeout=4000)
                inp.fill(str(value))
                inp.press("Enter")
            return
        except Exception:
            if attempt:
                raise
            page.wait_for_timeout(600)


def test_workflow_add_edit_reorder_ref(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    item = api.post("/items", json={"sku": "WF-RING", "name": "Workflow Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]

    page.goto(f"{ui_server}/inventory/{item}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#workflow-section:has-text('Production workflow')", timeout=10000)

    # Add a step.
    page.click("#workflow-section button:has-text('+ Add step')")
    page.wait_for_selector('td[data-col="workflow__0__name"]', timeout=8000)
    _wait(api, item, lambda s: len(s) == 1)

    # Edit its text + station.
    _set_cell(page, "workflow__0__name", "Cast")
    _wait(api, item, lambda s: s[0].get("name") == "Cast")
    _set_cell(page, "workflow__0__station", "Casting")
    _wait(api, item, lambda s: s[0].get("station") == "Casting")

    # Time: 12 + unit "hr" -> canonical 720 minutes.
    _set_cell(page, "workflow__0__time_value", "12")
    _wait(api, item, lambda s: float(s[0].get("time_value") or 0) == 12)
    _set_cell(page, "workflow__0__time_unit", "hr", is_select=True)
    _wait(api, item, lambda s: s[0].get("time_unit") == "hr" and float(s[0].get("time_minutes") or 0) == 720.0)

    # Wait flag (unattended) — excluded from active time.
    page.locator('#workflow-section tr.wf-row input[type=checkbox]').first.check()
    _wait(api, item, lambda s: s[0].get("wait") is True)

    # Add a second step and reorder via the drag handle (step 2 above step 1).
    page.click("#workflow-section button:has-text('+ Add step')")
    page.wait_for_selector('td[data-col="workflow__1__name"]', timeout=8000)
    _set_cell(page, "workflow__1__name", "Polish")
    _wait(api, item, lambda s: len(s) == 2 and s[1].get("name") == "Polish")

    order_before = [s["id"] for s in _steps(api, item)]
    handles = page.locator("#workflow-section .wf-drag")
    handles.nth(1).drag_to(page.locator("#workflow-section tr.wf-row").nth(0))
    _wait(api, item, lambda s: [x["id"] for x in s] == list(reversed(order_before)))
    # Polish is now first.
    assert _steps(api, item)[0]["name"] == "Polish"

    # Drop a reference image onto the first step's Reference cell.
    page.evaluate(
        """async ([b64]) => {
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
