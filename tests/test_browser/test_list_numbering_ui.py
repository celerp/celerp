# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: List numbers through the screens a user works in.

New List on a page filtered to one List type makes that type, numbered from its own counter.
A draft List changed to a type with another counter takes that counter's number. On Sales >
Numbering a refused format leaves the row in place with the typed value open and the reason
beside it in the user's language, so the user can choose another value there, and Reset shows
the number the next document gets. A quotation List converted from its page falls due by its
payment term from the day the document is issued. A duplicate of a sent, converted quotation is a
fresh draft that can be marked as sent and converted, and a void quotation is converted the way
its refusal advises: duplicate it and convert the copy.
"""
from __future__ import annotations

import re

import pytest
from datetime import date, timedelta

from playwright.sync_api import Page, expect

from .conftest import htmx_settled, wait_for_topbar
from .test_demo_items_link import _seed_demo_items
from ui.i18n import t

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
    htmx_settled(field)
    field.fill("QUO")
    field.press("Tab")
    error = row.locator(".cell-error")
    error.wait_for()
    assert "Quotation" in error.inner_text()
    assert field.input_value() == "QUO" and row.get_by_role("button", name="Reset").is_visible()
    htmx_settled(row)
    field.fill("AUDX")
    field.press("Tab")
    row.locator(".numbering-preview", has_text="AUDX-").wait_for()
    assert row.locator(".cell-error").count() == 0
    assert _preview(fresh_company, "audit").startswith("AUDX-")


def _numbering_row(page: Page, ui_server: str, doc_type: str):
    page.goto(f"{ui_server}/settings/sales?tab=numbering")
    wait_for_topbar(page)
    return page.locator("tr", has=page.locator(f'[hx-post="/settings/numbering/{doc_type}/reset"]'))


def test_a_refused_numbering_pattern_reads_in_the_users_language(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "th", "domain": host, "path": "/"}])
    try:
        row = _numbering_row(page, ui_server, "audit")
        for pattern, key, params in (("{PREFIX}-{YY}{MM}", "documents.err_numbering_no_sequence_token",
                                      {"example": "{####}"}),
                                     ("{PREFIX}-{FOO}-{####}", "documents.err_numbering_unknown_tokens",
                                      {"tokens": "{FOO}"})):
            cell = row.locator('[hx-get="/settings/numbering/audit/pattern/edit"]')
            if cell.count():
                cell.click()
            field = row.locator('input[name="value"]')
            htmx_settled(field)
            field.fill(pattern)
            field.press("Tab")
            # Each refusal replaces the one before it, so wait for this pattern's own text.
            expect(row.locator(".cell-error")).to_have_text(
                re.compile(rf"^\s*{re.escape(t(key, 'th', **params))}\s*$", re.I))
            assert field.input_value() == pattern
    finally:
        page.context.clear_cookies(name="celerp_lang")
    assert _preview(fresh_company, "audit").startswith("AUD-")


def _quotation(api) -> str:
    r = api.post("/docs", json={"doc_type": "quotation", "contact_name": "Buyer",
                                "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}]})
    assert r.status_code == 200, r.text
    return api.get(f"/docs/{r.json()['id']}").json()["ref_id"]


def test_reset_shows_the_number_the_next_document_gets(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    held = [_quotation(fresh_company) for _ in range(5)]
    row = _numbering_row(page, ui_server, "quotation")
    row.get_by_role("button", name="Reset").click()
    shown = row.locator(".numbering-preview")
    page.wait_for_function("el => el.textContent.endsWith('-0006')", arg=shown.element_handle())
    preview = shown.inner_text().strip()
    assert preview not in held
    assert _quotation(fresh_company) == preview


def test_a_quotation_list_converted_from_its_page_falls_due_by_its_term(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    r = fresh_company.post("/lists", json={
        "list_type": "quotation", "contact_name": "Buyer", "issue_date": "2026-10-01", "due_date": "2026-10-31",
        "payment_terms": "Net 30", "line_items": [{"name": "A", "quantity": 2, "unit_price": 50}]})
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    assert fresh_company.post(f"/lists/{entity_id}/finalize").status_code == 200
    page.goto(f"{ui_server}/lists/{entity_id}")
    wait_for_topbar(page)
    with page.expect_navigation():
        page.get_by_role("button", name=t("btn.convert", "en"), exact=True).click()
    wait_for_topbar(page)
    doc = fresh_company.get(f"/docs/{page.url.rsplit('/', 1)[-1]}").json()
    issued = date.fromisoformat(doc["issue_date"])
    assert (doc["source_list_id"], doc["doc_type"]) == (entity_id, "invoice")
    assert issued > date(2026, 10, 1) and doc["due_date"] == (issued + timedelta(days=30)).isoformat()


def test_converting_a_list_that_already_became_a_document_says_so_in_the_users_language(
        page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    r = fresh_company.post("/lists", json={
        "list_type": "quotation", "contact_name": "Buyer",
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 50}]})
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    assert fresh_company.post(f"/lists/{entity_id}/finalize").status_code == 200
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "th", "domain": host, "path": "/"}])
    try:
        page.goto(f"{ui_server}/lists/{entity_id}")
        wait_for_topbar(page)
        # Another session converts it while this page is still open.
        done = fresh_company.post(f"/lists/{entity_id}/convert", json={"target_type": "invoice"})
        assert done.status_code == 200, done.text
        number = fresh_company.get(f"/docs/{done.json()['target_doc_id']}").json()["ref_id"]
        page.get_by_role("button", name=t("btn.convert", "th"), exact=True).click()
        toast = page.locator(".toast-container .toast--error").last
        expect(toast).to_contain_text(t("documents.err_list_already_converted", "th", number=number))
        assert page.url.endswith(f"/lists/{entity_id}")
    finally:
        page.context.clear_cookies(name="celerp_lang")


def _click_to_next_page(page: Page, label: str) -> str:
    with page.expect_navigation():
        page.get_by_role("button", name=label, exact=True).click()
    wait_for_topbar(page)
    return page.url.rsplit("/", 1)[-1]


def test_a_duplicate_of_a_sent_converted_quotation_starts_fresh_and_converts(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    r = fresh_company.post("/lists", json={
        "list_type": "quotation", "contact_name": "Buyer",
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 50}]})
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    page.goto(f"{ui_server}/lists/{entity_id}")
    wait_for_topbar(page)
    _click_to_next_page(page, t("documents.issue", "en"))
    _click_to_next_page(page, t("btn.mark_as_sent", "en"))
    expect(page.get_by_role("button", name=t("btn.unmark_sent", "en"), exact=True)).to_be_visible()
    _click_to_next_page(page, t("btn.convert", "en"))
    page.goto(f"{ui_server}/lists/{entity_id}")
    wait_for_topbar(page)
    expect(page.get_by_text(t("doc.converted_to", "en"))).to_be_visible()

    copy = _click_to_next_page(page, t("btn.duplicate", "en"))
    assert copy != entity_id
    expect(page.get_by_text(t("doc.converted_to", "en"))).to_have_count(0)
    _click_to_next_page(page, t("documents.issue", "en"))
    expect(page.get_by_role("button", name=t("btn.mark_as_sent", "en"), exact=True)).to_be_visible()
    expect(page.get_by_role("button", name=t("btn.unmark_sent", "en"), exact=True)).to_have_count(0)
    doc_id = _click_to_next_page(page, t("btn.convert", "en"))
    doc = fresh_company.get(f"/docs/{doc_id}").json()
    assert (doc["source_list_id"], doc["doc_type"]) == (copy, "invoice")


def test_a_void_quotation_is_converted_the_way_its_refusal_advises(page: Page, ui_server, fresh_company):
    _seed_demo_items(fresh_company)
    r = fresh_company.post("/lists", json={
        "list_type": "quotation", "contact_name": "Buyer",
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 50}]})
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    assert fresh_company.post(f"/lists/{entity_id}/finalize").status_code == 200
    assert fresh_company.post(f"/lists/{entity_id}/send", json={"sent_via": "manual"}).status_code == 200
    assert fresh_company.post(f"/lists/{entity_id}/void", json={"reason": "Replaced"}).status_code == 200
    refused = fresh_company.post(f"/lists/{entity_id}/convert", json={"target_type": "invoice"})
    assert refused.status_code == 409 and refused.json()["detail"]["message_key"] == "documents.err_list_convert_status"
    # Duplicate it and convert the copy, from the List's own page.
    page.goto(f"{ui_server}/lists/{entity_id}")
    wait_for_topbar(page)
    copy = _click_to_next_page(page, t("btn.duplicate", "en"))
    _click_to_next_page(page, t("documents.issue", "en"))
    expect(page.get_by_role("button", name=t("btn.mark_as_sent", "en"), exact=True)).to_be_visible()
    doc_id = _click_to_next_page(page, t("btn.convert", "en"))
    doc = fresh_company.get(f"/docs/{doc_id}").json()
    assert (doc["source_list_id"], doc["doc_type"]) == (copy, "invoice")


def test_new_document_at_a_counter_out_of_numbers_says_why(page: Page, ui_server, fresh_company):
    """A New Document whose next number is too long to store shows the refusal and stays on the page."""
    _seed_demo_items(fresh_company)
    r = fresh_company.patch("/docs/sequences/quotation",
                            json={"prefix": "Q" * 95, "pattern": "{PREFIX}-{####}", "next": 9999})
    assert r.status_code == 200, r.text
    assert fresh_company.post("/docs", json={"doc_type": "quotation", "status": "draft"}).status_code == 200
    page.goto(f"{ui_server}/docs?type=quotation")
    wait_for_topbar(page)
    with page.expect_response(lambda r: "/docs/create-blank" in r.url) as reply:
        page.locator('[hx-post="/docs/create-blank?type=quotation"]').click()
    assert reply.value.status == 422
    expect(page.locator("#global-ui-error")).to_contain_text("101")
    assert page.url.split("?", 1)[0].endswith("/docs"), page.url
