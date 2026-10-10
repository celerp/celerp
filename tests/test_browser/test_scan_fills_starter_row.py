# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Scanning into a new document fills its empty starter row instead of leaving it blank
above the scanned line. Later scans append below, and the duplicate refusal still holds."""
from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.browser


def _seed(api, tag: str, *, allow_splitting=True) -> str:
    sku = f"STR-{tag}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": f"Starter {tag}",
                                 "sell_by": "piece", "quantity": 5, "allow_splitting": allow_splitting})
    assert r.status_code in {200, 201}, r.text
    return sku


def _new_invoice(page, ui_server, api) -> str:
    doc_id = api.post("/docs", json={"doc_type": "invoice", "status": "draft"}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)
    return doc_id


def _scan(page, code: str) -> None:
    page.evaluate("() => { document.querySelector('.scan-bar-status').textContent = ''; }")
    inp = page.locator("#scan-bar-input")
    inp.click()
    inp.fill(code)
    inp.press("Enter")
    page.wait_for_function(
        "() => /[\\u2713\\u2717]/.test(document.querySelector('.scan-bar-status')?.textContent || '')",
        timeout=8000)


def _row_skus(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#line-body tr'))"""
        """.map(r => r.querySelector('[data-name="sku"]')?.value || '')""")


def _stored_skus(api, doc_id: str, expected: int) -> list[str]:
    deadline = time.monotonic() + 8
    while True:
        lines = api.get(f"/docs/{doc_id}").json().get("line_items") or []
        if len(lines) >= expected or time.monotonic() > deadline:
            return [ln.get("sku") for ln in lines]
        time.sleep(0.25)


def test_first_scan_fills_the_starter_row_and_the_next_appends(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    first, second = _seed(api, f"A{tag}"), _seed(api, f"B{tag}")
    doc_id = _new_invoice(page, ui_server, api)
    assert _row_skus(page) == [""], "a new invoice opens with one empty row"

    _scan(page, first)
    assert _row_skus(page) == [first]
    assert _stored_skus(api, doc_id, 1) == [first]

    _scan(page, second)
    assert _row_skus(page) == [first, second]
    assert _stored_skus(api, doc_id, 2) == [first, second]


def test_a_duplicate_scan_into_the_filled_starter_row_is_still_refused(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    sku = _seed(api, f"N{tag}", allow_splitting=False)
    _new_invoice(page, ui_server, api)

    _scan(page, sku)
    assert _row_skus(page) == [sku]
    _scan(page, sku)
    assert "✗" in page.locator(".scan-bar-status").inner_text()
    assert _row_skus(page) == [sku]
