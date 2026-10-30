# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Default Sell Qty on the item page and in the scan bar.

The cell shows what a scan will add when nothing is set, takes Full from its list or a typed
number, refuses a bad value with its message, keeps the old value on Esc, and locks while Allow
Splitting is off without losing what was stored. A scan names the quantity it placed.
"""
from __future__ import annotations

import time
import uuid

import pytest
from playwright.sync_api import expect

from .conftest import css_class
from .inline_edit import ready, set_cell, settled

pytestmark = pytest.mark.browser

CELL = 'td[data-col="default_sell_qty"]'
SCOPE = "body"
EDITING = "td.cell--editing"


def _item(api, *, sell_by="carat", quantity=6, **extra) -> str:
    sku = f"DSQ-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "sell_by": sell_by,
                                 "quantity": quantity, "allow_splitting": True, **extra})
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _stored(api, item_id: str):
    return api.get(f"/items/{item_id}").json().get("default_sell_qty")


def _wait_stored(api, item_id: str, expected, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while _stored(api, item_id) != expected and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _stored(api, item_id) == expected


def _open(page, ui_server, item_id: str):
    page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    page.locator(CELL).first.wait_for(state="visible", timeout=8000)
    page.evaluate("window.__sameView = true")


def _open_editor(page):
    settled(page, SCOPE)
    page.dblclick(CELL)
    box = page.locator(f"{EDITING} .combobox-input")
    box.wait_for(state="visible", timeout=4000)
    ready(page, f"{EDITING} input[name=value]")
    return box


def _count_patches(page) -> list[str]:
    patches: list[str] = []
    page.on("request", lambda req: req.method == "PATCH" and patches.append(req.url))
    return patches


def _click_away(page):
    page.locator("h1").first.click()
    page.wait_for_timeout(1500)


def test_unset_cell_shows_the_resolved_default_in_grey(page, ui_server, api):
    parcel = _item(api, sell_by="carat")
    _open(page, ui_server, parcel)
    expect(page.locator(f"{CELL} .cell-suggestion")).to_contain_text(
        "Full (everything left)")

    screws = _item(api, sell_by="piece", quantity=10)
    _open(page, ui_server, screws)
    expect(page.locator(f"{CELL} .cell-suggestion")).to_have_text("1")
    assert _stored(api, parcel) is None and _stored(api, screws) is None


def test_full_chosen_in_a_translated_ui_stores_full_and_shows_the_label(page, ui_server, api):
    parcel = _item(api)
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "url": ui_server}])
    try:
        _open(page, ui_server, parcel)
        box = _open_editor(page)
        box.click()
        page.locator(f'{EDITING} .combobox-option[data-value="full"]').click()
        settled(page, SCOPE)
        _wait_stored(api, parcel, "full")
        expect(page.locator(CELL)).to_contain_text("Voll (alles Verbleibende)")
    finally:
        page.context.add_cookies([{"name": "celerp_lang", "value": "en", "url": ui_server}])
    assert page.evaluate("window.__sameView === true"), "the page reloaded"


@pytest.mark.parametrize("commit", ["Enter", "Tab"])
def test_a_typed_number_saves(page, ui_server, api, commit):
    parcel = _item(api)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    box.fill("2.5")
    box.press(commit)
    settled(page, SCOPE)
    _wait_stored(api, parcel, 2.5)
    expect(page.locator(CELL)).to_contain_text("2.5")


@pytest.mark.parametrize("typed", ["0.5", "abc"])
def test_a_refused_value_keeps_the_cell_to_correct(page, ui_server, api, typed):
    screws = _item(api, sell_by="piece", quantity=10)
    _open(page, ui_server, screws)
    box = _open_editor(page)
    box.fill(typed)
    box.press("Enter")
    expect(page.locator(f"{EDITING} .cell-error")).to_be_visible(timeout=6000)
    assert page.locator(".cell-error").inner_text().strip()
    assert _stored(api, screws) is None
    # The box still holds what was typed, with focus in it, so Esc or a correction works at once.
    box = page.locator(f"{EDITING} .combobox-input")
    expect(box).to_have_value(typed)
    expect(box).to_be_focused()

    box.fill("2")
    box.press("Enter")
    _wait_stored(api, screws, 2)
    expect(page.locator(CELL)).to_have_text("2")


def test_text_matching_several_options_says_to_pick_one_or_type_a_number(page, ui_server, api):
    """The note under a box that still matches several options says what this cell accepts.
    It never invites an Enter that the cell then refuses; a number typed after it saves."""
    parcel = _item(api)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    patches = _count_patches(page)
    box.fill("u")
    assert page.locator(f"{EDITING} .combobox-option:visible").count() > 1
    box.press("Tab")
    note = page.locator(f"{EDITING} .combobox-note")
    expect(note).to_have_text("Pick one of the matches, or type a number above 0.")
    assert patches == [] and _stored(api, parcel) is None
    box.fill("1.5")
    box.press("Enter")
    _wait_stored(api, parcel, 1.5)
    expect(page.locator(CELL)).to_contain_text("1.5")


def test_clearing_a_set_value_returns_it_to_unset(page, ui_server, api):
    screws = _item(api, sell_by="piece", quantity=10, default_sell_qty=2)
    _open(page, ui_server, screws)
    box = _open_editor(page)
    box.fill("")
    box.press("Enter")
    _wait_stored(api, screws, None)
    expect(page.locator(f"{CELL} .cell-suggestion")).to_have_text("1")


def test_the_unit_default_option_returns_a_value_to_unset(page, ui_server, api):
    parcel = _item(api, default_sell_qty=2)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    box.click()
    page.locator(f'{EDITING} .combobox-option[data-value=""]').click()
    _wait_stored(api, parcel, None)
    expect(page.locator(f"{CELL} .cell-suggestion")).to_contain_text("Full (everything left)")


def test_a_whole_number_shows_without_a_decimal(page, ui_server, api):
    screws = _item(api, sell_by="piece", quantity=10, default_sell_qty=3)
    _open(page, ui_server, screws)
    expect(page.locator(CELL)).to_have_text("3")


def _category_editor(page, ui_server, api, category: str, slugs=("diamond", "dolomite", "druzy")):
    for slug in slugs:
        r = api.patch(f"/companies/me/category-schema/{slug}", json={"fields": []})
        assert r.status_code in {200, 201}, r.text
    gem = _item(api, category=category)
    _open(page, ui_server, gem)
    settled(page, SCOPE)
    page.dblclick('td[data-col="category"]')
    box = page.locator(f"{EDITING} .combobox-input")
    box.wait_for(state="visible", timeout=4000)
    ready(page, f"{EDITING} input[name=value]")
    return gem, box


def _category(api, item_id: str):
    return api.get(f"/items/{item_id}").json().get("category")


def test_filtering_a_searchable_cell_then_clicking_away_keeps_its_value_and_says_why(page, ui_server, api):
    """A filter that still matches several options is a search: clicking away sends nothing,
    keeps the stored value, and leaves the editor open with the typed text and a note on how
    to finish. Enter then adds the typed text."""
    gem, box = _category_editor(page, ui_server, api, "diamond")
    patches = _count_patches(page)
    box.fill("d")
    assert page.locator(f"{EDITING} .combobox-option:visible").count() > 1
    _click_away(page)
    assert patches == []
    assert _category(api, gem) == "diamond"
    note = page.locator(f"{EDITING} .combobox-note")
    expect(note).to_have_text('Pick one of the matches, or press Enter to add "d".')
    assert box.input_value() == "d"
    box.focus()
    expect(note).to_have_count(1)
    box.press("Enter")
    page.wait_for_timeout(1500)
    assert _category(api, gem) == "d"
    assert len(patches) == 1


def test_a_new_category_one_label_contains_is_never_saved_as_that_category(page, ui_server, api):
    """Typed text that one existing label merely contains is still a search: clicking away
    saves nothing and says how to finish, Enter adds it as typed, so "Gems" never saves the
    "Rough Gemstone" category. Text no label contains saves as typed on click-away."""
    gem, box = _category_editor(page, ui_server, api, "diamond", slugs=("diamond", "rough_gemstone"))
    label = page.locator(f"{EDITING} .combobox-option[data-value='rough_gemstone']").first.inner_text().strip()
    assert "gems" in label.lower()
    patches = _count_patches(page)
    box.fill("Gems")
    assert page.locator(f"{EDITING} .combobox-option:visible").count() == 1
    _click_away(page)
    assert patches == []
    assert _category(api, gem) == "diamond"
    expect(page.locator(f"{EDITING} .combobox-note")).to_have_text(
        'Pick one of the matches, or press Enter to add "Gems".')
    box.focus()
    box.press("Enter")
    page.wait_for_timeout(1500)
    assert _category(api, gem) == "Gems"
    assert len(patches) == 1
    gem, box = _category_editor(page, ui_server, api, "diamond", slugs=())
    patches = _count_patches(page)
    box.fill("Zircon")
    assert page.locator(f"{EDITING} .combobox-option:not(.combobox-option--empty)"
                        ":not(.combobox-option--pinned):visible").count() == 0
    _click_away(page)
    assert _category(api, gem) == "Zircon"
    assert len(patches) == 1


@pytest.mark.parametrize("typed", ["DIAMOND", "diamond"])
def test_an_exact_label_or_value_in_another_case_picks_that_option(page, ui_server, api, typed):
    """An exact label or value match in any case picks the option itself, once, never a new
    value spelled differently."""
    gem, box = _category_editor(page, ui_server, api, "dolomite")
    patches = _count_patches(page)
    box.fill(typed)
    _click_away(page)
    assert _category(api, gem) == "diamond"
    assert len(patches) == 1


def test_tab_after_a_filter_matching_several_options_keeps_the_value(page, ui_server, api):
    """Tab commits typed text only when it names one option or none; a filter that still
    matches several options is a search, not a value."""
    gem, box = _category_editor(page, ui_server, api, "diamond")
    box.fill("d")
    assert page.locator(f"{EDITING} .combobox-option:visible").count() > 1
    box.press("Tab")
    page.wait_for_timeout(1500)
    assert _category(api, gem) == "diamond"
    expect(page.locator(f"{EDITING} .combobox-note")).to_have_count(1)


def test_a_refused_unit_change_keeps_the_qty_cell_and_says_what_to_change(page, ui_server, api):
    parcel = _item(api, default_sell_qty=2.5)
    _open(page, ui_server, parcel)
    settled(page, SCOPE)
    page.dblclick('td[data-col="quantity"] span.paired-secondary')
    box = page.locator(f"{EDITING} .combobox-input")
    box.wait_for(state="visible", timeout=4000)
    ready(page, f"{EDITING} input[name=value]")
    box.fill("piece")
    box.press("Enter")
    error = page.locator('td[data-col="quantity"] .cell-error')
    expect(error).to_contain_text("Change Default Sell Qty", timeout=6000)
    assert api.get(f"/items/{parcel}").json()["sell_by"] == "carat"


def test_sell_by_text_matching_several_units_says_to_pick_one_or_add_a_unit(page, ui_server, api):
    """The note under the Sell By half of the Qty cell says what the cell accepts: one of the
    company's units, or a new one added through Add new unit. It never offers an Enter the item
    write refuses; nothing is saved meanwhile, and a real unit picked after it still saves."""
    parcel = _item(api)
    _open(page, ui_server, parcel)
    settled(page, SCOPE)
    page.dblclick('td[data-col="quantity"] span.paired-secondary')
    box = page.locator(f"{EDITING} .combobox-input")
    box.wait_for(state="visible", timeout=4000)
    ready(page, f"{EDITING} input[name=value]")
    patches = _count_patches(page)
    box.fill("r")
    assert page.locator(f"{EDITING} .combobox-option:visible").count() > 1
    box.press("Tab")
    note = page.locator(f"{EDITING} .combobox-note")
    expect(note).to_have_text("Pick one of your units, or add a new one with Add new unit.")
    assert patches == [] and api.get(f"/items/{parcel}").json()["sell_by"] == "carat"
    box.fill("gram")
    box.press("Enter")
    deadline = time.monotonic() + 6
    while api.get(f"/items/{parcel}").json()["sell_by"] != "gram" and time.monotonic() < deadline:
        time.sleep(0.2)
    assert api.get(f"/items/{parcel}").json()["sell_by"] == "gram"


@pytest.mark.parametrize("field, stored, opener", [
    ("quantity", 12345.67, 'td[data-col="quantity"] .paired-primary'),
    ("reorder_point", 12345.678, 'td[data-col="reorder_point"]'),
])
def test_opening_a_number_cell_and_leaving_keeps_the_stored_value(page, ui_server, api, field, stored, opener):
    parcel = _item(api)
    old = api.get(f"/items/{parcel}").json().get(field)
    r = api.patch(f"/items/{parcel}", json={"fields_changed": {field: {"old": old, "new": stored}}})
    assert r.status_code == 200, r.text
    _open(page, ui_server, parcel)
    settled(page, SCOPE)
    page.dblclick(opener)
    editor = page.locator(f"{EDITING} input[name=value]")
    editor.wait_for(state="visible", timeout=4000)
    expect(editor).to_have_value(str(stored))
    patches: list[str] = []
    page.on("request", lambda req: req.method == "PATCH" and patches.append(req.url))
    page.locator("h1").first.click()
    page.wait_for_timeout(1500)
    assert patches == []  # an untouched editor sends nothing
    assert api.get(f"/items/{parcel}").json().get(field) == stored
    expect(page.locator(EDITING)).to_have_count(0)


def test_opening_a_multi_line_text_cell_and_leaving_keeps_the_line_break(page, ui_server, api):
    parcel = _item(api)
    old = api.get(f"/items/{parcel}").json().get("notes")
    stored = "Line one\nLine two"
    r = api.patch(f"/items/{parcel}", json={"fields_changed": {"notes": {"old": old, "new": stored}}})
    assert r.status_code == 200, r.text
    _open(page, ui_server, parcel)
    settled(page, SCOPE)
    page.dblclick('td[data-col="notes"]')
    editor = page.locator(f"{EDITING} [name=value]")
    editor.wait_for(state="visible", timeout=4000)
    ready(page, f"{EDITING} [name=value]")
    expect(editor).to_have_value(stored)  # the editor shows the line break
    patches: list[str] = []
    page.on("request", lambda req: req.method == "PATCH" and patches.append(req.url))
    page.locator("h1").first.click()
    page.wait_for_timeout(1500)
    assert patches == []  # an untouched editor sends nothing
    assert api.get(f"/items/{parcel}").json().get("notes") == stored
    expect(page.locator(EDITING)).to_have_count(0)


def test_esc_leaves_the_old_value(page, ui_server, api):
    parcel = _item(api, default_sell_qty=2)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    box.fill("9")
    box.press("Escape")
    settled(page, SCOPE)
    expect(page.locator(CELL)).to_contain_text("2")
    expect(page.locator(CELL)).not_to_contain_text("9")
    assert _stored(api, parcel) == 2


@pytest.mark.parametrize("then", ["click away", "Enter then click away", "Tab then click away"])
def test_a_typed_value_saves_once_however_the_box_is_left(page, ui_server, api, then):
    """A value typed into an allow-custom box saves on click away, and a box Enter or Tab
    already saved does not save it a second time."""
    parcel = _item(api)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    patches = _count_patches(page)
    box.fill("0.5")
    if then != "click away":
        box.press(then.split()[0])
        settled(page, SCOPE)
    _click_away(page)
    assert len(patches) == 1, patches
    _wait_stored(api, parcel, 0.5)
    expect(page.locator(CELL)).to_contain_text("0.5")
    expect(page.locator(EDITING)).to_have_count(0)


def test_an_option_picked_then_left_saves_once(page, ui_server, api):
    parcel = _item(api)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    patches = _count_patches(page)
    box.click()
    page.locator(f'{EDITING} .combobox-option[data-value="full"]').click()
    settled(page, SCOPE)
    _click_away(page)
    assert len(patches) == 1, patches
    _wait_stored(api, parcel, "full")


def test_a_typed_value_abandoned_with_esc_sends_nothing(page, ui_server, api):
    parcel = _item(api, default_sell_qty=2)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    patches = _count_patches(page)
    box.fill("0.5")
    box.press("Escape")
    _click_away(page)
    assert patches == []
    assert _stored(api, parcel) == 2


def test_a_search_typed_into_a_fixed_list_then_left_sends_nothing(page, ui_server, api):
    """A searchable select without free entry never saves its search text, however it is left."""
    shades = [f"Shade {n:02d}" for n in range(1, 13)]
    r = api.patch("/companies/me/category-schema/dsq_gem", json={"fields": [
        {"key": "shade", "label": "Shade", "type": "select", "options": shades, "required": False}]})
    assert r.status_code in {200, 201}, r.text
    gem = _item(api, category="dsq_gem", shade="Shade 03")
    _open(page, ui_server, gem)
    settled(page, SCOPE)
    page.locator('[hx-get$="/field/shade/edit"]').first.dblclick()
    wrap = page.locator(".combobox-wrap:has(input[name=value])").first
    wrap.wait_for(state="visible", timeout=4000)
    assert wrap.get_attribute("data-allow-custom") is None
    patches = _count_patches(page)
    wrap.locator(".combobox-input").fill("Shade 1")
    _click_away(page)
    assert patches == []
    assert api.get(f"/items/{gem}").json().get("shade") == "Shade 03"


def test_allow_splitting_locks_and_unlocks_the_cell_without_a_reload(page, ui_server, api):
    parcel = _item(api, default_sell_qty=3)
    _open(page, ui_server, parcel)
    cell = page.locator(CELL)
    expect(cell).not_to_have_class(css_class("cell--locked"))

    set_cell(page, "allow_splitting", "false", scope=SCOPE, kind="select")
    expect(page.locator(CELL)).to_have_attribute("title", "Not used while Allow Splitting is off: a scan always "
                                                           "adds the whole item. Turn Allow Splitting on to change this.")
    expect(page.locator(CELL)).to_have_class(css_class("cell--locked"))
    expect(page.locator(CELL)).to_contain_text("3")
    assert _stored(api, parcel) == 3

    set_cell(page, "allow_splitting", "true", scope=SCOPE, kind="select")
    page.wait_for_function(
        "(sel) => !document.querySelector(sel).classList.contains('cell--locked')", arg=CELL, timeout=6000)
    expect(page.locator(CELL)).to_contain_text("3")
    assert page.evaluate("window.__sameView === true"), "the page reloaded"
    _open_editor(page).press("Escape")


def test_a_customised_schema_shows_the_field_after_allow_splitting(page, ui_server, fresh_company):
    schema = fresh_company.get("/companies/me/item-schema").json()
    fields = [f for f in (schema.get("fields") if isinstance(schema, dict) else schema)
              if f["key"] != "default_sell_qty"]
    for f in fields:
        if f["key"] == "name":
            f["label"] = "Item Name"
    assert fresh_company.patch("/companies/me/item-schema", json={"fields": fields}).status_code == 200
    r = fresh_company.post("/items", json={"status": "available", "sku": "DSQ-SCHEMA", "name": "Parcel",
                                           "sell_by": "carat", "quantity": 6, "allow_splitting": True})
    assert r.status_code in {200, 201}, r.text

    page.goto(f"{ui_server}/inventory/{r.json()['id']}", wait_until="domcontentloaded")
    page.locator(CELL).first.wait_for(state="visible", timeout=8000)
    order = page.evaluate(
        "() => Array.from(document.querySelectorAll('td[data-col]')).map(td => td.dataset.col)")
    assert order.index("default_sell_qty") == order.index("allow_splitting") + 1, order


def test_the_scan_bar_names_the_placed_quantity(page, ui_server, api):
    sku = f"DSQ-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "sell_by": "carat",
                                 "quantity": 0.6, "allow_splitting": True})
    assert r.status_code in {200, 201}, r.text
    doc_id = api.post("/docs", json={"doc_type": "invoice", "status": "draft"}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    inp = page.locator("#scan-bar-input")
    inp.click()
    inp.fill(sku)
    inp.press("Enter")
    page.locator("#scan-bar-add").click()
    page.wait_for_function(
        "() => /\\u2713/.test(document.querySelector('.scan-bar-status')?.textContent || '')", timeout=8000)
    status = page.locator(".scan-bar-status").inner_text()
    assert sku in status and "0.60" in status and "carat" in status, status
    [line] = api.get(f"/docs/{doc_id}").json()["line_items"]
    assert line["quantity"] == pytest.approx(0.6)
