# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Recovery from a scan-run conflict (a lost response on a committed batch).

The scan bar keys every Add click with a run key so a retried click after a lost
response replays instead of re-adding lines. If that key is reused for a batch
that was never the one it committed, the server refuses with scan_run_conflict.
The client must not mint a fresh key and blindly resubmit against a page that may
already hold the committed lines: it keeps the field, refreshes the visible rows
and the optimistic-lock version from the server, tells the operator to review
before adding again, and never auto-resubmits.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

_FIXED_RUN_KEY = "test-fixed-scan-run-key"


def test_scan_run_conflict_refreshes_list_without_auto_resubmit(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    sku_a = f"CONF-A-{tag}"
    sku_b = f"CONF-B-{tag}"
    bc_a = str(uuid.uuid4().int)[:12]
    bc_b = str(uuid.uuid4().int)[:12]
    api.post("/items", json={"status": "available", "sku": sku_a, "name": "Committed Widget",
             "sell_by": "piece", "quantity": 10, "barcode": bc_a})
    api.post("/items", json={"status": "available", "sku": sku_b, "name": "Never Added Widget",
             "sell_by": "piece", "quantity": 10, "barcode": bc_b})
    list_id = api.post("/lists", json={"list_type": "quotation"}).json()["id"]

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    # Pin the client's run-key generator so the run that commits sku_a and the run that
    # replays it later reuse the EXACT same key - the deterministic way to reproduce "the
    # earlier response for this key was lost": the server still holds the run recorded
    # under that key, so resubmitting a different batch under it must 409 as a conflict,
    # never silently mint a fresh key and add on top.
    page.evaluate(f"window.crypto.randomUUID = () => {_FIXED_RUN_KEY!r}")

    # First run: commits sku_a under the pinned key.
    page.locator("#scan-bar-input").click()
    page.locator("#scan-bar-input").fill(bc_a)
    page.locator("#scan-bar-add").click()
    page.wait_for_selector(f'#line-body [data-name="sku"][value="{sku_a}"]', timeout=8000)

    server_version = api.get(f"/lists/{list_id}").json()["version"]

    # Second run: the field now holds a DIFFERENT batch (sku_b), but the run-key generator
    # is still pinned to the same key the first run already committed under - simulating a
    # click whose earlier acknowledgement never reached the client. The server must reject
    # this as scan_run_conflict rather than replay or double-add.
    page.locator("#scan-bar-input").click()
    page.locator("#scan-bar-input").fill(bc_b)
    page.locator("#scan-bar-add").click()

    # The operator-facing recovery message appears (the refresh succeeded, so this is the
    # "already saved, review before adding again" copy, not the reload-needed fallback).
    page.wait_for_function(
        "() => (document.getElementById('scan-bar-status').textContent || '').includes("
        "'The previous scan was already saved. Review the updated list before adding again.')",
        timeout=8000,
    )

    # The list rows and the optimistic-lock version resynced from the server: sku_a's row
    # (the actually-committed batch) is present exactly once, and sku_b - the batch the
    # conflicting run never committed - never appears.
    page.wait_for_function(
        "(v) => window._celerpListVersion === v || _celerpListVersion === v",
        arg=server_version, timeout=8000,
    )
    assert page.locator(f'#line-body [data-name="sku"][value="{sku_a}"]').count() == 1
    assert page.locator(f'#line-body [data-name="sku"][value="{sku_b}"]').count() == 0

    # The input is preserved, not cleared, for the operator to see and decide what to do
    # next - and nothing was auto-resubmitted on the client's own initiative.
    assert page.locator("#scan-bar-input").input_value() == bc_b
    assert page.locator("#scan-bar-add").is_enabled()

    # Give any latent auto-resubmit a moment to fire, then confirm it did not: no duplicate
    # of the committed line, and the batch that was never acknowledged still never landed.
    page.wait_for_timeout(1200)
    assert page.locator(f'#line-body [data-name="sku"][value="{sku_a}"]').count() == 1
    assert page.locator(f'#line-body [data-name="sku"][value="{sku_b}"]').count() == 0

    # Authoritative check against the server: exactly one committed line, for sku_a only.
    line_items = api.get(f"/lists/{list_id}").json().get("line_items", [])
    skus = [li.get("sku") for li in line_items]
    assert skus == [sku_a], f"server list must hold only the committed line, got {skus}"
