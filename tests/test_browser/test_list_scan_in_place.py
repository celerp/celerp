# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Scanning into a building list accumulates codes client-side (Enter appends a comma, never a
per-scan request) and submits the whole run with one Add button. The lines appear without a page
reload and the scan bar keeps focus, so a fast run of scans can never glue two barcodes together.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def test_building_list_scan_accumulates_then_add_submits_without_reload(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    barcode = str(uuid.uuid4().int)[:12]
    api.post("/items", json={"status": "available", "sku": f"SCN-{tag}", "name": "Scan Widget", "sell_by": "piece",
             "quantity": 10, "barcode": barcode})
    list_id = api.post("/lists", json={"list_type": "quotation"}).json()["id"]

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    # A reload would wipe this flag; it surviving proves both the accumulate and the Add were in place.
    page.evaluate("window.__scanProbe = 1")

    page.locator("#scan-bar-input").click()
    page.locator("#scan-bar-input").fill(barcode)
    page.locator("#scan-bar-input").press("Enter")

    # Enter is a separator, not a submit: the code stays in the field behind a comma and nothing was
    # sent, so no line has appeared yet.
    assert page.locator("#scan-bar-input").input_value().rstrip().rstrip(",").strip() == barcode
    assert page.locator(f'#line-body [data-name="sku"][value="SCN-{tag}"]').count() == 0

    # The Add button submits the whole run in one request; the line lands without a navigation.
    page.locator("#scan-bar-add").click()
    page.wait_for_selector(f'#line-body [data-name="sku"][value="SCN-{tag}"]', timeout=8000)
    assert page.evaluate("window.__scanProbe") == 1, "page reloaded on scan"
    # Focus stays on the scan bar, cleared and ready for the next run.
    assert page.evaluate("document.activeElement && document.activeElement.id") == "scan-bar-input"
    assert page.locator("#scan-bar-input").input_value() == ""


def test_rapid_scans_never_concatenate(page, ui_server, api):
    """The reported bug: two barcodes scanned quickly glued into one invalid code. Enter appending a
    comma makes that impossible - each scan sits behind its own separator, and Add submits them all."""
    tag = uuid.uuid4().hex[:6]
    bc_a = str(uuid.uuid4().int)[:12]
    bc_b = str(uuid.uuid4().int)[:12]
    api.post("/items", json={"status": "available", "sku": f"RA-{tag}", "name": "A", "sell_by": "piece",
             "quantity": 5, "barcode": bc_a})
    api.post("/items", json={"status": "available", "sku": f"RB-{tag}", "name": "B", "sell_by": "piece",
             "quantity": 5, "barcode": bc_b})
    list_id = api.post("/lists", json={"list_type": "quotation"}).json()["id"]

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    inp = page.locator("#scan-bar-input")
    inp.click()
    # Simulate a scanner firing the second barcode before any request could have returned: append the
    # second code straight onto the field right after the first Enter, with no wait.
    inp.fill(bc_a)
    inp.press("Enter")
    page.evaluate("(c) => { const i = document.getElementById('scan-bar-input'); i.value += c; }", bc_b)
    inp.press("Enter")

    # Both codes are present and comma-separated - never the glued bc_a+bc_b.
    val = inp.input_value()
    assert bc_a in val and bc_b in val
    assert (bc_a + bc_b) not in val

    page.locator("#scan-bar-add").click()
    page.wait_for_selector(f'#line-body [data-name="sku"][value="RA-{tag}"]', timeout=8000)
    page.wait_for_selector(f'#line-body [data-name="sku"][value="RB-{tag}"]', timeout=8000)
    assert page.locator("#scan-bar-input").input_value() == ""
