# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fixed assets user journeys, end to end in the browser. Each journey meets its usual
error and reaches its outcome from the page that shows the error, using only the actions
that page offers."""
from __future__ import annotations

import uuid
from decimal import Decimal as D

import pytest
from playwright.sync_api import Page, expect

from celerp.accounting_roles import SEEDED_TARGETS

pytestmark = pytest.mark.browser

API = "/fixed-assets/api"


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


def _line(c, description: str) -> dict:
    return next(i for i in _ok(c.get(f"{API}/intake"))["items"] if description in i["description"])


def _run(c, month: str) -> dict:
    return _operate(c, "/runs", {"month": month})


def _review_and_confirm(page: Page) -> None:
    """Review the form on the page, then confirm what the review shows and wait for the
    page it leads to."""
    page.get_by_role("button", name="Review", exact=True).first.click()
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


def _pick(page: Page, cell, text: str) -> None:
    """Open a click-to-edit account cell and choose the account whose label starts with `text`."""
    cell.dblclick()
    box = page.locator("td.cell--editing .combobox-input").first
    box.click()
    box.fill(text)
    page.locator(".combobox-option:visible", has_text=text).first.click()


def test_j1_header_account_is_refused_and_the_picker_stays_open(page: Page, fresh_company):
    c = fresh_company
    _books(c)
    page.goto("/fixed-assets")
    page.locator("#fa-start").get_by_role("button").click()
    expect(page.locator("#fa-start")).to_have_count(0, timeout=20000)
    vehicles = _classes(c)["Vehicles"]["id"]
    page.goto("/settings/fixed-assets")
    cell = page.locator(f'td[hx-get="/settings/fixed-assets/classes/{vehicles}/asset_account/edit"]')

    _pick(page, cell, "1200 ")
    error = page.locator("td.cell--editing .cell-error")
    expect(error).to_contain_text("1200")
    expect(page.locator("td.cell--editing .combobox-input")).to_be_visible()
    assert _classes(c)["Vehicles"]["asset_account"] != "1200"

    box = page.locator("td.cell--editing .combobox-input").first
    box.click()
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
    page.locator("select[name=old]").select_option(index=1)
    page.locator("select[name=new]").select_option(index=1)
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
    expect(page.locator("#fa-preview .flash--error")).to_contain_text("400.00, not 500.00")
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


def test_j4_locked_date_is_refused_and_the_first_open_date_saves(page: Page, fresh_company):
    c = fresh_company
    _started(c)
    _ok(c.post("/accounting/period-lock", json={"lock_date": "2026-03-31"}))

    page.goto("/fixed-assets")
    page.get_by_role("link", name="Add asset").click()
    page.get_by_role("link", name="Contributed or acquired without a bill").click()
    page.locator("input[name=name]").fill("Owner's car")
    page.locator("select[name=class_id]").select_option(label="Vehicles")
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
