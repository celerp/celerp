# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The status badge of a reserved line follows a saved quantity edit straight away.

A line reserved for its quantity reads "Reserved". Once a draft lowers that quantity, the
line holds more than it needs, and the badge says so as soon as the edit is saved, with no
reload of the page.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def _reserved_draft(api) -> str:
    """A draft invoice whose one line reserved 4 pieces of an item, then went back to draft."""
    sku = f"HBE-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": 10,
                                 "sell_by": "piece", "allow_splitting": True})
    assert r.status_code in {200, 201}, r.text
    item = r.json()["id"]
    r = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": item, "entity_id": item, "sku": sku, "name": sku, "quantity": 4, "unit": "piece",
         "unit_price": 10.0, "line_total": 40.0}]})
    assert r.status_code in {200, 201}, r.text
    doc = r.json()["id"]
    assert api.post(f"/docs/{doc}/finalize").status_code == 200, "finalize"
    [line_id] = [li["line_id"] for li in api.get(f"/docs/{doc}").json()["line_items"]]
    r = api.post(f"/docs/{doc}/reserve-lines", json={"line_ids": [line_id], "new_status": "reserved"})
    assert r.status_code == 200, r.text
    assert api.post(f"/docs/{doc}/revert-to-draft", json={}).status_code == 200
    return doc


def test_lowering_a_reserved_line_updates_its_badge_without_a_reload(page, ui_server, api):
    doc = _reserved_draft(api)
    page.goto(f"{ui_server}/docs/{doc}", wait_until="domcontentloaded")
    row = page.locator("#line-body tr").first
    badge = row.locator(".col-item-status")
    expect(badge).to_contain_text("Reserved", timeout=8000)
    page.evaluate("window.__sameView = true")

    qty = row.locator('[data-name="quantity"]')
    qty.fill("2")
    qty.dispatch_event("input")
    with page.expect_response(lambda r: r.url.endswith("/lines") and r.request.method == "POST") as resp:
        qty.dispatch_event("blur")
    assert resp.value.ok, resp.value.text()

    expect(page.locator("#line-body tr").first.locator(".col-item-status")).to_contain_text(
        "Holds 4, needs 2", timeout=5000)
    assert page.evaluate("window.__sameView === true"), "the page reloaded"
    assert api.get(f"/docs/{doc}").json()["line_holds"]
