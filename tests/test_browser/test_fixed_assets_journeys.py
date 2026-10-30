# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fixed assets user journeys, end to end in the browser. Each journey meets its usual
error and reaches its outcome from the page that shows the error, using only the actions
that page offers."""
from __future__ import annotations

import asyncio
import io
import os
import re
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from decimal import Decimal as D

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Page, expect

from celerp.accounting_roles import SEEDED_TARGETS

from .conftest import css_class

pytestmark = pytest.mark.browser

API = "/fixed-assets/api"
_MODULE_TESTS = Path(__file__).resolve().parents[2] / "default_modules" / "celerp-fixed-assets" / "tests"


@pytest.fixture(autouse=True)
def _prompt(page: Page):
    """A step that never comes fails in seconds rather than at the suite's timeout."""
    page.set_default_timeout(20000)


def _ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def _books(c) -> None:
    """A chart with its posting roles, the state a company meets the module in."""
    r = c.post("/accounting/chart/seed", json={})
    assert r.status_code in (200, 201, 409), r.text
    for role, code in SEEDED_TARGETS.items():
        _ok(c.put(f"/accounting/posting-accounts/{getattr(role, 'value', role)}", json={"code": code}))


def _started(c, first: str = "2026-01-01") -> None:
    """The module started on the books and managing assets from `first`."""
    _books(c)
    _ok(c.post(f"{API}/start"))
    _ok(c.patch(f"{API}/settings", json={"first_managed_period": first}))


def _bill(c, name: str, price: str, issue_date: str) -> str:
    """A finalized bill for one asset line."""
    doc = _ok(c.post("/docs", json={
        "doc_type": "bill", "contact_name": "City Motors", "issue_date": issue_date, "total": float(price),
        "line_items": [{"name": name, "quantity": 1, "unit_price": float(price), "receive_as": "asset"}]}))["id"]
    _ok(c.post(f"/docs/{doc}/finalize"))
    return doc


def _buyer(c) -> str:
    """A customer contact to sell an asset to."""
    return _ok(c.post("/crm/contacts", json={"name": "Buyer Co", "contact_type": "customer"}))["id"]


def _line(c, description: str) -> dict:
    return next(i for i in _ok(c.get(f"{API}/intake"))["items"] if description in i["description"])


def _run(c, month: str) -> dict:
    return _operate(c, "/runs", {"month": month})


def _review_and_confirm(page: Page, review: str = "Review") -> None:
    """Review the form on the page, then confirm what the review shows and wait for the
    page it leads to."""
    page.get_by_role("button", name=review, exact=True).first.click()
    confirm = page.locator("#fa-preview").get_by_role("button", name="Confirm")
    with page.expect_navigation():
        confirm.click()


def _run_from_page(page: Page, month: str) -> None:
    """Run depreciation through `month` from the Runs page."""
    page.goto("/fixed-assets/runs")
    page.locator("input[name=month]").fill(month)
    _review_and_confirm(page)
    expect(page.locator("main")).to_contain_text(month)


def _reconciled(page: Page) -> None:
    page.goto("/fixed-assets/reconciliation")
    expect(page.locator("main")).to_contain_text("Reconciled")


def _operate(c, path: str, body: dict) -> dict:
    key = uuid.uuid4().hex[:10]
    p = _ok(c.post(f"{API}{path}", json={**body, "preview": True, "op_key": key}, timeout=60))
    return _ok(c.post(f"{API}{path}", json={**body, "preview": False, "op_key": key,
                                           "fingerprint": p["fingerprint"]}, timeout=60))


def _classes(c) -> dict[str, dict]:
    return {k["name"]: k for k in _ok(c.get(f"{API}/classes"))["items"]}


def test_j1_the_account_picker_offers_only_postable_accounts_and_saves_a_pick(page: Page, fresh_company):
    c = fresh_company
    _books(c)
    page.goto("/fixed-assets")
    page.locator("#fa-start").get_by_role("button").click()
    expect(page.locator("#fa-start")).to_have_count(0, timeout=20000)
    vehicles = _classes(c)["Vehicles"]["id"]
    page.goto("/settings/fixed-assets")
    cell = page.locator(f'td[hx-get="/settings/fixed-assets/classes/{vehicles}/asset_account/edit"]')

    # 1200 is a header with accounts under it: no journal line can post to it, so it is not offered.
    cell.click()
    box = page.locator("td.cell--editing .combobox-input").first
    box.click()
    box.fill("1200 ")
    expect(page.locator(".combobox-option:visible", has_text="1200 ")).to_have_count(0)

    box.fill("1210")
    page.locator(".combobox-option:visible", has_text="1210").first.click()
    expect(cell).to_contain_text("1210")
    expect(page.locator("td.cell--editing")).to_have_count(0)
    assert _classes(c)["Vehicles"]["asset_account"] == "1210"


def test_j2_voided_bill_is_replaced_from_the_asset(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    doc = _bill(c, "Pickup truck", "12000", "2026-01-10")
    asset = _operate(c, "/register", {"source": _line(c, "Pickup truck")["source"],
                                      "class_id": _classes(c)["Vehicles"]["id"], "mode": "separate"})["assets"][0]["id"]
    _run(c, "2026-02")
    _run(c, "2026-03")
    _ok(c.post(f"/docs/{doc}/void", json={"reason": "Wrong price"}))
    _bill(c, "Pickup truck", "12100", "2026-01-10")
    _run(c, "2026-04")

    page.goto(f"/fixed-assets/assets/{asset}")
    card = page.locator("#fa-exception")
    expect(card).to_contain_text("Review the source")
    card.get_by_role("link", name="Replace source").click()
    page.wait_for_url("**field=replace_source**")
    _choose(page, "old", "12,000")
    _choose(page, "new", "12,100")
    page.locator("input[name=posting_date]").fill("2026-04-20")
    page.get_by_role("button", name="Review", exact=True).first.click()
    preview = page.locator("#fa-preview")
    kept = preview.locator("h4:has-text('Kept as posted') + table")
    expect(kept).to_contain_text("2026-02-28")
    expect(kept).to_contain_text("2026-03-31")
    corrected = preview.locator("h4:has-text('Corrected') + table")
    expect(corrected).to_contain_text("12,100")
    expect(corrected).to_contain_text("12,000")
    with page.expect_navigation(url="**/fixed-assets/corrections/**"):
        preview.get_by_role("button", name="Confirm").click()
    assert D(_ok(c.get(f"{API}/assets/{asset}"))["cost"]) == D(12100)
    # The month held while the source was in doubt is caught up by the next run.
    _run_from_page(page, "2026-05")
    page.goto(f"/fixed-assets/assets/{asset}")
    expect(page.locator("#fa-exception")).to_have_count(0)
    _reconciled(page)


def _contributed(c, name: str, cost: str = "12000", on: str = "2026-01-15", cls: str = "Vehicles") -> str:
    return _operate(c, "/assets", {"kind": "contributed", "class_id": _classes(c)[cls]["id"], "name": name,
                                   "available_date": on, "cost": cost, "credit_account": "3100"})["assets"][0]["id"]


def _journal(c, ts: str, memo: str, entries: list[dict]) -> str:
    return _ok(c.post("/accounting/journal-entries", json={
        "ts": ts, "memo": memo, "idempotency_token": str(uuid.uuid4()), "entries": entries}))["je_id"]


def test_j3_manual_depreciation_is_linked_over_two_assets(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car, van = _contributed(c, "Owner's car"), _contributed(c, "Delivery van", cost="18000")
    _journal(c, "2026-01-31", "January depreciation booked by hand",
             [{"account": "6400", "debit": 500}, {"account": "1220", "credit": 500}])

    page.goto("/fixed-assets/intake")
    row = page.locator("tr", has_text="January depreciation booked by hand")
    row.get_by_role("link").last.click()
    page.wait_for_url("**/fixed-assets/intake/line**")
    page.locator(f"input[name='share:{car}']").fill("200")
    page.locator(f"input[name='share:{van}']").fill("200")
    page.get_by_role("button", name="Review", exact=True).click()
    expect(page.locator("#fa-preview .flash--error")).to_contain_text("The amounts add up to $400.00, not $500.00.")
    page.locator(f"input[name='share:{van}']").fill("300")
    _review_and_confirm(page)
    page.wait_for_url("**/fixed-assets/intake")
    expect(page.locator("main")).not_to_contain_text("January depreciation booked by hand")
    assert {a["id"]: D(a["accumulated_depreciation"]) for a in _ok(c.get(f"{API}/assets"))["items"]} == {
        car: D(200), van: D(300)}
    _reconciled(page)


def _choose(page: Page, name: str, text: str) -> None:
    """Choose `text` in the searchable picker named `name`."""
    box = page.locator(f".combobox-wrap:has(input[name='{name}']) .combobox-input")
    box.click()
    box.fill(text)
    page.locator(".combobox-option:visible", has_text=text).first.click()


def test_an_asset_files_tag_filter_is_searched_and_refreshes_the_list(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    van = _contributed(c, "Delivery van")
    page.goto(f"/fixed-assets/assets/{van}")
    with page.expect_response(lambda r: "/_section" in r.url and "tag_filter=warranties" in r.url):
        _choose(page, "tag_filter", "Warrant")
    expect(page.locator(".combobox-wrap:has(input[name='tag_filter']) .combobox-input")).to_have_value(
        re.compile("Warrant"))


def test_j4_locked_date_is_refused_and_the_first_open_date_saves(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _ok(c.post("/accounting/period-lock", json={"lock_date": "2026-03-31"}))

    page.goto("/fixed-assets")
    page.locator(".page-header").get_by_role("link", name="Add asset").click()
    page.get_by_role("link", name="Contributed or acquired without a bill").click()
    page.locator("input[name=name]").fill("Owner's car")
    _choose(page, "class_id", "Vehicles")
    page.locator("input[name=available_date]").fill("2026-03-15")
    page.locator("input[name=cost]").fill("12000")
    _choose(page, "credit_account", "3100")
    page.get_by_role("button", name="Review", exact=True).click()
    error = page.locator("#fa-preview .flash--error")
    expect(error).to_contain_text("Locked up to 31 Mar")
    expect(error).to_contain_text("1 Apr")
    expect(page.locator("input[name=available_date]")).to_have_value("2026-03-15")

    page.locator("input[name=available_date]").fill("2026-04-01")
    _review_and_confirm(page)
    page.wait_for_url("**/fixed-assets/assets/fa:asset:*")
    expect(page.locator("main")).to_contain_text("Owner's car")
    expect(page.get_by_role("button", name="Undo")).to_be_visible()


_REGISTER = (b"name,class,available_date,cost,accumulated_depreciation\n"
             b"Warehouse,Buildings,2020-01-01,250000,40000\n"
             b"Garage,Garages,2020-01-01,50000,0\n"
             b"Shed,Buildings,2020-01-01,-100,0\n"
             b"Forklift,Vehicles,2020-01-01,10000,12000\n")


def test_j5_bad_import_rows_are_fixed_in_the_grid_and_imported(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    page.goto("/fixed-assets/import")
    page.locator("input[type='file']").first.set_input_files(
        {"name": "register.csv", "mimeType": "text/csv", "buffer": _REGISTER})
    page.locator("#csv-preview-btn").click()
    page.get_by_role("button", name="Continue to Preview").click()
    review = page.locator("#import-preview")
    expect(review).to_contain_text("Garages")
    # The clean row imports now; the three bad rows stay in the import to be fixed.
    # Each tick is checked again before the next, so wait for the review to show it; the check re-reads
    # the whole file on the server, which runs well past the default wait while other suites share the host.
    for done, n in enumerate((2, 3, 4), 1):
        review.locator(f"input[name=exclude][value='{n}']").check()
        expect(review.locator("tr.import-row--excluded")).to_have_count(done, timeout=20000)
    with page.expect_navigation(url="**/fixed-assets/imports/**"):
        review.get_by_role("button", name="Import 1 Row").click()
    expect(page.locator("#fa-import-imported tbody tr")).to_have_count(1)

    page.locator("input[name='v.2.class']").fill("Buildings")
    page.locator("input[name='v.3.cost']").fill("100")
    page.locator("input[name='v.4.accumulated_depreciation']").fill("2000")
    _review_and_confirm(page, "Fix and import rows")
    expect(page.locator("#fa-import-imported tbody tr")).to_have_count(4)
    expect(page.locator("input[name='v.2.class']")).to_have_count(0)
    # The import's reconciliation: the register now holds every row against the ledger.
    expect(page.locator("#fa-opening-accounts")).to_contain_text("310,100")


def test_u25_the_import_review_grid_scrolls_in_its_own_box_with_whole_problems_and_values(page: Page, fresh_company):
    from celerp_fixed_assets.imports import COLUMNS

    c = fresh_company
    _started(c)
    row = {"number": "FA-0001", "name": "Warehouse", "class": "Buildings", "available_date": "2020-01-01",
           "cost": "250000", "accumulated_depreciation": "40000"}
    lines = [",".join(COLUMNS), *[",".join(row.get(k, "") for k in COLUMNS)] * 2]
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/fixed-assets/import")
    page.locator("input[type='file']").first.set_input_files(
        {"name": "register.csv", "mimeType": "text/csv", "buffer": "\n".join(lines).encode()})
    page.locator("#csv-preview-btn").click()
    page.get_by_role("button", name="Continue to Preview").click()
    grid = page.locator("#import-preview .csv-fix-table")
    expect(grid.locator("input.cell-edit").first).to_be_visible()
    # Every register column is in the grid, which scrolls inside its own box: the page never does.
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    assert page.evaluate("document.querySelector('#import-preview .csv-fix-table').parentElement"
                         ".getBoundingClientRect().right") <= 1440
    # A problem reads in full, and a cell to fix shows its whole value.
    for cell in page.locator("#import-preview .csv-fix-table td.import-problem").all():
        assert cell.evaluate("e => e.scrollWidth <= e.clientWidth"), cell.inner_text()
    assert page.locator("#import-preview .csv-fix-table td.import-problem").count() == 2
    for box in grid.locator("input.cell-edit").all():
        assert box.evaluate("e => e.scrollWidth <= e.clientWidth"), box.input_value()


def test_u31_form_fields_are_spaced_and_hints_read_quieter_than_labels(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    page.goto(f"/fixed-assets/assets/{car}/dispose")
    form = page.locator("main form").filter(has=page.locator("select[name=reason]"))
    labels = form.locator(".form-label")
    hint = form.locator(".form-hint").first
    expect(hint).to_be_visible()
    style = "e => { const s = getComputedStyle(e); return [s.color, parseFloat(s.fontSize)]; }"
    label_color, label_size = labels.first.evaluate(style)
    hint_color, hint_size = hint.evaluate(style)
    assert hint_color == label_color and hint_size <= label_size, (hint_color, hint_size, label_color, label_size)
    # Each label sits clear of the field above it.
    inputs = form.locator(".form-group .form-input")
    for n in range(1, labels.count()):
        above = inputs.nth(n - 1).bounding_box()
        below = labels.nth(n).bounding_box()
        assert below["y"] - (above["y"] + above["height"]) >= 12, n


def test_u33_an_asset_page_reads_cleanly_with_its_rare_actions_grouped(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    page.goto(f"/fixed-assets/assets/{car}")
    # Labels read as labels: left-aligned, in sentence case.
    label = page.locator(".detail-grid .detail-table th").first
    expect(label).to_be_visible()
    assert label.evaluate("e => [getComputedStyle(e).textAlign, getComputedStyle(e).textTransform]") == ["left", "none"]
    # The maintenance log is a part of its card, so it reads smaller than the card's heading.
    size = "e => parseFloat(getComputedStyle(e).fontSize)"
    upkeep = page.locator("#fa-upkeep")
    log = upkeep.get_by_text("Maintenance log", exact=True)
    assert log.evaluate(size) < upkeep.locator(".section-title").evaluate(size)
    # The month to skip shows what to type, and sits level with its button.
    month = page.locator("input[name=month]")
    assert month.get_attribute("type") == "text" and month.get_attribute("placeholder") == "YYYY-MM"
    skip = page.get_by_role("button", name="Skip month")
    box, button = month.bounding_box(), skip.bounding_box()
    assert abs((box["y"] + box["height"]) - (button["y"] + button["height"])) <= 1, (box, button)
    # The everyday actions stand alone, and the rest open from one More button.
    header = page.locator(".page-header")
    assert header.locator("a.btn:visible").count() <= 3
    for name in ("Add to asset", "Move", "Reclassify", "Transfer"):
        expect(header.get_by_role("link", name=name)).to_be_hidden()
    header.get_by_text("More", exact=True).click()
    for name in ("Add to asset", "Move", "Reclassify", "Transfer"):
        expect(header.get_by_role("link", name=name)).to_be_visible()
    page.keyboard.press("Escape")
    expect(header.get_by_role("link", name="Move")).to_be_hidden()
    header.get_by_text("More", exact=True).click()
    header.get_by_role("link", name="Move").click()
    expect(page).to_have_url(re.compile(r"/move$"))


def _level(field, button) -> None:
    """A field and its button stand the same height on the same line."""
    box, btn = field.bounding_box(), button.bounding_box()
    assert abs(box["y"] - btn["y"]) <= 1 and abs(box["height"] - btn["height"]) <= 1, (box, btn)


def test_u40_a_repeat_scan_keeps_one_row_named_with_controls_level(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    number = _ok(c.get(f"{API}/assets/{car}"))["number"]
    page.goto("/fixed-assets/checks")
    rows = page.locator("#fa-checks tbody tr")
    for said in (f"{number} checked.", f"{number} was already checked today."):
        page.locator("input[name=tag]").fill(number)
        page.get_by_role("button", name="Record").click()
        expect(page.get_by_text(said)).to_be_visible()
        expect(rows).to_have_count(1)
    row = rows.first
    expect(row).to_contain_text("Owner's car")
    expect(page.locator("#fa-checks thead")).to_contain_text("Name")
    _level(page.locator("input[name=tag]"), page.get_by_role("button", name="Record"))
    _level(page.locator("input[name=since]"), page.get_by_role("button", name="Show"))


def test_u45_the_register_header_keeps_everyday_actions_and_its_banner_reads_in_line(page: Page, fresh_company):
    c = fresh_company
    _books(c)
    _ok(c.post(f"{API}/start"))
    page.goto("/fixed-assets")
    header = page.locator(".page-header")
    # The everyday actions stand on one line; the rest wait under More.
    tops = {round(b["y"]) for b in (el.bounding_box() for el in header.locator(".page-actions > .btn, "
                                                                            ".page-actions summary").all())}
    assert len(tops) == 1, tops
    expect(header.get_by_role("link", name="Add asset")).to_be_visible()
    rare = header.get_by_role("link", name="Year end")
    expect(rare).to_be_hidden()
    header.get_by_text("More", exact=True).click()
    expect(rare).to_be_visible()
    for name in ("Corrections", "Checks", "Import register"):
        expect(header.get_by_role("link", name=name)).to_be_visible()
    page.keyboard.press("Escape")
    # The banner's button and its link share a line, and the filters stand clear of it.
    banner = page.locator("#fa-first-period")
    confirm = banner.get_by_role("button", name="Confirm").bounding_box()
    other = banner.get_by_role("link", name="Choose another month").bounding_box()
    assert abs((confirm["y"] + confirm["height"] / 2) - (other["y"] + other["height"] / 2)) <= 1, (confirm, other)
    assert other["x"] - (confirm["x"] + confirm["width"]) >= 8, (confirm, other)
    below = banner.bounding_box()
    search = page.locator("main input[name=q]").bounding_box()
    assert search["y"] - (below["y"] + below["height"]) >= 12, (below, search)


def test_u46_the_first_use_card_keeps_its_list_inside_and_its_closing_line_apart(page: Page, fresh_company):
    _books(fresh_company)
    page.goto("/fixed-assets")
    card = page.locator("#fa-start")
    inner = card.evaluate("e => e.getBoundingClientRect().left + parseFloat(getComputedStyle(e).paddingLeft)")
    # The bullets sit in the list's own indent, inside the card's padding.
    for item in card.locator("li").all():
        assert item.bounding_box()["x"] - inner >= 16, (inner, item.bounding_box())
    listed = card.locator("ul").bounding_box()
    closing = card.locator("p").last.bounding_box()
    assert closing["y"] - (listed["y"] + listed["height"]) >= 8, (listed, closing)


def _edges(page: Page, table: str) -> tuple[float, float]:
    """How light the left and right edges of a table's scroll box look on screen, along its
    first row (255 is white): the pixels a person sees, whatever draws them."""
    box = page.locator(table).evaluate("t => { const r = t.parentElement.getBoundingClientRect(); return [r.x, r.width]; }")
    row = page.locator(f"{table} tbody tr").first.bounding_box()
    shot = Image.open(io.BytesIO(page.screenshot(clip={"x": box[0], "y": row["y"] + 2, "width": box[1],
                                                         "height": max(row["height"] - 4, 1)}))).convert("L")
    width, height = shot.size

    def strip(x0: int) -> float:
        pixels = [shot.getpixel((x, y)) for x in range(x0, x0 + 3) for y in range(height)]
        return sum(pixels) / len(pixels)
    return strip(2), strip(width - 5)


def _painted(page: Page, table: str, shown) -> tuple[float, ...]:
    """The table's edges once *shown* accepts them, else as they are after five seconds. A
    loaded machine can paint the shade a frame or more after the class that draws it is set."""
    end = time.monotonic() + 5
    while True:
        edges = _edges(page, table)
        if shown(*edges) or time.monotonic() > end:
            return edges
        page.wait_for_timeout(100)


def test_u50_a_table_wider_than_a_phone_shades_where_more_columns_wait(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _contributed(c, "Delivery van")
    table = "main table.data-table"
    # Wide enough for every column, the register is unshaded.
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto("/fixed-assets")
    expect(page.locator(table).locator("xpath=..")).not_to_have_class(css_class("scroll-more-left"))
    expect(page.locator(table).locator("xpath=..")).not_to_have_class(css_class("scroll-more-right"))
    # Narrower, it scrolls in its own box and the page itself never scrolls sideways.
    page.set_viewport_size({"width": 1280, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.set_viewport_size({"width": 390, "height": 844})
    box = page.locator(table).evaluate_handle("t => t.parentElement")
    assert box.evaluate("b => b.scrollWidth > b.clientWidth")
    # More columns wait to the right: the right edge is visibly darker than the left.
    page.wait_for_function("t => document.querySelector(t).parentElement.classList.contains('scroll-more-right')",
                           arg=table, timeout=5000)
    left, right = _painted(page, table, lambda l, r: r < l - 30)
    assert right < left - 30, (left, right)
    # Scrolled to the end, the shade moves to the left edge, where the first columns now wait.
    box.evaluate("b => { b.scrollLeft = b.scrollWidth; }")
    page.wait_for_function("t => !document.querySelector(t).parentElement.classList.contains('scroll-more-right')",
                           arg=table, timeout=5000)
    left_end, right_end = _painted(page, table, lambda l, r: l < r - 30 and r > right + 30)
    assert left_end < right_end - 30 and right_end > right + 30, (left, right, left_end, right_end)


def test_the_register_filter_keeps_its_button_beside_a_field_on_a_phone(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/fixed-assets")
    form = page.locator("main form[action='/fixed-assets']")
    button = form.locator("button[type=submit]").bounding_box()
    classes = form.locator(".combobox-wrap").first.bounding_box()
    assert abs((button["y"] + button["height"] / 2) - (classes["y"] + classes["height"] / 2)) < 4, (button, classes)
    assert button["x"] > classes["x"] + classes["width"] - 1


def test_a_report_printed_from_its_page_prints_every_figure_and_none_of_its_controls(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _contributed(c, "Delivery van with a name long enough to need more than one line on paper")
    _run(c, "2026-02")
    # The content width of an A4 page at 96 dpi with the browser's default margins.
    page.set_viewport_size({"width": 718, "height": 1000})
    page.goto("/fixed-assets/reports/schedule?date_from=2026-01-01&date_to=2026-03-31")
    page.emulate_media(media="print")
    assert page.locator("main .breadcrumbs").first.is_hidden()
    assert page.locator("main form[method='get']").first.is_hidden()
    boxes = page.locator("main .table-scroll-wrap").all()
    assert boxes
    for box in boxes:
        assert box.evaluate("b => b.scrollWidth <= b.clientWidth + 1"), box.evaluate("b => b.outerHTML.slice(0, 300)")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


_SPLIT_WORDS = """t => {
  const split = [];
  for (const th of t.querySelectorAll('thead th')) {
    const walk = document.createTreeWalker(th, NodeFilter.SHOW_TEXT);
    for (let node = walk.nextNode(); node; node = walk.nextNode()) {
      for (const m of node.data.matchAll(/\\S+/g)) {
        const r = document.createRange();
        r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
        const tops = new Set([...r.getClientRects()].filter(x => x.width > 0).map(x => Math.round(x.top)));
        if (tops.size > 1) split.push(m[0]);
      }
    }
  }
  return split;
}"""


def test_printed_column_headers_wrap_between_words_never_inside_one(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    # Six-figure totals, as on the demo company, leave the headers the least room.
    for name, cost, cls in [("Delivery van", "132000", "Vehicles"), ("Forklift", "124000", "Plant and machinery"),
                            ("Office fit-out", "118500", "Assets under construction")]:
        _contributed(c, name, cost=cost, cls=cls)
    _run(c, "2026-02")
    page.emulate_media(media="print")
    page.set_viewport_size({"width": 680, "height": 1000})
    # The page printed as it stands (the browser's Print), which carries the app's print rules.
    page.goto("/fixed-assets/reports/schedule?date_from=2026-01-01&date_to=2026-03-31")
    tables = page.locator("main table.data-table").all()
    assert tables
    for table in tables:
        assert table.evaluate(_SPLIT_WORDS) == [], table.evaluate("t => t.outerHTML.slice(0, 300)")
    # Whole header words still leave every table inside the page width.
    assert page.evaluate("document.documentElement.scrollWidth") <= 680
    page.emulate_media(media="screen")


def test_the_register_fits_its_card_at_desktop_width_when_names_are_short(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    # The demo company's register: short names, its longest class and five-figure costs.
    for name, cost, cls in [("Delivery van", "32000", "Vehicles"), ("Forklift", "24000", "Plant and machinery"),
                            ("Laptop", "1450", "Computer equipment"),
                            ("Office fit-out", "18500", "Assets under construction")]:
        _contributed(c, name, cost=cost, cls=cls)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/fixed-assets")
    box = page.locator("#fa-register").evaluate_handle("t => t.closest('.table-scroll-wrap') || t.parentElement")
    # Every column, Status last, is inside the card without scrolling.
    assert box.evaluate("b => b.scrollWidth <= b.clientWidth"), box.evaluate("b => [b.scrollWidth, b.clientWidth]")
    status = page.locator("#fa-register thead th").last.bounding_box()
    edge = box.evaluate("b => b.getBoundingClientRect().right")
    assert status["x"] + status["width"] <= edge + 1, (status, edge)


def test_a_long_asset_name_wraps_in_the_register_at_a_readable_measure(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _contributed(c, " ".join(["Refrigerated delivery van with tail lift and rear camera"] * 4))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/fixed-assets")
    name = page.locator("#fa-register tbody tr").first.locator("td").nth(2)
    # The whole name reads, over several lines, in a column no wider than a readable measure,
    # so the figures after it stay on screen.
    assert name.evaluate("e => e.scrollWidth <= e.clientWidth && e.offsetHeight > 2 * parseFloat(getComputedStyle(e).lineHeight)")
    assert name.bounding_box()["width"] <= 320, name.bounding_box()
    cost = page.locator("#fa-register thead th", has_text="Cost").first.bounding_box()
    assert cost["x"] + cost["width"] <= 1440, cost


def _lists_sit_inside(page: Page, selector: str) -> None:
    """Every bullet of the lists under ``selector`` sits in the list's own indent, inside its container."""
    lists = page.locator(f"{selector} ul").all()
    assert lists, selector
    for ul in lists:
        inner = ul.evaluate("u => { const p = u.parentElement; "
                            "return p.getBoundingClientRect().left + parseFloat(getComputedStyle(p).paddingLeft); }")
        for item in ul.locator("li").all():
            assert item.bounding_box()["x"] - inner >= 16, (selector, inner, item.bounding_box())


def test_lists_on_the_year_end_and_asset_pages_sit_inside_their_card(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _bill(c, "Pickup truck", "12000", "2026-01-10")
    asset = _operate(c, "/register", {"source": _line(c, "Pickup truck")["source"],
                                      "class_id": _classes(c)["Vehicles"]["id"], "mode": "separate"})["assets"][0]["id"]
    page.goto("/fixed-assets/year-end?year=2026")
    _lists_sit_inside(page, "main")
    page.goto(f"/fixed-assets/assets/{asset}")
    _lists_sit_inside(page, "main")


def test_u35_enter_a_bill_stands_below_its_hint_and_clear_of_the_kind_buttons(page: Page, fresh_company):
    _started(fresh_company)
    page.goto("/fixed-assets/assets/new?kind=contributed")
    hint = page.get_by_text("If you paid a supplier", exact=False)
    button = page.get_by_role("button", name="Enter a bill")
    kinds = page.get_by_role("link", name="Contributed or acquired without a bill")
    h, b, k = hint.bounding_box(), button.bounding_box(), kinds.bounding_box()
    assert b["y"] >= h["y"] + h["height"] and abs(b["x"] - h["x"]) <= 1, (h, b)
    assert h["y"] - (k["y"] + k["height"]) >= 8, (k, h)
    # The kind buttons stand apart from each other.
    opening = page.get_by_role("link", name="Already in my accounts").bounding_box()
    assert k["x"] - (opening["x"] + opening["width"]) >= 4, (opening, k)


def _details(page: Page, table: str, text: str):
    """Open a row's details with its labelled button and return the row it opens."""
    button = page.locator(f"{table} tbody tr", has_text=text).first.get_by_role("button", name="Details")
    button.click()
    expect(button).to_have_attribute("aria-expanded", "true")
    detail = page.locator(f"#{button.get_attribute('aria-controls')}")
    expect(detail).to_be_visible()
    return detail


def _as_member(page: Page, c, api_server: str, role: str) -> None:
    """Sign the browser in as a new member with ``role``."""
    import psycopg2
    from urllib.parse import urlsplit

    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    _ok(c.post("/companies/me/users", json={"email": email, "name": role.title(), "role": role,
                                            "password": "Member12345!"}))
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username, password=parts.password,
                            dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    with conn.cursor() as cur:  # one live session per installation: let the member sign in
        cur.execute("DELETE FROM session_registry;")
    conn.close()
    token = _ok(httpx.post(f"{api_server}/auth/login", json={"email": email, "password": "Member12345!"},
                           timeout=10))["access_token"]
    page.context.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])


def test_j6_labels_off_asks_an_admin_and_the_admin_turns_it_on_and_prints(page: Page, fresh_company, api_server):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    number = _ok(c.get(f"{API}/assets/{car}"))["number"]
    # The company chooses its own modules: it starts from everything the server loads, so it
    # turns Labels off.
    _ok(c.post("/companies/me/modules/celerp-fixed-assets/enable"))
    _ok(c.post("/companies/me/modules/celerp-labels/disable"))
    assert "celerp-labels" not in _ok(c.get("/companies/me"))["settings"]["enabled_modules"]
    owner = page.context.cookies()

    _as_member(page, c, api_server, "operator")
    page.goto(f"/fixed-assets/assets/{car}")
    tag = page.locator("#fa-tag")
    expect(tag).to_contain_text("Ask an admin to turn on Labels")
    expect(tag).to_contain_text(number)
    expect(tag.get_by_role("button", name="Turn on")).to_have_count(0)

    page.context.clear_cookies()
    page.context.add_cookies(owner)
    page.goto(f"/fixed-assets/assets/{car}")
    expect(tag).to_contain_text("Turn on Labels to print asset tags")
    with page.expect_navigation(url=f"**/fixed-assets/assets/{car}"):
        tag.get_by_role("button", name="Turn on").click()
    tag.get_by_role("link", name="Print tag").click()
    page.wait_for_url("**/labels/print-source**")
    expect(page.locator("body")).to_contain_text(number)


def _reading(page: Page, month: str, units: str) -> None:
    readings = page.locator("#fa-readings")
    readings.locator("input[name=period]").fill(month)
    readings.locator("input[name=units]").fill(units)
    with page.expect_navigation():
        readings.get_by_role("button", name="Record reading").click()
    expect(page.locator("#fa-readings tbody")).to_contain_text(month)


def test_j7_missing_reading_holds_one_asset_and_the_next_run_catches_up(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _ok(c.post(f"{API}/classes", json={"name": "Metered machines", "kind": "tangible", "method": "units_of_use",
                                      "params": {"total_units": "10000"}}))
    _contributed(c, "Owner's car", cost="5040")
    press = _contributed(c, "Press", cost="10000", cls="Metered machines")
    for month, units in (("2026-01", "100"), ("2026-02", "120")):
        _ok(c.post(f"{API}/assets/{press}/readings", json={"period": month, "units": units}))
    _run(c, "2026-02")

    page.goto("/fixed-assets/runs")
    page.locator("input[name=month]").fill("2026-03")
    page.get_by_role("button", name="Review", exact=True).click()
    preview = page.locator("#fa-preview")
    expect(preview).to_contain_text("84.00")
    expect(preview).to_contain_text("Press")
    with page.expect_navigation():
        preview.get_by_role("button", name="Confirm").click()
    assert D(_ok(c.get(f"{API}/assets/{press}"))["accumulated_depreciation"]) == D(220)

    page.goto(f"/fixed-assets/assets/{press}")
    card = page.locator("#fa-exception")
    expect(card).to_contain_text("2026-03")
    card.get_by_role("link", name="Enter reading").click()
    expect(page.locator("#fa-readings")).to_be_in_viewport()
    _reading(page, "2026-03", "90")
    _reading(page, "2026-04", "50")
    _run_from_page(page, "2026-04")
    page.goto(f"/fixed-assets/assets/{press}")
    expect(page.locator("#fa-exception")).to_have_count(0)
    assert D(_ok(c.get(f"{API}/assets/{press}"))["accumulated_depreciation"]) == D(360)
    _reconciled(page)


def _finalize_doc(page: Page) -> None:
    """Finalize the open credit note from its own page."""
    page.get_by_role("button", name="Issue Credit Note").click()


def test_j8_over_credit_is_refused_then_the_asset_is_returned_from_correct_sale(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    number = next(a["number"] for a in _ok(c.get(f"{API}/assets"))["items"] if a["id"] == car)
    _run(c, "2026-03")
    invoice = _operate(c, f"/assets/{car}/sell", {"date": "2026-05-20", "price": "10000",
                                                 "contact_id": _buyer(c)})["invoice_id"]
    _ok(c.post(f"/docs/{invoice}/finalize"))
    line = next(li for li in _ok(c.get(f"/docs/{invoice}"))["line_items"] if li.get("asset_id"))

    def note(amount: float, on: str) -> str:
        return _ok(c.post("/docs", json={
            "doc_type": "credit_note", "original_doc_id": invoice, "issue_date": on, "total": amount,
            "line_items": [{"name": line.get("name"), "quantity": 1, "unit_price": amount, "line_total": amount,
                            "source_line_id": line["line_id"], "asset_id": car}]}))["id"]

    _ok(c.post(f"/docs/{note(2000, '2026-06-10')}/finalize"))
    over = note(9000, '2026-06-15')
    page.goto(f"/docs/{over}")
    _finalize_doc(page)
    toast = page.locator(".toast-container .toast--error").last
    expect(toast).to_contain_text("8,000")
    expect(toast).to_contain_text(number)

    page.get_by_role("link", name="Correct sale").first.click()
    page.wait_for_url(f"**/fixed-assets/assets/{car}**")
    sale = page.locator(f"[id='fa-sale-{car}']")
    expect(sale).to_contain_text("8,000.00")
    # The refused credit note stays open as a draft, so the sale offers it rather than a second one.
    expect(sale.get_by_role("link", name="Open the credit note")).to_have_attribute("href", f"/docs/{over}")
    expect(sale.locator("select[name=kind]")).to_have_count(0)
    assert c.delete(f"/docs/{over}").status_code == 200
    page.reload()
    sale.locator("select[name=kind]").select_option(label="Asset returned")
    sale.locator("input[name=date]").fill("2026-06-20")
    sale.get_by_role("button", name="Credit the sale", exact=True).click()
    with page.expect_navigation(url="**/docs/**"):
        page.locator("#fa-preview").get_by_role("button", name="Confirm").click()
    with page.expect_navigation():
        _finalize_doc(page)
    expect(page.get_by_role("button", name="Issue Credit Note")).to_have_count(0)
    page.goto(f"/fixed-assets/assets/{car}")
    expect(page.locator(f"[id='fa-sale-{car}']")).to_contain_text("Returned")
    assert next(a for a in _ok(c.get(f"{API}/assets"))["items"] if a["id"] == car)["status"] == "active"
    _reconciled(page)


def test_j9_stale_move_form_asks_for_a_new_review_and_the_move_succeeds(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    office, store = (_ok(c.post("/companies/me/locations", json={"name": n, "type": "warehouse"}))["id"]
                     for n in ("Head office", "North store"))
    car = _contributed(c, "Owner's car")

    page.goto(f"/fixed-assets/assets/{car}/move")
    page.locator("input[name=date]").fill("2026-02-01")
    _choose(page, "location_id", "North store")
    page.get_by_role("button", name="Review", exact=True).click()
    expect(page.locator("#fa-preview")).to_contain_text("North store")
    # Someone else moves the car while this review is open.
    _operate(c, f"/assets/{car}/move", {"date": "2026-01-20", "location_id": office})

    page.locator("#fa-preview").get_by_role("button", name="Confirm").click()
    stale = page.locator("#fa-preview .flash--error")
    expect(stale).to_contain_text("FA-0001 (Moved) changed since you reviewed it.")
    expect(stale.get_by_role("link", name="FA-0001")).to_have_attribute("href", f"/fixed-assets/assets/{car}")
    page.locator("#fa-preview").get_by_role("button", name="Review again").click()
    with page.expect_navigation(url=f"**/fixed-assets/assets/{car}"):
        page.locator("#fa-preview").get_by_role("button", name="Confirm").click()
    asset = next(a for a in _ok(c.get(f"{API}/assets"))["items"] if a["id"] == car)
    assert asset["location_id"] == store
    expect(page.locator("main")).to_contain_text("North store")


def test_a_slow_review_or_confirm_disables_its_button_and_says_it_is_working(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _ok(c.post("/companies/me/locations", json={"name": "North store", "type": "warehouse"}))
    car = _contributed(c, "Owner's car")
    page.goto(f"/fixed-assets/assets/{car}/move")
    page.locator("input[name=date]").fill("2026-02-01")
    _choose(page, "location_id", "North store")
    for step, button, working in (("review", page.get_by_role("button", name="Review", exact=True), "Reviewing..."),
                                  ("confirm", page.locator("#fa-preview").get_by_role("button", name="Confirm"),
                                   "Confirming...")):
        held = []
        page.route(f"**/fixed-assets/op/move/{step}", lambda route: held.append(route))
        cue = button.locator("xpath=..").get_by_text(working)
        expect(cue).to_have_css("opacity", "0")
        button.click()
        expect(button).to_be_disabled()
        expect(cue).to_have_css("opacity", "1")
        if step == "confirm":
            with page.expect_navigation(url=f"**/fixed-assets/assets/{car}"):
                held[0].continue_()
            break
        held[0].continue_()
        expect(page.locator("#fa-preview")).to_contain_text("North store")
        expect(button).to_be_enabled()
        expect(cue).to_have_css("opacity", "0")
    expect(page.locator("main")).to_contain_text("North store")


def test_a_run_through_a_month_not_ended_offers_the_latest_one_in_one_click(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _contributed(c, "Owner's car")
    page.goto("/fixed-assets/runs")
    month = page.locator("input[name=month]")
    latest = month.input_value()
    year, number = map(int, latest.split("-"))
    current = f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"
    month.fill(current)
    page.get_by_role("button", name="Review", exact=True).click()
    refusal = page.locator("#fa-preview .flash--error")
    expect(refusal).to_contain_text(f"{current} has not ended yet.")
    refusal.get_by_role("button", name=f"Review through {latest}").click()
    expect(month).to_have_value(latest)
    preview = page.locator("#fa-preview")
    expect(preview).to_contain_text("Owner's car")
    # A review of several months is confirmed from its summary or from its end.
    expect(preview.get_by_role("button", name="Confirm").last).to_be_visible()


def test_j10_revalue_on_a_cost_class_starts_the_class_workflow_and_revalues_it(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    first, second = (_contributed(c, f"Building {n}", cost="48000", on="2026-01-01", cls="Buildings") for n in "AB")

    page.goto(f"/fixed-assets/assets/{first}/remeasure")
    expect(page.locator("main")).to_contain_text("This asset's class uses the cost model, so its assets are not revalued.")
    expect(page.locator("input[name=fair_value]")).to_have_count(0)
    page.get_by_role("link", name="Change class to revaluation").click()
    page.wait_for_url("**/fixed-assets/classes/**/revalue")
    expect(page.locator("h1")).to_contain_text("Change Buildings to revaluation")
    page.locator("input[name=date]").fill("2026-03-31")
    page.locator("input[name=basis]").fill("Valuer report March 2026")
    page.locator(f"input[name='fair_value:{first}']").fill("50000")
    page.locator(f"input[name='fair_value:{second}']").fill("47000")
    page.get_by_role("button", name="Review", exact=True).click()
    expect(page.locator("#fa-preview")).to_contain_text("97,000.00")
    with page.expect_navigation(url="**/settings/fixed-assets**"):
        page.locator("#fa-preview").get_by_role("button", name="Confirm").click()
    assets = {a["id"]: D(a["nbv"]) for a in _ok(c.get(f"{API}/assets"))["items"]}
    assert (assets[first], assets[second]) == (D(50000), D(47000))
    assert _classes(c)["Buildings"]["measurement_policy"] == "revaluation"
    _reconciled(page)


def test_j11_split_short_of_the_balance_names_the_remainder_and_completes(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    site = _contributed(c, "Office fit-out", cost="20000", on="2026-02-01", cls="Assets under construction")

    page.goto(f"/fixed-assets/assets/{site}/place-in-service")
    page.locator("input[name=date]").fill("2026-03-01")
    for n, (name, cls, amount) in enumerate((("Office fittings", "Furniture and fittings", "12000"),
                                             ("Meeting room screens", "Computer equipment", "5000"))):
        page.locator(f"input[name='row:{n}:name']").fill(name)
        _choose(page, f"row:{n}:class_id", cls)
        page.locator(f"input[name='row:{n}:amount']").fill(amount)
    page.get_by_role("button", name="Review", exact=True).click()
    preview = page.locator("#fa-preview")
    expect(preview.locator("tr", has_text="Still under construction after")).to_contain_text("3,000.00")
    expect(preview).to_contain_text("Meeting room screens")
    with page.expect_navigation(url=f"**/fixed-assets/assets/{site}"):
        preview.get_by_role("button", name="Confirm").click()
    assets = {a["name"]: a for a in _ok(c.get(f"{API}/assets"))["items"]}
    assert D(assets["Office fit-out"]["cost"]) == D(3000) and assets["Office fit-out"]["status"] == "active"
    assert (D(assets["Office fittings"]["cost"]), D(assets["Meeting room screens"]["cost"])) == (D(12000), D(5000))
    _reconciled(page)


def test_j12_unexplained_journal_names_its_fix_and_is_registered(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    cls = next(k for k in _classes(c).values() if k["asset_account"] == "1210")
    _journal(c, "2026-02-10", "Generator paid in cash", [{"account": "1210", "debit": 900},
                                                         {"account": "3100", "credit": 900}])

    page.goto("/fixed-assets/reconciliation")
    expect(page.locator("main")).to_contain_text("Differences open")
    expect(page.locator("#fa-recon-differences tbody tr", has_text="2026-02-10").first).to_contain_text(
        "Not yet registered")
    detail = _details(page, "#fa-recon-differences", "2026-02-10")
    expect(detail).to_contain_text("Generator paid in cash")
    detail.get_by_role("link", name="Register it").click()
    page.wait_for_url("**/fixed-assets/intake")
    page.locator("tr", has_text="Generator paid in cash").get_by_role("link").last.click()
    page.wait_for_url("**/fixed-assets/intake/line**")
    page.locator("input[name=name]").fill("Standby generator")
    _choose(page, "class_id", cls["name"])
    _review_and_confirm(page)
    (generator,) = [a for a in _ok(c.get(f"{API}/assets"))["items"] if a["name"] == "Standby generator"]
    assert D(generator["cost"]) == D(900)
    _reconciled(page)


@pytest.fixture
def zz(monkeypatch):
    """The test-only country module ZZ and its asset tax regime, in the running app."""
    monkeypatch.syspath_prepend(str(_MODULE_TESTS))
    from fa_fixtures import admit_zz

    yield from admit_zz(monkeypatch)


def test_j13_noted_keeps_the_year_open_and_accepted_lets_it_close(page: Page, fresh_company, zz):
    c = fresh_company
    _started(c, "2025-07-01")
    _ok(c.patch(f"{API}/settings", json={"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    # A missing tax figure cannot be accepted, so the car's class carries its tax category.
    _ok(c.patch(f"{API}/classes/{_classes(c)['Vehicles']['id']}", json={"tax_categories": {"zz_asset": "zz_plant"}}))
    _contributed(c, "Owner's car", on="2025-07-15")
    _run(c, "2026-06")
    _journal(c, "2026-03-10", "Generator paid in cash", [{"account": "1210", "debit": 900},
                                                         {"account": "3100", "credit": 900}])
    for item in _ok(c.get(f"{API}/year-end", params={"year": 2026}))["items"]:
        if item["kind"] != "difference":
            _ok(c.post(f"{API}/year-end/2026/marks", json={"key": item["key"], "state": "accepted",
                                                          "reason": "Reviewed at year end", "effect": "None"}))

    page.goto("/fixed-assets/year-end?year=2026")
    status = page.locator("#fa-ye-status")
    expect(status).to_contain_text("Not ready to close.")
    row = _details(page, "#fa-ye-items", "Reconciliation difference")
    row.get_by_label("Status", exact=True).select_option("noted")
    row.get_by_label("Reason", exact=True).fill("Generator invoice requested from the supplier")
    with page.expect_navigation():
        row.get_by_role("button", name="Save").click()
    expect(status).to_contain_text("Not ready to close.")

    expect(page.locator("#fa-ye-items tbody tr", has_text="Reconciliation difference")).to_contain_text(
        "Generator invoice requested from the supplier")
    row = _details(page, "#fa-ye-items", "Reconciliation difference")
    expect(row.get_by_label("Status", exact=True)).to_have_value("noted")
    row.get_by_label("Status", exact=True).select_option("accepted")
    row.get_by_label("Accounting effect", exact=True).fill("Cost of 900.00 registered next year")
    with page.expect_navigation():
        row.get_by_role("button", name="Save").click()
    expect(status).to_contain_text("Ready to close.")
    assert _ok(c.get(f"{API}/reconciliation"))["status"] != "Reconciled", "accepting for close leaves the books"

    page.locator("#fa-ye-next").get_by_role("link").first.click()
    page.wait_for_url("**/fixed-assets/tax-books**")
    with page.expect_navigation():
        page.locator("#fa-tax-close").get_by_role("button", name="Close tax year").click()
    expect(page.locator("#fa-tax-reopen")).to_be_visible()
    book = _ok(c.get(f"{API}/tax-books", params={"regime": "zz_asset", "year": 2026}))
    assert book["status"] == "closed"


def test_j14_import_with_the_module_off_turns_it_on_and_imports(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _ok(c.post("/companies/me/modules/celerp-fixed-assets/disable"))

    page.goto("/settings/accounting?tab=chart")
    page.get_by_role("link", name="Import asset register").click()
    page.wait_for_url("**/import/asset-register")
    expect(page.locator("main")).to_contain_text("Turn on Fixed assets to import the asset register")
    with page.expect_navigation(url="**/fixed-assets/import"):
        page.get_by_role("button", name="Turn on").click()
    page.locator("input[type='file']").first.set_input_files(
        {"name": "register.csv", "mimeType": "text/csv", "buffer": b"\n".join(_REGISTER.split(b"\n")[:2]) + b"\n"})
    page.locator("#csv-preview-btn").click()
    page.get_by_role("button", name="Continue to Preview").click()
    with page.expect_navigation(url="**/fixed-assets/imports/**"):
        page.locator("#import-preview").get_by_role("button", name="Import 1 Row").click()
    expect(page.locator("#fa-import-imported tbody tr")).to_have_count(1)
    assert [a["name"] for a in _ok(c.get(f"{API}/assets"))["items"]] == ["Warehouse"]


def test_j15_sale_voided_while_off_waits_read_only_and_is_reissued_once_on(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    invoice = _operate(c, f"/assets/{car}/sell", {"date": "2026-05-20", "price": "10000",
                                                 "contact_id": _buyer(c)})["invoice_id"]
    _ok(c.post(f"/docs/{invoice}/finalize"))
    # The company has chosen its modules, fixed assets and what it needs, then turns fixed assets off.
    _ok(c.post("/companies/me/modules/celerp-fixed-assets/enable"))
    _ok(c.post("/companies/me/modules/celerp-fixed-assets/disable"))
    # The invoice is the document's own business: voiding it still reverses the disposal.
    _ok(c.post(f"/docs/{invoice}/void", json={"reason": "Buyer withdrew"}))

    page.goto(f"/fixed-assets/assets/{car}")
    expect(page.locator("#fa-read-only")).to_contain_text("Fixed assets is turned off. Records are read-only.")
    sale = page.locator(f"[id='fa-sale-{car}']")
    expect(sale).to_contain_text("Waiting on its invoice")
    expect(sale.get_by_role("button", name="New draft invoice")).to_have_count(0)
    assert _ok(c.get(f"{API}/assets/{car}"))["status"] == "active"

    with page.expect_navigation(url=f"**/fixed-assets/assets/{car}"):
        page.locator("#fa-read-only").get_by_role("button", name="Turn on").click()
    expect(page.locator("#fa-read-only")).to_have_count(0)
    _review_and_confirm(page, "New draft invoice")
    page.wait_for_url("**/docs/**")
    reissued = page.url.rstrip("/").rsplit("/", 1)[1]
    assert reissued != invoice
    assert _ok(c.get(f"{API}/assets/{car}"))["sale"]["invoice_id"] == reissued


def test_j16_archived_class_account_is_fixed_by_moving_its_balances_and_the_run_posts(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _ok(c.post("/accounting/accounts", json={"code": "1225", "name": "Vehicles accumulated depreciation",
                                             "account_type": "asset", "parent_code": "1200"}))
    vehicles = _classes(c)["Vehicles"]
    _ok(c.patch(f"{API}/classes/{vehicles['id']}", json={"accumulated_depreciation_account": "1225"}))
    car = _contributed(c, "Owner's car")
    _run(c, "2026-01")
    _ok(c.patch("/accounting/accounts/1225", json={"is_active": False}))
    _run(c, "2026-02")

    page.goto(f"/fixed-assets/assets/{car}")
    card = page.locator("#fa-exception")
    expect(card).to_contain_text("2026-02")
    card.get_by_role("link", name="Fix posting account").click()
    page.wait_for_url("**/fix-account**")
    expect(page.locator("main")).to_contain_text("1225")
    _choose(page, "account", "1220")
    expect(page.locator("input[name=mode][value=move]")).to_be_checked()
    page.get_by_role("button", name="Review", exact=True).click()
    preview = page.locator("#fa-preview")
    expect(preview).to_contain_text("1220")
    expect(preview).to_contain_text("200.00")
    with page.expect_navigation():
        preview.get_by_role("button", name="Confirm").click()
    assert _classes(c)["Vehicles"]["accumulated_depreciation_account"] == "1220"

    _run_from_page(page, "2026-03")
    page.goto(f"/fixed-assets/assets/{car}")
    expect(page.locator("#fa-exception")).to_have_count(0)
    assert D(_ok(c.get(f"{API}/assets/{car}"))["accumulated_depreciation"]) == D(600)
    _reconciled(page)


def test_j17_unknown_tag_is_named_and_the_correct_tag_scan_is_recorded(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    _ok(c.patch(f"{API}/assets/{car}", json={"changes": {"tag": "CAR-1"}, "previous": {"tag": None}}))
    number = _ok(c.get(f"{API}/assets/{car}"))["number"]

    page.goto("/fixed-assets/checks")
    tag = page.locator("input[name=tag]")
    expect(tag).to_be_focused()
    tag.fill("CAR-9")
    tag.press("Enter")
    expect(page.locator(".toast-container .toast--error").last).to_contain_text("No asset with tag CAR-9")
    expect(page.locator("#fa-checks")).to_contain_text("No checks recorded yet.")
    expect(tag).to_have_value("CAR-9")

    tag.fill("car-1")
    tag.press("Enter")
    expect(page.locator("#fa-checks")).to_contain_text(number)
    expect(tag).to_have_value("")
    checks = _ok(c.get(f"{API}/checks"))["items"]
    assert [(k["asset_id"], k["date"]) for k in checks] == [(car, _ok(c.get(f"{API}/setup"))["today"])]


def test_j18_cutover_difference_kept_open_carries_the_banner_until_acknowledged(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _operate(c, "/assets", {"kind": "opening", "class_id": _classes(c)["Buildings"]["id"], "name": "Warehouse",
                            "available_date": "2020-01-01", "cost": "250000", "accumulated_depreciation": "40000"})

    page.goto("/fixed-assets/opening")
    page.get_by_role("button", name="Finish").click()
    refusal = page.locator("#fa-opening-refusal")
    expect(refusal).to_contain_text("keep the differences open")
    expect(refusal.get_by_role("link", name="Record the opening journal")).to_have_attribute(
        "href", re.compile(r"/accounting/journal/new\?ts="))
    with page.expect_navigation():
        page.get_by_role("button", name="Keep differences open").click()
    expect(page.locator("main")).to_contain_text("finished with their differences kept open")

    page.goto("/fixed-assets")
    banner = page.locator("#fa-recon-banner")
    expect(banner).to_be_visible()
    banner.get_by_role("link").click()
    page.wait_for_url("**/fixed-assets/reconciliation")
    expect(page.locator("main")).to_contain_text("Differences open")
    for account in ("1210", "1220"):
        row = _details(page, "#fa-recon-differences", account)
        row.get_by_label("Status", exact=True).select_option("noted")
        row.get_by_label("Reason", exact=True).fill("Valuer's report due")
        with page.expect_navigation():
            row.get_by_role("button", name="Save").click()
        expect(page.locator("#fa-recon-differences tbody tr", has_text=account).first).to_contain_text(
            "Valuer's report due")
        row = _details(page, "#fa-recon-differences", account)
        expect(row.get_by_label("Reason", exact=True)).to_have_value("Valuer's report due")
        expect(page.locator("#fa-recon-accounts tr", has_text=account)).to_contain_text("Differences acknowledged")
    assert _ok(c.get(f"{API}/reconciliation"))["status"] == "Differences acknowledged"

    page.goto("/fixed-assets")
    expect(page.locator("#fa-recon-banner")).to_have_count(0)


def test_u26_u28_a_difference_opens_below_its_row_with_labelled_fields_on_a_phone(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _operate(c, "/assets", {"kind": "opening", "class_id": _classes(c)["Buildings"]["id"], "name": "Warehouse",
                            "available_date": "2020-01-01", "cost": "250000", "accumulated_depreciation": "40000"})
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/fixed-assets/reconciliation")
    expect(page.locator("#fa-recon-differences")).to_contain_text("Opening balance not in the register")
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    main = page.locator("main")
    expect(main).not_to_contain_text("Needs Manage accounting")
    expect(main).not_to_contain_text("Fixed in the current period")

    detail = _details(page, "#fa-recon-differences", "1210")
    expect(detail).to_contain_text("Opening balances")
    for label in ("Status", "Reason", "Accounting effect"):
        field = detail.get_by_label(label, exact=True)
        expect(field).to_be_visible()
        box = field.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390, label
    save = detail.get_by_role("button", name="Save")
    expect(save).to_be_visible()
    box = save.bounding_box()
    assert box and box["x"] + box["width"] <= 390
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0


def _daily_check(context, today: date) -> None:
    """The module's daily check for the session's company, run on its own engine and loop."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.services.auth import decode_access_token
    from celerp_fixed_assets import runs

    token = next(k["value"] for k in context.cookies() if k["name"] == "celerp_token")
    company_id = decode_access_token(token)["company_id"]

    async def check():
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await runs.tick(s, company_id, today)
                await s.commit()
        finally:
            await engine.dispose()

    errors: list[BaseException] = []

    def target():
        try:
            asyncio.run(check())
        except BaseException as exc:  # surfaced to the test below
            errors.append(exc)
    worker = threading.Thread(target=target)
    worker.start()
    worker.join(60)
    assert not worker.is_alive() and not errors, errors


def test_j19_reminder_due_is_told_once_postponed_with_a_reason_and_kept_in_history(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    number = _ok(c.get(f"{API}/assets/{car}"))["number"]
    today = date.fromisoformat(_ok(c.get(f"{API}/setup"))["today"])
    due, later = (today + timedelta(days=10)).isoformat(), (today + timedelta(days=40)).isoformat()
    _ok(c.post(f"{API}/assets/{car}/reminders", json={"kind": "maintenance", "work": "Oil change", "due": due}))
    _ok(c.post(f"{API}/assets/{car}/reminders", json={"kind": "warranty", "provider": "Dealer", "due": due}))
    _daily_check(page.context, today)
    _daily_check(page.context, today)
    told = [n for n in _ok(c.get("/notifications"))["items"] if n["title"] == "Asset reminders"]
    assert len(told) == 1, told

    page.goto("/fixed-assets")
    page.click(".notif-bell-btn")
    item = page.locator("#notif-panel .notif-item", has_text="Asset reminders").first
    body = item.locator(".notif-item__body")
    expect(body).to_contain_text("Oil change")
    expect(body).to_contain_text("Dealer")
    item.locator("a").first.click()
    page.wait_for_url("**/fixed-assets/reminders")
    page.locator("main tr", has_text="Oil change").get_by_role("link", name=number).click()
    page.wait_for_url(f"**/fixed-assets/assets/{car}")

    row = page.locator("#fa-reminders tbody tr", has_text="Oil change")
    row.locator("summary", has_text="Postpone").click()
    row.locator("input[name=to]").fill(later)
    row.locator("input[name=reason]").fill("Garage booked out")
    with page.expect_navigation():
        row.get_by_role("button", name="Postpone").click()
    row = page.locator("#fa-reminders tbody tr", has_text="Oil change")
    expect(row).to_contain_text(later)
    row.locator("summary", has_text="History").click()
    history = row.locator(".fa-history li", has_text="Postponed")
    expect(history).to_contain_text("Garage booked out")
    expect(history).to_contain_text(later)

    page.goto("/fixed-assets/reminders")
    expect(page.locator("main")).not_to_contain_text("Oil change")
    expect(page.locator("main")).to_contain_text("Dealer")


def test_j20_prior_year_cost_error_requested_by_an_operator_is_applied_by_a_manager(page: Page, fresh_company,
                                                                                    api_server):
    c = fresh_company
    _started(c, first="2025-01-01")
    car = _contributed(c, "Owner's car", on="2025-06-15")
    number = _ok(c.get(f"{API}/assets/{car}"))["number"]
    _run(c, "2025-12")
    owner = page.context.cookies()

    _as_member(page, c, api_server, "operator")
    page.goto(f"/fixed-assets/assets/{car}")
    page.get_by_role("link", name="Correct", exact=True).click()
    # Only the chosen method's own settings show.
    declining = page.locator("[data-method=declining_balance]")
    expect(declining).to_be_hidden()
    page.locator("select[name=method]").select_option(label="Declining balance")
    expect(declining).to_be_visible()
    expect(page.locator("[data-method=units_of_use]")).to_be_hidden()
    page.get_by_role("link", name="Cost", exact=True).click()
    page.wait_for_url("**field=cost**")
    page.locator("input[name=cost]").fill("15000")
    page.locator("input[name=reason]").fill("The invoice was 15,000")
    with page.expect_navigation(url="**/fixed-assets/corrections/**"):
        page.get_by_role("button", name="Request", exact=True).click()
    main = page.locator("main")
    expect(main).to_contain_text("Requested")
    expect(page.locator("#fa-correction-need")).to_contain_text("Needs Manage accounting")
    assert D(_ok(c.get(f"{API}/assets/{car}"))["cost"]) == D(12000)

    page.context.clear_cookies()
    page.context.add_cookies(owner)
    page.goto("/fixed-assets")
    page.click(".notif-bell-btn")
    page.locator("#notif-panel .notif-item", has_text="Corrections waiting").first.locator("a").first.click()
    page.wait_for_url("**/fixed-assets/corrections")
    page.locator("#fa-corrections tbody tr", has_text=number).get_by_role("link").last.click()
    page.wait_for_url("**/fixed-assets/corrections/**")
    preview = page.locator("#fa-preview")
    page.get_by_role("button", name="Review", exact=True).click()
    expect(preview).to_contain_text("2025")
    with page.expect_navigation():
        preview.get_by_role("button", name="Confirm").click()
    expect(main).to_contain_text("Applied")
    assert D(_ok(c.get(f"{API}/assets/{car}"))["cost"]) == D(15000)

    page.goto("/fixed-assets/corrections")
    page.get_by_role("link", name="Prior-period adjustments").click()
    page.wait_for_url("**/prior-period-adjustments**")
    page.locator("input[name=year]").fill("2025")
    with page.expect_navigation(url="**year=2025**"):
        page.get_by_role("button", name="Show", exact=True).click()
    expect(main).to_contain_text(number)
    expect(main).to_contain_text("2025")


def _alignments(page: Page, table: str) -> list[tuple[str, str, str]]:
    """Each column of `table`: (class, header alignment, first cell alignment)."""
    return page.eval_on_selector(table, """t => [...t.querySelectorAll('thead th')].map((th, i) => {
        const td = t.querySelector(`tbody tr td:nth-child(${i + 1})`);
        return [th.className, getComputedStyle(th).textAlign, td ? getComputedStyle(td).textAlign : ''];
    })""")


def _aligned(page: Page, table: str) -> None:
    """Number and money headers sit right over their right-aligned figures; other headers
    are centered over left-aligned text."""
    columns = _alignments(page, table)
    assert any("cell--number" in cls for cls, _, _ in columns), columns
    for cls, head, cell in columns:
        if "cell--number" in cls:
            assert (head, cell) == ("right", "right"), (table, cls, head, cell)
        else:
            assert head == "center" and cell in ("left", "start", ""), (table, cls, head, cell)


def test_visual_esc_empty_marks_newest_first_and_alignment_on_screen_and_in_print(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    _run(c, "2026-01")
    _run(c, "2026-02")

    page.goto(f"/fixed-assets/assets/{car}")
    serial = page.locator("td[data-col=serial]")
    expect(serial).to_have_text("--")
    serial.dblclick()
    editor = page.locator("td.cell--editing [name=value]")
    expect(editor).to_be_visible()
    editor.press("Escape")
    expect(editor).to_have_count(0)
    expect(page.locator("td[data-col=serial]")).to_have_text("--")

    page.goto("/fixed-assets/runs")
    months = page.locator("main table.data-table tbody tr td:first-child").all_inner_texts()
    assert months[:2] == ["2026-02", "2026-01"], months

    for media in ("screen", "print"):
        page.emulate_media(media=media)
        page.goto("/fixed-assets/runs")
        _aligned(page, "main table.data-table")
        page.goto("/fixed-assets")
        _aligned(page, "main table.data-table")
        page.goto("/fixed-assets/reports/depreciation/print?date_from=2026-01-01&date_to=2026-02-28")
        expect(page.locator("body")).to_contain_text("Owner's car")
        _aligned(page, "table")
    page.emulate_media(media="screen")


def _figure_font(page: Page) -> str:
    """The typeface of the report's first figure."""
    return page.eval_on_selector("main table td.cell--number, body > div table td.cell--number",
                                 "td => getComputedStyle(td).fontFamily")


def test_u42_figures_keep_one_typeface_on_screen_and_in_print(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _contributed(c, "Owner's car")
    _run(c, "2026-01")
    query = "?date_from=2026-01-01&date_to=2026-01-31"
    page.goto(f"/fixed-assets/reports/schedule{query}")
    screen = _figure_font(page)
    assert "mono" in screen.lower(), screen
    page.goto(f"/fixed-assets/reports/schedule/print{query}")
    expect(page.locator("body")).to_contain_text("Owner's car")
    assert _figure_font(page) == screen


def test_column_headers_share_one_typeface_over_text_and_figures(page: Page, fresh_company):
    _started(fresh_company)
    _contributed(fresh_company, "Owner's car")
    page.goto("/fixed-assets")
    fonts = page.eval_on_selector_all("main table.data-table thead th",
                                      "ths => ths.map(th => getComputedStyle(th).fontFamily)")
    assert len(fonts) > 5 and len(set(fonts)) == 1, fonts


def _pdf_pages(pdf: bytes) -> list[tuple[float, float, list[tuple[float, float, str]]]]:
    """Each page of a PDF: its width, height and each piece of text with where it starts.
    pypdf reports some runs with no position of their own (an identity text matrix); only
    the runs it places are kept, which include each page's first row and the footer."""
    import io as _io

    from pypdf import PdfReader

    pages = []
    for pg in PdfReader(_io.BytesIO(pdf)).pages:
        found: list[tuple[float, float, str]] = []

        def visit(text, cm, tm, _font, _size, found=found):
            if text.strip() and list(tm) != [1, 0, 0, 1, 0, 0]:
                x, y = tm[4], tm[5]
                found.append((cm[0] * x + cm[2] * y + cm[4], cm[1] * x + cm[3] * y + cm[5], text.strip()))
        pg.extract_text(visitor_text=visit)
        pages.append((float(pg.mediabox.width), float(pg.mediabox.height), found))
    return pages


def test_u43_a_long_report_prints_on_a4_inside_its_margins_on_every_page(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    for n in range(24):
        _contributed(c, f"Van {n:02d}")
    _run(c, "2026-01")
    url = "/fixed-assets/reports/schedule/print?date_from=2026-01-01&date_to=2026-01-31"
    # The figures fit the printable width of A4 (210mm less its margins), with no table cut off.
    page.emulate_media(media="print")
    page.set_viewport_size({"width": 680, "height": 1000})
    page.goto(url)
    assert page.evaluate("document.documentElement.scrollWidth") <= 680
    pages = _pdf_pages(page.pdf(format="A4", prefer_css_page_size=True))
    assert len(pages) > 1, len(pages)
    margin = 10 * 72 / 25.4  # at least 10mm clear on every side, every page
    for number, (width, height, texts) in enumerate(pages, 1):
        assert texts, number
        for x, y, text in texts:
            assert margin <= x <= width - margin and margin <= y <= height - margin, (number, x, y, text)
        # The footer stands below the figures rather than printing over them.
        footer = [y for _, y, text in texts if "Powered by" in text]
        body = [y for _, y, text in texts if "Powered by" not in text and "celerp.com" not in text]
        assert all(f < min(body) for f in footer), (number, footer, min(body))
    page.emulate_media(media="screen")


def test_j21_bill_line_account_is_kept_and_the_finalized_bill_leads_to_registering_it(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    doc = _ok(c.post("/docs", json={
        "doc_type": "bill", "contact_name": "City Motors", "issue_date": "2026-03-10", "total": 12000.0,
        "line_items": [{"name": "Coffee roaster", "quantity": 1, "unit_price": 12000.0, "receive_as": "asset"}]}))["id"]

    page.goto(f"/docs/{doc}")
    picker = page.locator("td.col-account .combobox-input").first
    picker.click()
    picker.fill("1210")
    with page.expect_response(lambda r: doc in r.url and r.request.method in ("PATCH", "POST", "PUT")):
        page.locator("td.col-account .combobox-option[data-value='1210']").first.click()
    line = _ok(c.get(f"/docs/{doc}"))["line_items"][0]
    assert line.get("account_code") == "1210", "the account picked on the line is kept"

    _ok(c.post(f"/docs/{doc}/finalize"))
    page.goto(f"/docs/{doc}")
    page.get_by_role("link", name="Register as asset").click()
    page.wait_for_url("**/fixed-assets/intake/line**")
    page.locator("input[name=name]").fill("Coffee roaster")
    _choose(page, "class_id", next(k for k in _classes(c).values() if k["asset_account"] == "1210")["name"])
    _review_and_confirm(page)
    (roaster,) = [a for a in _ok(c.get(f"{API}/assets"))["items"] if a["name"] == "Coffee roaster"]

    page.goto(f"/docs/{doc}")
    page.get_by_role("link", name="Register as asset").click()
    expect(page.locator("main")).to_contain_text(f"registered as {roaster['number']}")
    expect(page.get_by_role("link", name=roaster["number"])).to_have_attribute(
        "href", f"/fixed-assets/assets/{roaster['id']}")


# Every column checked against what it holds: a header over figures sits right, over them; any
# other header is centred; a figure is right-aligned and any other value left. The header grid
# follows rowspan and colspan, so a column under a grouped header is matched to its own label.
_MISALIGNED = """table => {
  const grid = [], heads = [...table.tHead.rows];
  heads.forEach((row, r) => { let col = 0; grid[r] = grid[r] || [];
    for (const th of row.cells) {
      while (grid[r][col]) col++;
      for (let dr = 0; dr < th.rowSpan; dr++) for (let dc = 0; dc < th.colSpan; dc++) {
        (grid[r + dr] = grid[r + dr] || [])[col + dc] = th; }
      col += th.colSpan; } });
  const own = grid[grid.length - 1] || [], out = [];
  const figure = /^[-+(]?[^\\d\\s]{0,4}\\s?[\\d,]+(\\.\\d+)?\\)?%?$/;
  const is = (el, side) => { const a = getComputedStyle(el).textAlign;
    return a === side || (side === "left" && a === "start") || (side === "right" && a === "end"); };
  own.forEach((th, i) => {
    const cells = [...table.tBodies[0].rows].map(r => r.cells[i]).filter(c => c && c.colSpan === 1
      && !c.querySelector("input, select, button, details") && c.innerText.trim() && c.innerText.trim() !== "--");
    if (!cells.length) return;
    const num = cells.every(c => figure.test(c.innerText.trim()));
    if (th.colSpan === 1 && th.innerText.trim() && !is(th, num ? "right" : "center"))
      out.push(`header ${th.innerText.trim()}: ${getComputedStyle(th).textAlign}`);
    cells.forEach(c => { if (!is(c, num ? "right" : "left"))
      out.push(`${th.innerText.trim()} ${c.innerText.trim()}: ${getComputedStyle(c).textAlign}`); }); });
  return out; }"""


def test_u32_settings_classes_and_the_import_review_align_headers_and_values_by_what_they_hold(
        page: Page, fresh_company):
    c = fresh_company
    _started(c)
    page.goto("/settings/fixed-assets")
    assert page.locator("#fa-classes table").evaluate(_MISALIGNED) == []
    page.goto("/fixed-assets/import")
    page.locator("input[type='file']").first.set_input_files(
        {"name": "register.csv", "mimeType": "text/csv", "buffer": _REGISTER})
    page.locator("#csv-preview-btn").click()
    page.get_by_role("button", name="Continue to Preview").click()
    rows = page.locator("#import-preview table:has-text('Warehouse')")
    expect(rows).to_be_visible()
    assert rows.evaluate(_MISALIGNED) == []


def test_u39_a_done_toast_steps_aside_for_the_next_action(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    _operate(c, f"/assets/{car}/sell", {"date": "2026-05-20", "price": "10000", "contact_id": _buyer(c)})
    page.goto(f"/fixed-assets/assets/{car}")
    page.get_by_role("button", name="Cancel the sale").click()
    with page.expect_navigation():
        page.locator("#fa-preview").get_by_role("button", name="Confirm").click()
    done = page.locator(".toast-container .toast", has_text="Sale cancelled.")
    expect(done).to_have_count(1)
    # The next action on the same page answers on its own, without the last one's toast beside it.
    with page.expect_response(lambda r: r.request.method == "POST" and "/fixed-assets/" in r.url):
        page.get_by_role("button", name="Undo registration").click()
    expect(done).to_have_count(0)


def test_u33_opening_balances_fit_a_phone_with_finish_in_reach(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _operate(c, "/assets", {"kind": "opening", "class_id": _classes(c)["Buildings"]["id"], "name": "Warehouse",
                            "available_date": "2020-01-01", "cost": "250000", "accumulated_depreciation": "40000"})
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/fixed-assets/opening")
    # The page never scrolls sideways; a wide table scrolls inside its own box.
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    finish = page.get_by_role("button", name="Finish").bounding_box()
    assert finish["x"] >= 0 and finish["x"] + finish["width"] <= 390, finish


def test_u30_the_register_follows_its_search_as_it_is_typed(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _contributed(c, "Delivery van")
    _contributed(c, "Forklift")
    page.goto("/fixed-assets")
    rows = page.locator("main table.data-table tbody tr")
    expect(rows).to_have_count(2)
    page.locator("main input[name=q]").press_sequentially("Fork")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Forklift")
    expect(page).to_have_url(re.compile(r"[?&]q=Fork"))
    page.locator("main input[name=q]").fill("")
    expect(rows).to_have_count(2)


def test_u27_a_long_first_run_opens_with_its_summary_and_confirm(page: Page, fresh_company):
    c = fresh_company
    _started(c, first="2025-01-01")
    _contributed(c, "Owner's car", on="2025-01-15")
    page.goto("/fixed-assets/runs")
    expect(page.locator(".empty-state-cta")).to_be_visible()
    page.locator("input[name=month]").fill("2026-06")
    page.get_by_role("button", name="Review", exact=True).click()
    preview = page.locator("#fa-preview")
    expect(preview).to_contain_text("2025-01 to 2026-06")
    # Months, assets and the total come first, with Confirm in reach before the months below.
    confirm = preview.get_by_role("button", name="Confirm").first
    first_month = preview.locator(".form-subtitle").first
    assert confirm.bounding_box()["y"] < first_month.bounding_box()["y"]
    expect(page.locator(".empty-state-cta")).to_be_hidden()
    confirm.click()
    page.wait_for_url("**/fixed-assets/runs")
    expect(page.locator("main")).to_contain_text("2026-06")


def test_u35_headings_clear_what_is_above_them_values_sit_by_their_labels_and_filter_lines_up(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    car = _contributed(c, "Owner's car")
    _reconciled(page)
    main = page.locator("main")
    box, button = main.locator("input[name=at]").bounding_box(), main.get_by_role("button", name="Filter").bounding_box()
    assert abs((box["y"] + box["height"] / 2) - (button["y"] + button["height"] / 2)) <= 2, (box, button)

    def gap(above, below) -> float:
        a, b = above.bounding_box(), below.bounding_box()
        return b["y"] - (a["y"] + a["height"])

    headings = main.locator(".section-title")
    assert gap(main.locator("#fa-recon-status"), headings.nth(0)) >= 12
    assert gap(main.locator("#fa-recon-accounts"), headings.nth(1)) >= 12

    page.goto(f"/fixed-assets/assets/{car}")
    for table in page.locator(".detail-table").all():
        label, whole = table.locator("th[scope=row]").first.bounding_box(), table.bounding_box()
        assert label["width"] < whole["width"] / 2, (label, whole)
    money = page.locator(".detail-table td.cell--number").first
    assert money.evaluate("e => getComputedStyle(e).textAlign") in ("left", "start")
