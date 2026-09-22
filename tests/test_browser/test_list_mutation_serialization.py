# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Same-tab List writes must not stale-conflict with each other."""
from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.browser


def test_header_patch_waits_for_inflight_line_save(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    sku = f"SER-{tag}"
    item = api.post("/items", json={
        "status": "available",
        "sku": sku,
        "name": f"Serialized Widget {tag}",
        "sell_by": "piece",
        "quantity": 10,
        "retail_price": 10,
    }).json()
    item_id = item.get("id") or item.get("entity_id")
    created = api.post("/lists", json={
        "list_type": "quotation",
        "reference": "before",
        "line_items": [{
            "item_id": item_id,
            "sku": sku,
            "description": f"Serialized Widget {tag}",
            "quantity": 1,
            "unit_price": 10,
            "line_total": 10,
        }],
    })
    assert created.status_code in {200, 201}, created.text
    list_id = created.json()["id"]

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector('#line-body [data-name="quantity"]', timeout=8000)

    # Hold the line-save fetch after the page has queued it. A same-tab header
    # patch must wait behind this request instead of committing a new List version
    # and making the older line save stale.
    page.evaluate(
        """() => {
            const realFetch = window.fetch.bind(window);
            window.__lineSaveStarted = false;
            window.__releaseLineSave = null;
            window.fetch = function(input, init) {
                const url = typeof input === 'string' ? input : input.url;
                if (url.includes('/lists/') && url.endsWith('/lines')) {
                    window.__lineSaveStarted = true;
                    return new Promise((resolve, reject) => {
                        window.__releaseLineSave = () => realFetch(input, init).then(resolve, reject);
                    });
                }
                return realFetch(input, init);
            };
        }"""
    )

    qty = page.locator('#line-body [data-name="quantity"]').first
    qty.fill("2")
    page.evaluate("() => { window.__heldSave = _celerpPersist(); }")
    page.wait_for_function("() => window.__lineSaveStarted === true", timeout=5000)

    # Use the real inline-editor route, but mount its fragment directly so this
    # regression does not depend on where Reference happens to be laid out.
    page.evaluate(
        """async (url) => {
            const host = document.createElement('div');
            host.id = 'serialization-header-editor';
            document.body.appendChild(host);
            const response = await fetch(url);
            host.outerHTML = await response.text();
            const editors = document.querySelectorAll('.editable-cell--editing');
            const mounted = editors[editors.length - 1];
            if (mounted) htmx.process(mounted);
        }""",
        f"/lists/{list_id}/field/reference/edit",
    )
    editor = page.locator('.editable-cell--editing input[name="value"]').last
    editor.fill("after")
    editor.blur()

    # The delayed blur has fired, but the held line save still owns the mutation
    # queue. Without serialization the scalar PATCH commits here and advances the
    # server version ahead of the line save.
    page.wait_for_timeout(500)
    held = api.get(f"/lists/{list_id}").json()
    assert held.get("reference") == "before"
    assert float(held["line_items"][0]["quantity"]) == 1.0

    page.evaluate("() => window.__releaseLineSave()")

    deadline = time.time() + 8
    state = api.get(f"/lists/{list_id}").json()
    while time.time() < deadline:
        if state.get("reference") == "after" and float(state["line_items"][0]["quantity"]) == 2.0:
            break
        time.sleep(0.1)
        state = api.get(f"/lists/{list_id}").json()

    assert state.get("reference") == "after"
    assert float(state["line_items"][0]["quantity"]) == 2.0


def _scan_serialization_fixture(api):
    tag = uuid.uuid4().hex[:6]
    existing_sku = f"SER-EDIT-{tag}"
    scanned_sku = f"SER-SCAN-{tag}"
    scanned_barcode = str(uuid.uuid4().int)[:12]
    existing = api.post("/items", json={
        "status": "available",
        "sku": existing_sku,
        "name": f"Edited Widget {tag}",
        "sell_by": "piece",
        "quantity": 10,
        "retail_price": 10,
    }).json()
    existing_id = existing.get("id") or existing.get("entity_id")
    api.post("/items", json={
        "status": "available",
        "sku": scanned_sku,
        "name": f"Scanned Widget {tag}",
        "sell_by": "piece",
        "quantity": 10,
        "retail_price": 20,
        "barcode": scanned_barcode,
    })
    created = api.post("/lists", json={
        "list_type": "quotation",
        "line_items": [{
            "item_id": existing_id,
            "sku": existing_sku,
            "description": f"Edited Widget {tag}",
            "quantity": 1,
            "unit_price": 10,
            "line_total": 10,
        }],
    })
    assert created.status_code in {200, 201}, created.text
    return created.json()["id"], existing_sku, scanned_sku, scanned_barcode


@pytest.mark.parametrize("save_state", ["debounced", "inflight"])
def test_scan_waits_for_draft_line_state(page, ui_server, api, save_state):
    list_id, existing_sku, scanned_sku, scanned_barcode = _scan_serialization_fixture(api)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector('#line-body [data-name="quantity"]', timeout=8000)

    page.evaluate(
        """() => {
            const realFetch = window.fetch.bind(window);
            window.__lineSaveStarted = 0;
            window.__scanStarted = 0;
            window.__releaseLineSave = null;
            window.__holdNextLineSave = true;
            window.fetch = function(input, init) {
                const url = typeof input === 'string' ? input : input.url;
                if (url.includes('/lists/') && url.endsWith('/lines')) {
                    window.__lineSaveStarted += 1;
                    if (window.__holdNextLineSave) {
                        window.__holdNextLineSave = false;
                        return new Promise((resolve, reject) => {
                            window.__releaseLineSave = () => realFetch(input, init).then(resolve, reject);
                        });
                    }
                }
                if (url.includes('/lists/') && url.endsWith('/scan')) {
                    window.__scanStarted += 1;
                }
                return realFetch(input, init);
            };
        }"""
    )

    qty = page.locator('#line-body [data-name="quantity"]').first
    scan = page.locator("#scan-bar-input")
    qty.fill("2")

    if save_state == "inflight":
        # Move focus first so the real blur/autosave has marked this DOM edit,
        # then replace that pending debounce with a deliberately held save.
        scan.fill(scanned_barcode)
        page.evaluate(
            "() => { clearTimeout(_celerpSaveTimer); _celerpSaveTimer = null; "
            "window.__heldSave = _celerpPersist(); }"
        )
        page.wait_for_function("() => window.__lineSaveStarted === 1", timeout=5000)
        page.locator("#scan-bar-add").click()
    else:
        # Start the real debounce and click Add in the same browser task so the
        # timer cannot win just because the test runner is slow.
        page.evaluate(
            """(barcode) => {
                const input = document.getElementById('scan-bar-input');
                input.value = barcode;
                celerpAutoSave();
                document.getElementById('scan-bar-add').click();
            }""",
            scanned_barcode,
        )
    page.wait_for_function("() => window.__lineSaveStarted === 1", timeout=5000)

    # Scan must not overtake either a held line save or the forced flush of a
    # debounced edit. The persisted List is still the original state here.
    assert page.evaluate("() => window.__scanStarted") == 0
    held = api.get(f"/lists/{list_id}").json()
    edited = next(li for li in held["line_items"] if li.get("sku") == existing_sku)
    assert float(edited["quantity"]) == 1.0
    assert scanned_sku not in {li.get("sku") for li in held["line_items"]}

    page.evaluate("() => window.__releaseLineSave()")
    page.wait_for_function("() => window.__scanStarted === 1", timeout=8000)

    deadline = time.time() + 8
    state = api.get(f"/lists/{list_id}").json()
    while time.time() < deadline:
        lines = state.get("line_items", [])
        edited = next((li for li in lines if li.get("sku") == existing_sku), None)
        if edited and float(edited["quantity"]) == 2.0 and any(
            li.get("sku") == scanned_sku for li in lines
        ):
            break
        time.sleep(0.1)
        state = api.get(f"/lists/{list_id}").json()

    lines = state["line_items"]
    edited = next(li for li in lines if li.get("sku") == existing_sku)
    assert float(edited["quantity"]) == 2.0
    assert any(li.get("sku") == scanned_sku for li in lines)
    assert page.evaluate("() => window.__lineSaveStarted") == 1


def test_scan_aborts_when_draft_line_flush_fails(page, ui_server, api):
    list_id, existing_sku, scanned_sku, scanned_barcode = _scan_serialization_fixture(api)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector('#line-body [data-name="quantity"]', timeout=8000)

    page.evaluate(
        """() => {
            const realFetch = window.fetch.bind(window);
            window.__scanStarted = 0;
            window.fetch = function(input, init) {
                const url = typeof input === 'string' ? input : input.url;
                if (url.includes('/lists/') && url.endsWith('/scan')) window.__scanStarted += 1;
                return realFetch(input, init);
            };
        }"""
    )

    page.locator('#line-body [data-name="quantity"]').first.fill("")
    page.locator("#scan-bar-input").fill(scanned_barcode)
    page.locator("#scan-bar-add").click()
    page.wait_for_timeout(700)

    assert page.evaluate("() => window.__scanStarted") == 0
    state = api.get(f"/lists/{list_id}").json()
    edited = next(li for li in state["line_items"] if li.get("sku") == existing_sku)
    assert float(edited["quantity"]) == 1.0
    assert scanned_sku not in {li.get("sku") for li in state["line_items"]}
