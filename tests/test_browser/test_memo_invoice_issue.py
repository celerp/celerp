# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An invoice drawn from a consignment memo is edited on its page and issued there. The demo
gemstone catalogue gives a carat parcel, invoiced in part, and a stone sold by the piece: after
the edits the page saves (a price, a discount, a tax, an added line deleted again) the invoice issues,
its stones are sold to the customer and the memo keeps the rest of the parcel."""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from .test_demo_items_link import _seed_demo_items
from .test_fulfillment import _assert_no_crash

from .conftest import answer_in_app

pytestmark = pytest.mark.browser


def _demo_gem(api, sell_by: str) -> dict:
    items = api.get("/items", params={"status": "available", "limit": 500}).json()["items"]
    return next(i for i in items if str(i.get("sku", "")).startswith("DEMO-") and i.get("sell_by") == sell_by
                and float(i.get("quantity") or 0) > 0)


def _issued_memo(api, contact_id: str, items: list[dict]) -> tuple[str, list[str]]:
    lines = [{"item_id": i["id"], "sku": i["sku"], "description": i["sku"], "quantity": float(i["quantity"]),
              "unit_price": 50.0, "sell_by": i["sell_by"]} for i in items]
    memo = api.post("/docs", json={"doc_type": "memo", "contact_id": contact_id, "line_items": lines})
    assert memo.status_code in (200, 201), memo.text
    memo = memo.json()["id"]
    assert api.post(f"/docs/{memo}/finalize").status_code == 200
    lids = [li["line_id"] for li in api.get(f"/docs/{memo}").json()["line_items"]]
    assert api.post(f"/docs/{memo}/fulfill-lines", json={"line_ids": lids}).status_code == 200
    return memo, lids


def _draw(api, memo: str, quantities: dict[str, float]) -> str:
    body = {"line_ids": list(quantities), "quantities": quantities}
    preview = api.post(f"/docs/{memo}/invoice-lines", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    r = api.post(f"/docs/{memo}/invoice-lines", json={**body, "binding": preview.json()["binding"],
                                                    "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()["target_doc_id"]


def _edit(page, field, value: str) -> None:
    """Type a value into a line field and wait for the page's save of the lines to succeed."""
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/lines")) as saved:
        field.fill(value)
        field.blur()
    assert saved.value.ok, saved.value.text()


def test_an_invoice_drawn_from_a_memo_issues_after_page_edits(page, ui_server, fresh_company):
    api = fresh_company
    _seed_demo_items(api, "gemstones")
    parcel, stone = _demo_gem(api, "carat"), _demo_gem(api, "piece")
    customer = api.post("/crm/contacts", json={"name": "Showroom Customer", "contact_type": "customer"}).json()["id"]
    memo, (parcel_line, stone_line) = _issued_memo(api, customer, [parcel, stone])
    invoice = _draw(api, memo, {parcel_line: 10.0, stone_line: float(stone["quantity"])})

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{ui_server}/docs/{invoice}", wait_until="domcontentloaded")
    _assert_no_crash(page, "drawn invoice")
    rows = page.locator("tr[data-drawn='1']")
    expect(rows).to_have_count(2)
    # A drawn row shows its lot's measures without letting them be typed over.
    for field in rows.locator("[data-name='weight'], [data-name='pieces']").all():
        expect(field).to_be_disabled()

    # A price and a discount on the drawn lines, each saved on its own.
    _edit(page, rows.nth(0).locator("[data-name='unit_price']"), "55")
    _edit(page, rows.nth(1).locator("[data-name='discount_pct']"), "5")
    # A tax picked on a drawn line, and an ordinary line added and then deleted again.
    tax = rows.nth(0).locator("[data-name='tax_select']")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/lines")) as saved:
        tax.select_option("Exempt|0.0")
        tax.dispatch_event("blur")
    assert saved.value.ok, saved.value.text()
    page.click("button[onclick='celerpAddLine()']")
    extra = page.locator("table.doc-lines tbody tr:not([data-drawn])").last
    extra.locator("[data-name='description']").fill("Polishing")
    _edit(page, extra.locator("[data-name='unit_price']"), "20")
    assert len(api.get(f"/docs/{invoice}").json()["line_items"]) == 3
    extra.locator(".li-select").check()
    page.select_option("#li-bulk-select", "li-delete")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/lines")) as saved:
        page.click("#li-bulk-delete-btn")
        answer_in_app(page)
    assert saved.value.ok, saved.value.text()

    stored = [li for li in api.get(f"/docs/{invoice}").json()["line_items"] if li.get("drawn_from")]
    assert len(stored) == len(api.get(f"/docs/{invoice}").json()["line_items"]) == 2
    assert [li["unit_price"] for li in stored][0] == 55.0
    assert float(stored[1]["discount_pct"]) == 5.0
    assert all("weight" not in li and "pieces" not in li for li in stored), stored

    page.get_by_role("button", name="Issue Invoice").click()
    expect(page.locator(".badge", has_text="Draft")).to_have_count(0, timeout=15000)
    doc = api.get(f"/docs/{invoice}").json()
    assert doc["status"] not in ("draft", "void"), doc["status"]
    for li in doc["line_items"]:
        lot = api.get(f"/items/{li['item_id']}").json()
        assert lot.get("status_doc_id") == invoice, lot

    left = {li["line_id"]: li for li in api.get(f"/docs/{memo}").json()["line_items"]}
    assert float(left[parcel_line]["quantity"]) == pytest.approx(float(parcel["quantity"]))
    parcel_now = api.get(f"/items/{parcel['id']}").json()
    assert float(parcel_now["quantity"]) == pytest.approx(float(parcel["quantity"]) - 10.0)
    assert parcel_now.get("status_doc_id") == memo
