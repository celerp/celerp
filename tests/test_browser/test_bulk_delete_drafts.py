# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory bulk bar offers Delete only while every selected row is a draft, and
deleting a selected draft removes it from the list."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _delete_offered(page) -> bool:
    return page.locator('#bulk-action-select option[value="delete"]').evaluate("o => !o.hidden")


def _select(page, item_id: str) -> None:
    row = page.locator(f'input.row-select[value="{item_id}"]')
    row.wait_for(timeout=8000)
    row.click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)


def test_delete_is_offered_only_for_drafts(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    draft_id = api.post("/items", json={"sku": f"BDD-{tag}", "name": "Draft", "sell_by": "piece",
                                        "quantity": 1}).json()["id"]
    stock_id = api.post("/items", json={"sku": f"BDA-{tag}", "name": "Stock", "sell_by": "piece",
                                        "quantity": 1}).json()["id"]
    assert api.post("/items/bulk/make-available", json={"entity_ids": [stock_id]}).status_code == 200

    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/inventory?q={tag}", wait_until="domcontentloaded")
    page.evaluate("window.CelerpSelection && window.CelerpSelection.clear()")

    _select(page, draft_id)
    assert _delete_offered(page)
    _select(page, stock_id)
    assert not _delete_offered(page)

    page.locator(f'input.row-select[value="{stock_id}"]').click()
    assert _delete_offered(page)
    page.locator("#bulk-action-select").select_option("delete")
    page.wait_for_timeout(500)
    assert api.get(f"/items/{draft_id}").status_code == 404
    assert api.get(f"/items/{stock_id}").json()["status"] == "available"


def test_the_row_menu_offers_delete_only_for_a_draft_and_a_refusal_keeps_the_row(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    draft_id = api.post("/items", json={"sku": f"RMD-{tag}", "name": "Draft", "sell_by": "piece",
                                        "quantity": 1}).json()["id"]
    stock_id = api.post("/items", json={"sku": f"RMA-{tag}", "name": "Stock", "sell_by": "piece",
                                        "quantity": 1}).json()["id"]
    assert api.post("/items/bulk/make-available", json={"entity_ids": [stock_id]}).status_code == 200

    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/inventory?q={tag}", wait_until="domcontentloaded")
    draft_row, stock_row = (f"#row-{i.replace(':', '-')}" for i in (draft_id, stock_id))
    page.locator(draft_row).wait_for(timeout=8000)
    assert page.locator(f"{stock_row} .row-menu-item--danger").count() == 0
    assert page.locator(f"{draft_row} .row-menu-item--danger").count() == 1

    # The draft is made available elsewhere after the list was drawn: Delete is refused,
    # the row stays as it was and a toast says why.
    assert api.post("/items/bulk/make-available", json={"entity_ids": [draft_id]}).status_code == 200
    page.locator(f"{draft_row} .row-menu-btn").click()
    page.locator(f"{draft_row} .row-menu-item--danger").click()
    page.locator(".toast-container .toast--error").wait_for(timeout=5000)
    assert page.locator(f"{draft_row} input.row-select").count() == 1
    assert page.locator(f"{draft_row} .flash--error").count() == 0
    assert api.get(f"/items/{draft_id}").json()["status"] == "available"
