# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Double-clicking a line action asks once: the button does nothing more while its confirm
is open or its request is on the way."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _invoice(api) -> str:
    sku = f"DBL-{uuid.uuid4().hex[:6]}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": 1,
                                 "sell_by": "piece"})
    assert r.status_code in {200, 201}, r.text
    item = r.json()["id"]
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 10.0, "line_total": 10.0, "entity_id": item}],
        "total": 10.0})
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code in {200, 201}
    return doc_id


def _bill(api) -> str:
    r = api.post("/docs", json={"doc_type": "bill", "line_items": [
        {"sku": f"DBB-{uuid.uuid4().hex[:6]}", "name": "Goods", "quantity": 2, "unit_price": 5.0}]})
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code == 200
    return doc_id


@pytest.mark.parametrize("make, action, button", [
    (_invoice, "li-reserve", "#li-bulk-reserve-btn"),
    (_invoice, "li-fulfill", "#li-bulk-fulfill-btn button[type=submit]"),
    (_bill, "li-fulfill", "#li-bulk-fulfill-btn button[type=submit]"),
])
@pytest.mark.parametrize("answer", ["accept", "dismiss"])
def test_a_double_click_asks_once(page, ui_server, api, make, action, button, answer):
    doc_id = make(api)
    asked = []
    page.on("dialog", lambda d: (asked.append(d.message), getattr(d, answer)()))
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.locator(".li-select").first.check()
    page.locator("#li-bulk-select").select_option(value=action)
    page.locator(button).dblclick()
    page.wait_for_timeout(1500)
    assert len(asked) == 1, asked
