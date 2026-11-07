# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: List numbers through the screens a user works in.

New List on a page filtered to one List type makes that type, numbered from its own counter.
A draft List changed to a type with another counter takes that counter's number. On Sales >
Numbering a refused format leaves the row in place with the typed value open and the reason
beside it, so the user can choose another value there.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

from .conftest import wait_for_topbar
from .test_demo_items_link import _seed_demo_items

pytestmark = pytest.mark.browser


def _preview(api, doc_type: str) -> str:
    return next(r["preview"] for r in api.get("/docs/sequences").json() if r["doc_type"] == doc_type)


def test_new_list_on_a_type_filtered_page_makes_that_type(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    expected = _preview(fresh_company, "transfer")
    page.goto(f"{ui_server}/lists?type=transfer")
    wait_for_topbar(page)
    with page.expect_navigation():
        page.locator("button.btn--primary", has_text="New List").click()
    entity_id = page.url.rsplit("/", 1)[-1]
    state = fresh_company.get(f"/lists/{entity_id}").json()
    assert (state["list_type"], state["ref_id"]) == ("transfer", expected)
    assert expected in page.locator("main").inner_text()


def test_a_draft_list_changed_to_another_type_takes_that_types_number(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    r = fresh_company.post("/lists", json={"list_type": "quotation", "status": "draft"})
    assert r.status_code == 200, r.text
    entity_id, quote_ref = r.json()["id"], fresh_company.get(f"/lists/{r.json()['id']}").json()["ref_id"]
    expected = _preview(fresh_company, "audit")
    page.goto(f"{ui_server}/lists/{entity_id}")
    wait_for_topbar(page)
    assert quote_ref in page.locator("main").inner_text()
    with page.expect_navigation():
        page.locator(".list-type-bar select").select_option("audit")
    wait_for_topbar(page)
    main = page.locator("main").inner_text()
    assert expected in main and quote_ref not in main
    assert fresh_company.get(f"/lists/{entity_id}").json()["ref_id"] == expected


def test_a_refused_numbering_format_stays_in_its_row_and_can_be_changed(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    page.goto(f"{ui_server}/settings/sales?tab=numbering")
    wait_for_topbar(page)
    row = page.locator("tr", has=page.locator('[hx-post="/settings/numbering/audit/reset"]'))
    row.locator('[hx-get="/settings/numbering/audit/prefix/edit"]').click()
    field = row.locator('input[name="value"]')
    field.fill("QUO")
    field.press("Tab")
    error = row.locator(".cell-error")
    error.wait_for()
    assert "Quotation" in error.inner_text()
    assert field.input_value() == "QUO" and row.get_by_role("button", name="Reset").is_visible()
    field.fill("AUDX")
    field.press("Tab")
    row.locator(".numbering-preview", has_text="AUDX-").wait_for()
    assert row.locator(".cell-error").count() == 0
    assert _preview(fresh_company, "audit").startswith("AUDX-")
