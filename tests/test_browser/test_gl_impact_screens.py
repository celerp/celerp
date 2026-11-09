# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: GL Impact on the screens a user opens.

A written-off List shows what it posted on its own page, the write-off and the cost
correction that followed it. A paid invoice heads each entry with what posted it and
the invoice number, never an internal entry id.
"""
from __future__ import annotations

from datetime import date

import pytest
from playwright.sync_api import Page, expect

from .conftest import wait_for_topbar
from .test_demo_items_link import _seed_demo_items

pytestmark = pytest.mark.browser


def _demo_company(api) -> None:
    _seed_demo_items(api)
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), r.text


def _stocked_item(api) -> dict:
    items = api.get("/items", params={"limit": 200}).json()["items"]
    return next(i for i in items if float(i.get("quantity") or 0) > 0 and float(i.get("cost_total") or 0) > 0)


def _gl_section(page: Page, url: str):
    page.goto(url)
    wait_for_topbar(page)
    section = page.locator("[id^='doc-gl-impact-'].doc-section")
    expect(section).to_be_visible(timeout=15000)
    return section


def _headings(section) -> list[str]:
    return [h.strip() for h in section.locator(".gl-impact-entry a").all_text_contents()]


def test_a_written_off_list_shows_what_it_posted_on_its_page(page: Page, ui_server, fresh_company):
    api = fresh_company
    _demo_company(api)
    item = _stocked_item(api)
    eid, qty, cost = item["id"], float(item["quantity"]), float(item["cost_total"])
    r = api.post("/lists/writeoff", json={"entity_ids": [eid]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    lst = api.get(f"/lists/{wo}").json()
    line = next(ln for ln in lst["line_items"] if ln["item_id"] == eid)
    assert api.post(f"/lists/{wo}/writeoff-line",
                    json={"line_id": line["line_id"], "qty_out": qty, "account": "6950"}).status_code == 200
    assert api.post(f"/lists/{wo}/finalize").status_code == 200
    assert api.post(f"/lists/{wo}/write-off").status_code == 200
    r = api.patch(f"/items/{eid}", json={"fields_changed": {"cost_total": {"old": cost, "new": round(cost * 1.5, 2)}}})
    assert r.status_code == 200, r.text

    section = _gl_section(page, f"{ui_server}/lists/{wo}")
    headings = _headings(section)
    assert len(headings) == 2, headings
    assert all(lst["ref_id"] in h and not h.startswith("je:") for h in headings), headings
    expect(section.locator("td", has_text="6950").first).to_be_visible()


def test_a_paid_invoice_heads_each_entry_with_what_posted_it(page: Page, ui_server, fresh_company):
    api = fresh_company
    _demo_company(api)
    contact = next(c for c in api.get("/crm/contacts", params={"limit": 200}).json()["items"]
                   if c.get("contact_type") in ("customer", "both"))
    item = _stocked_item(api)
    r = api.post("/docs", json={
        "doc_type": "invoice", "contact_id": contact["id"],
        "line_items": [{"item_id": item["id"], "sku": item.get("sku"), "name": item.get("name"),
                        "quantity": 1, "unit_price": 50.0, "line_total": 50.0}],
        "subtotal": 50.0, "tax": 0.0, "total": 50.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code == 200
    doc = api.get(f"/docs/{doc_id}").json()
    r = api.post(f"/docs/{doc_id}/payment", json={"amount": doc["amount_outstanding"], "method": "cash", "bank_account": "1111",
                                                   "payment_date": date.today().isoformat()})
    assert r.status_code == 200, r.text
    ref = doc["ref_id"]

    section = _gl_section(page, f"{ui_server}/docs/{doc_id}")
    headings = _headings(section)
    assert len(headings) >= 2, headings
    assert all(h.endswith(ref) and h != ref and "je:" not in h for h in headings), headings
    # Newest first, as the Journal lists them: the payment heads the section, the invoice follows.
    from ui.routes.documents import _gl_entry_heading
    posted = api.get(f"/accounting/journal/by-doc/{doc_id}").json()["entries"]
    assert headings == [_gl_entry_heading(e, ref) for e in reversed(posted)], headings
