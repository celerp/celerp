# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bulk action bar shows only while something is selected (or, on the bank page, while it
carries the last post's outcome and its Undo). Every list page with a bulk bar loads with
nothing selected, so no bar may be visible there; ticking a row brings it up. The bank page
needs an imported statement for its bar, so its journey in test_bank_page.py holds both."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from playwright.sync_api import expect

from .conftest import htmx_idle
pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def lists_with_rows(api):
    """One row on every list that carries a bulk bar, so the bar and its checkboxes render."""
    tag = uuid.uuid4().hex[:6]
    r = api.post("/items", json={"sku": f"BAR-{tag}", "sell_by": "piece", "name": f"Bar {tag}", "quantity": 1})
    assert r.status_code in {200, 201}, r.text
    r = api.post("/crm/contacts", json={"name": f"Bar Contact {tag}", "contact_type": "customer"})
    assert r.status_code in {200, 201}, r.text
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": []})
    assert r.status_code in {200, 201}, r.text


@pytest.mark.parametrize("path", ["/inventory", "/contacts", "/docs"])
def test_bulk_bar_hidden_with_nothing_selected(page, ui_server, lists_with_rows, path):
    page.goto(f"{ui_server}{path}", wait_until="load")
    htmx_idle(page)
    bars = page.locator(".bulk-toolbar")
    assert bars.count() > 0, f"{path} renders no bulk bar"
    assert page.locator("input.row-select:checked").count() == 0
    for i in range(bars.count()):
        expect(bars.nth(i)).to_be_hidden()


def test_bulk_bar_shows_once_a_row_is_ticked(page, ui_server, lists_with_rows):
    page.goto(f"{ui_server}/inventory", wait_until="load")
    htmx_idle(page)
    page.locator("input.row-select").first.check()
    expect(page.locator("#bulk-toolbar")).to_be_visible()


# ── After an action, its outcome is on screen ────────────────────────────────────────────
# Every bulk action of the item list, with what its form sends beside the ticked rows, the
# API call its outcome comes from and that call's answer when it works. A selection the
# action needs comes from `bulk_items` (two rows are ticked; `{first}` is the first one).
_DELETED = {"deleted": 2, "moved_to_deleted": 0, "items": []}
_BULK_ACTIONS = {
    "/api/items/bulk/status": ({"bulk_status": "archived"}, "bulk_set_status", {"updated": 2}),
    "/api/items/bulk/make-available": ({}, "make_items_available", {"updated": 2}),
    "/api/items/bulk/revert-to-draft": ({}, "revert_items_to_draft", {"updated": 2}),
    "/api/items/bulk/transfer": ({"bulk_location_id": "loc:any"}, "bulk_transfer", {"updated": 2}),
    "/api/items/bulk/delete": ({}, "bulk_delete", _DELETED),
    "/api/items/bulk/restore-deleted": ({}, "bulk_restore_deleted", {"restored": 2}),
    "/api/items/bulk/expire": ({}, "bulk_expire", {"expired": 2}),
    "/api/items/bulk/duplicate": ({}, "create_item", {}),
    "/api/items/bulk/merge": ({"target_sku_from": "{first}"}, "merge_items", {}),
    "/api/items/bulk/split": ({"entity_id": "{first}", "child_qty": "1"}, "split_item", {}),
    "/api/items/bulk/transform": ({"entity_id": "{first}", "child_qty": "1", "child_sku": "OUT-CHILD"},
                                  "transform_item", {"child_sku": "OUT-CHILD", "parent_sku": "OUT-PARENT"}),
    # Opens the new write-off instead of answering on the list.
    "/api/items/bulk/write-off": ({}, "create_writeoff", {"id": "list:none"}),
}
# Run from a connected store's own menu, and answered by a full page refresh
# (test_a_store_action_that_worked_says_so_after_the_page_reloads).
_STORE_ACTIONS = "/api/items/bulk/channel-sync/{platform}/{action}"
# Where an action can answer: the bar's result line, a toast, or the page's error banner.
_OUTCOME = "#bulk-action-result .flash:visible, #toast-container .toast__msg:visible, #global-ui-error:visible"
_POST_TICKED_JS = """([url, values]) => {
  var form = document.createElement('form');
  var add = function (name, value) {
    var i = document.createElement('input'); i.type = 'hidden'; i.name = name; i.value = value; form.appendChild(i);
  };
  CelerpSelection.ids().forEach(function (id) { add('selected', id); });
  Object.keys(values).forEach(function (name) { add(name, values[name].replace('{first}', CelerpSelection.ids()[0])); });
  document.body.appendChild(form);
  var done = function () { form.remove(); };
  htmx.ajax('POST', url, {source: form, target: '#bulk-action-result', swap: 'outerHTML'}).then(done, done);
}"""


@pytest.fixture(scope="module")
def bulk_items(api):
    tag = uuid.uuid4().hex[:6]
    for n in (1, 2):
        r = api.post("/items", json={"sku": f"OUT-{tag}-{n}", "sell_by": "piece", "name": f"Outcome {tag}", "quantity": 5})
        assert r.status_code in {200, 201}, r.text
    return f"OUT-{tag}"


def _tick_two(page, ui_server, q):
    page.goto(f"{ui_server}/inventory?q={q}&status=all", wait_until="load")
    htmx_idle(page)
    page.evaluate("CelerpSelection.clear()")
    boxes = page.locator("input.row-select")
    expect(boxes).to_have_count(2)
    boxes.nth(0).check()
    boxes.nth(1).check()
    expect(page.locator("#bulk-toolbar")).to_be_visible()


def test_every_bulk_action_of_the_item_list_is_walked(ui_server):
    from ui.app import app
    routes = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/items/bulk/")
              and "POST" in (getattr(r, "methods", None) or ())}
    assert routes == set(_BULK_ACTIONS) | {_STORE_ACTIONS}


@pytest.mark.parametrize("url", sorted(_BULK_ACTIONS))
def test_a_refused_bulk_action_says_so_on_screen(page, ui_server, bulk_items, url):
    from ui.api_client import APIError
    values, call, _ = _BULK_ACTIONS[url]
    _tick_two(page, ui_server, bulk_items)
    with patch(f"ui.api_client.{call}", new=AsyncMock(side_effect=APIError(409, "Refused for this test."))):
        with page.expect_response(lambda r: r.url.endswith(url)):
            page.evaluate(_POST_TICKED_JS, [url, values])
        htmx_idle(page)
    outcome = page.locator(_OUTCOME)
    expect(outcome.first).to_be_visible()
    assert "".join(outcome.all_inner_texts()).strip()
    assert page.locator(".toast--success").count() == 0


@pytest.mark.parametrize("url", sorted(set(_BULK_ACTIONS) - {"/api/items/bulk/write-off"}))
def test_a_bulk_action_that_worked_says_so_after_the_bar_hides(page, ui_server, bulk_items, url):
    values, call, answer = _BULK_ACTIONS[url]
    _tick_two(page, ui_server, bulk_items)
    with patch(f"ui.api_client.{call}", new=AsyncMock(return_value=answer)):
        with page.expect_response(lambda r: "/inventory/content" in r.url):  # the list reloads itself
            page.evaluate(_POST_TICKED_JS, [url, values])
        htmx_idle(page)
    expect(page.locator("#bulk-toolbar")).to_be_hidden()
    outcome = page.locator(_OUTCOME)
    expect(outcome).to_have_count(1)
    assert outcome.inner_text().strip()
    expect(page.locator("#toast-container .toast--success")).to_be_visible()


def test_a_store_action_that_worked_says_so_after_the_page_reloads(page, ui_server, bulk_items):
    url = _STORE_ACTIONS.format(platform="teststore", action="publish")
    _tick_two(page, ui_server, bulk_items)
    with patch("ui.routes.inventory_channels.channel_for", return_value={"id": "teststore", "label": "Test Store"}), \
            patch("ui.api_client.set_connector_item_sync", new=AsyncMock(return_value={"updated": 2})):
        with page.expect_navigation():  # the page reloads itself
            page.evaluate(_POST_TICKED_JS, [url, {}])
        page.wait_for_load_state("load")
    outcome = page.locator(_OUTCOME)
    expect(outcome).to_have_count(1)
    assert "Test Store" in outcome.inner_text()
    expect(page.locator("#toast-container .toast--success")).to_be_visible()


# Every table whose column setup can be changed and reset from its own menu.
_COLUMN_PAGES = ["/inventory", "/contacts/customers"]


def _toggle_first_optional_column(page):
    page.click("#col-mgr-btn")
    box = page.locator("#col-mgr-menu input[type=checkbox]:checked:not(:disabled)").last
    key = box.get_attribute("value")
    box.uncheck()
    return key


@pytest.mark.parametrize("path", _COLUMN_PAGES)
def test_a_column_change_or_reset_that_cannot_save_says_so_and_stays(page, ui_server, lists_with_rows, path):
    page.route("**/column-prefs", lambda r: r.fulfill(status=503, body="down") if r.request.method == "POST"
               else r.continue_())
    page.goto(f"{ui_server}{path}", wait_until="load")
    htmx_idle(page)
    error = page.locator("#global-ui-error")
    with page.expect_response("**/column-prefs"):
        _toggle_first_optional_column(page)
    expect(error).to_be_visible()
    assert error.inner_text().strip()
    page.evaluate("document.getElementById('global-ui-error').style.display = 'none'")
    loads = []
    page.on("load", lambda _: loads.append(1))
    with page.expect_response("**/column-prefs"):
        page.locator("#col-mgr-menu button").last.click()
    expect(error).to_be_visible()
    assert error.inner_text().strip()
    page.wait_for_timeout(500)  # a reload would have started by now
    assert not loads, "the page reloaded as if the reset had been saved"


@pytest.mark.parametrize("path", _COLUMN_PAGES)
def test_a_refused_column_save_shows_the_servers_refusal_as_other_saves_do(page, ui_server, lists_with_rows, path):
    """The column save reads a refusal the way every other save does: a short plain-text
    refusal is shown as written; an answer with no readable refusal shows the general notice."""
    refusal = "Column setup is kept for at most 60 tables."
    answers = iter([(422, "text/plain; charset=utf-8", refusal),
                    (500, "text/html", "<html><body>Internal Server Error</body></html>")])

    def refuse(route):
        if route.request.method != "POST":
            return route.continue_()
        status, ctype, body = next(answers)
        route.fulfill(status=status, content_type=ctype, body=body)
    page.route("**/column-prefs", refuse)
    page.goto(f"{ui_server}{path}", wait_until="load")
    htmx_idle(page)
    error = page.locator("#global-ui-error")
    with page.expect_response("**/column-prefs"):
        _toggle_first_optional_column(page)
    expect(error).to_contain_text(refusal)
    general = page.evaluate("window.__shellI18n.requestFailed")
    assert refusal not in general
    with page.expect_response("**/column-prefs"):
        page.locator("#col-mgr-menu input[type=checkbox]:not(:disabled)").last.click()
    expect(error).to_contain_text(general)


@pytest.mark.parametrize("path", _COLUMN_PAGES)
def test_a_column_change_and_reset_that_save_show_on_the_table(page, ui_server, lists_with_rows, path):
    page.goto(f"{ui_server}{path}", wait_until="load")
    htmx_idle(page)
    with page.expect_response("**/column-prefs"):
        key = _toggle_first_optional_column(page)
    header = page.locator(f'#data-table thead th[data-key="{key}"], #data-table thead th[data-col="{key}"]').first
    expect(header).to_be_hidden()
    with page.expect_navigation():
        page.locator("#col-mgr-menu button").last.click()
    htmx_idle(page)
    expect(header).to_be_visible()
    expect(page.locator("#global-ui-error")).to_be_hidden()


@pytest.mark.parametrize("path,table", [("/inventory", "inventory"), ("/contacts/customers", "customers")])
def test_the_column_setup_an_older_version_kept_in_the_browser_moves_to_the_user_once(
        page, ui_server, lists_with_rows, path, table):
    """The browser copy shows on the first page load, is saved as the user's setup and cleared,
    so the next load shows it from the server and a later change is not overwritten."""
    page.goto(f"{ui_server}{path}", wait_until="load")
    htmx_idle(page)
    page.click("#col-mgr-btn")
    key = page.locator("#col-mgr-menu input[type=checkbox]:checked:not(:disabled)").last.get_attribute("value")
    header = page.locator(f'#data-table thead th[data-key="{key}"], #data-table thead th[data-col="{key}"]').first
    expect(header).to_be_visible()
    old_keys = [f"celerp_cols_{table}", f"celerp_col_order_{table}", f"celerp_col_widths_{table}"]
    page.evaluate("([vis, order, key]) => { localStorage.setItem(vis, JSON.stringify({[key]: false}));"
                  " localStorage.setItem(order, 'not a setup'); }", [old_keys[0], old_keys[1], key])
    try:
        with page.expect_response(lambda r: r.url.endswith("/column-prefs") and r.request.method == "POST") as saved:
            page.reload(wait_until="load")
        assert saved.value.ok
        assert saved.value.request.post_data_json == {table: {"vis": {key: False}}}
        htmx_idle(page)
        expect(header).to_be_hidden()
        assert page.evaluate("keys => keys.map(k => localStorage.getItem(k))", old_keys) == [None, None, None]
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.url.endswith("/column-prefs") else None)
        page.reload(wait_until="load")
        htmx_idle(page)
        expect(header).to_be_hidden()
        assert not posts, "the browser copy was imported a second time"
    finally:
        page.evaluate("keys => keys.forEach(k => localStorage.removeItem(k))", old_keys)
        page.click("#col-mgr-btn")
        with page.expect_navigation():
            page.locator("#col-mgr-menu button").last.click()
