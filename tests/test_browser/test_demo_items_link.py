# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard's demo note carries a "Remove demo items" link while any of setup's
samples is untouched. It opens the inventory list filtered to those samples, with a
hint pointing at the select-all box: "Tick the box to select them all, then choose
Delete." Nothing is ticked for the owner; select-all plus the existing bulk Delete
removes exactly the untouched samples, never an edited or renamed one or the owner's
own item named "[DEMO] ...". The hint goes on Esc or its close button and never
covers a control.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from .test_import_hint import _OVERLAPS_JS

pytestmark = pytest.mark.browser

_HINT = "Tick the box to select them all, then choose Delete."
_LINK = "Remove demo items"
_DEMO_URL = "/inventory?filter=demo&hint=demo"
_REAL = "Real teak chair"
_RENAMED = "House cattle feed"
_EDITED = "[DEMO] Jasmine Rice - 25kg bag"
_MINE = "[DEMO] My own showroom piece"
# What the owner keeps after removing the samples.
_KEPT = sorted([_EDITED, _MINE, _RENAMED, _REAL])
_LOCALES = Path(__file__).resolve().parents[2] / "ui" / "locales"


def _items(api) -> list[dict]:
    r = api.get("/items", params={"status": "all", "limit": 500})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _seed_demo_items(api) -> None:
    """An added company starts without samples; seed the agricultural set the way
    setup does for a new install."""
    import asyncio
    import base64
    import os
    import threading
    import uuid

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from celerp.models.company import Location
    from celerp.services.demo import seed_demo_items

    claims = api.headers["Authorization"].split()[1].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    company_id, user_id = uuid.UUID(claims["company_id"]), uuid.UUID(claims["sub"])

    async def _run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with AsyncSession(engine) as session:
                loc = (await session.execute(sa.select(Location.id).where(
                    Location.company_id == company_id, Location.is_default == True))).scalar()  # noqa: E712
                await seed_demo_items(session, company_id, user_id, vertical="agricultural",
                                      default_location_id=loc)
                await session.commit()
        finally:
            await engine.dispose()

    # Playwright's sync API owns this thread's event loop.
    t = threading.Thread(target=asyncio.run, args=(_run(),))
    t.start()
    t.join()


def _seed(api) -> None:
    """Five agricultural samples: one renamed by the owner, one with its quantity
    changed (name kept), three untouched. Plus a real item and the owner's own item
    named "[DEMO] ..."."""
    _seed_demo_items(api)
    by_sku = {i["sku"]: i for i in _items(api)}
    feed, rice = by_sku.get("DEMO-AGR-005"), by_sku.get("DEMO-AGR-001")
    assert feed and rice, sorted(by_sku)
    assert rice["name"] == _EDITED, rice["name"]
    r = api.patch(f"/items/{feed['id']}", json={"fields_changed": {"name": {"old": feed["name"], "new": _RENAMED}}})
    assert r.status_code == 200, r.text
    r = api.patch(f"/items/{rice['id']}", json={"fields_changed": {"quantity": {"old": rice.get("quantity"), "new": 999}}})
    assert r.status_code == 200, r.text
    for sku, name in (("REAL-1", _REAL), ("MINE-1", _MINE)):
        r = api.post("/items", json={"sku": sku, "name": name, "sell_by": "piece", "quantity": 1})
        assert r.status_code in (200, 201), r.text


def _untouched(api) -> list[dict]:
    r = api.get("/items", params={"filter": "demo", "limit": 500})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _table_names(page: Page) -> list[str]:
    return page.locator("#data-table tbody tr").all_inner_texts()


def _ticked(page: Page) -> int:
    return page.evaluate("document.querySelectorAll('#data-table input[type=checkbox]:checked').length")


def test_note_link_shows_while_demo_items_exist_and_goes_when_none(page: Page, fresh_company):
    _seed(fresh_company)
    page.goto("/dashboard")
    note = page.locator("#demo-note")
    expect(note).to_be_visible()
    link = note.locator("#remove-demo-items")
    expect(link).to_have_text(_LINK)
    assert link.get_attribute("href") == _DEMO_URL
    ids = [i["id"] for i in _untouched(fresh_company)]
    assert len(ids) == 3, ids
    assert fresh_company.post("/items/bulk/delete", json={"entity_ids": ids}).status_code == 200
    page.reload()
    expect(page.locator("h1.page-title")).to_be_visible()
    expect(page.locator("#demo-note")).to_have_count(0)
    expect(page.locator("#remove-demo-items")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 1280])
def test_link_lists_only_demo_items_with_hint_and_nothing_ticked(page: Page, fresh_company, width):
    _seed(fresh_company)
    page.set_viewport_size({"width": width, "height": 800})
    page.goto("/dashboard")
    page.locator("#remove-demo-items").click()
    page.wait_for_url("**/inventory*")
    tip = page.locator(".import-arrow")
    expect(tip).to_contain_text(_HINT)
    assert "hint=demo" not in page.url
    rows = _table_names(page)
    assert len(rows) == 3, rows
    assert all("[DEMO]" in r for r in rows), rows
    assert not any(name in r for r in rows for name in _KEPT), rows
    assert _ticked(page) == 0
    page.wait_for_load_state("load")
    assert page.evaluate(_OVERLAPS_JS) == [], width
    # Both rects in one read: late shell layout (the company switcher row at phone
    # width) moves the hint and the box together, never one without the other.
    t, box = page.evaluate("""() => [document.querySelector('.import-arrow'),
        document.querySelector('#select-all-rows')].map(e => e.getBoundingClientRect().toJSON())""")
    assert t["bottom"] <= box["top"], (width, "the hint sits above the box", t, box)
    assert t["left"] < box["right"] and box["left"] < t["right"], (width, t, box)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width


def _select_all_and_delete(page: Page) -> None:
    page.on("dialog", lambda d: d.accept())
    page.locator("#select-all-rows").check()
    expect(page.locator("#bulk-count")).to_contain_text("3")
    page.select_option("#bulk-action-select", "delete")
    expect(page.locator("#data-table tbody tr.row-item, #data-table tbody tr[id^=row-]")).to_have_count(0)


def test_select_all_and_delete_removes_exactly_the_demo_items(page: Page, fresh_company):
    _seed(fresh_company)
    page.goto(_DEMO_URL)
    expect(page.locator(".import-arrow")).to_be_visible()
    _select_all_and_delete(page)
    assert sorted(i["name"] for i in _items(fresh_company)) == _KEPT
    page.goto("/dashboard")
    expect(page.locator("h1.page-title")).to_be_visible()
    expect(page.locator("#demo-note")).to_have_count(0)


def test_an_earlier_selection_is_not_carried_into_the_demo_delete(page: Page, fresh_company):
    """A row ticked earlier in the session stays in the bulk selection across pages.
    Arriving from the link starts with nothing selected, so Delete takes only what the
    owner ticks on this list."""
    _seed(fresh_company)
    page.goto("/inventory")
    page.locator("#data-table tbody tr", has_text=_REAL).locator("input.row-select").check()
    expect(page.locator("#bulk-count")).to_contain_text("1")
    page.goto("/dashboard")
    page.locator("#remove-demo-items").click()
    page.wait_for_url("**/inventory*")
    expect(page.locator(".import-arrow")).to_be_visible()
    assert _ticked(page) == 0
    _select_all_and_delete(page)
    assert sorted(i["name"] for i in _items(fresh_company)) == _KEPT


def test_hint_dismissed_by_esc_and_by_its_close(page: Page, fresh_company):
    _seed(fresh_company)
    page.goto(_DEMO_URL)
    expect(page.locator(".import-arrow")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".import-arrow")).to_have_count(0)
    expect(page.locator("#select-all-rows")).not_to_have_class("import-arrow-pulse")
    page.goto(_DEMO_URL)
    page.locator(".import-arrow").get_by_role("button", name="Close").click()
    expect(page.locator(".import-arrow")).to_have_count(0)
    assert _ticked(page) == 0
    page.reload()
    page.wait_for_load_state("load")
    expect(page.locator(".import-arrow")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 1280])
def test_link_and_hint_in_german(page: Page, fresh_company, width):
    en = json.loads((_LOCALES / "en.json").read_text())
    de = json.loads((_LOCALES / "de.json").read_text())
    _seed(fresh_company)
    page.set_viewport_size({"width": width, "height": 800})
    page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})
    page.goto("/dashboard")
    link = page.locator("#remove-demo-items")
    expect(link).to_have_text(de["dashboard.remove_demo_items"])
    assert de["dashboard.remove_demo_items"] != en["dashboard.remove_demo_items"]
    link.click()
    page.wait_for_url("**/inventory*")
    tip = page.locator(".import-arrow")
    expect(tip).to_contain_text(de["shell.demo_hint"])
    assert en["shell.demo_hint"] not in tip.inner_text()
    page.wait_for_load_state("load")
    assert page.evaluate(_OVERLAPS_JS) == [], width
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width


def test_back_to_the_demo_list_drops_a_selection_made_elsewhere(page: Page, fresh_company):
    """Red statement: the selection was cleared only when the one-time hint showed, so
    Back to the demo list after ticking a real item elsewhere showed "4 selected" over
    3 rows and Delete removed the real item with the samples."""
    _seed(fresh_company)
    page.goto("/dashboard")
    page.locator("#remove-demo-items").click()
    expect(page.locator(".import-arrow")).to_be_visible()
    page.goto("/inventory?q=Real")
    page.locator("#data-table tbody tr", has_text=_REAL).locator("input.row-select").check()
    expect(page.locator("#bulk-count")).to_contain_text("1")
    page.go_back()
    page.wait_for_url("**/inventory?filter=demo")
    expect(page.locator(".import-arrow")).to_have_count(0)
    assert _ticked(page) == 0
    _select_all_and_delete(page)
    assert sorted(i["name"] for i in _items(fresh_company)) == _KEPT
