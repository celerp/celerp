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
