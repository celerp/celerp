# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock on the posting-accounts tab reads cleanly on a laptop screen, and a
notice that asks the user to act leads the bell and stands out."""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from .test_migration_wizard_browser import _pg_admin

pytestmark = pytest.mark.browser


def _db(sql: str, *params) -> None:
    conn = _pg_admin(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


def _company_id(client) -> str:
    token = client.headers["Authorization"].split()[1]
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["company_id"]


def _older_lot(client, sku: str, cost: float) -> str:
    """A lot as an older release left it: its inventory account was never recorded."""
    r = client.post("/items", json={"sku": sku, "name": "Older ring", "quantity": 1,
                                    "sell_by": "piece", "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    _db("UPDATE projections SET state = (state::jsonb - 'inventory_account_code')::json"
        " WHERE company_id = %s AND entity_id = %s", _company_id(client), item_id)
    return item_id


def test_older_stock_reads_cleanly_at_laptop_width_and_asks_before_the_final_choice(
        page, ui_server, fresh_company, tmp_path):
    from playwright.sync_api import expect

    tag = uuid.uuid4().hex[:6].upper()
    _older_lot(fresh_company, f"OLD-{tag}", 260.0)
    _older_lot(fresh_company, f"ZERO-{tag}", 0.0)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{ui_server}/settings/accounting?tab=posting-accounts", wait_until="domcontentloaded")

    table = page.locator("table.posting-accounts")
    row = table.locator("tr", has_text=f"OLD-{tag}")
    expect(row).to_have_count(1)
    expect(table.locator("tr", has_text=f"ZERO-{tag}")).to_have_count(0)  # holds nothing
    cells = row.locator("td")
    assert cells.nth(0).inner_text().strip() == f"Older stock OLD-{tag} Older ring"
    assert cells.nth(2).inner_text().strip() == "Valued at $260.00"
    expect(page.locator("#older-stock-hint")).to_have_count(1)

    # Nothing on the tab is cut off: the table fits its card and the page fits the window.
    fit = page.evaluate("""() => {
        const wrap = document.querySelector('table.posting-accounts').closest('.table-scroll-wrap');
        return {wrap: wrap.scrollWidth - wrap.clientWidth,
                page: document.documentElement.scrollWidth - window.innerWidth};
    }""")
    assert fit == {"wrap": 0, "page": 0}, fit
    page.screenshot(path=str(tmp_path / "older-stock-1280.png"), full_page=True)

    dialogs: list[str] = []

    def _dismiss(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _dismiss)
    cells.nth(1).click()
    picker = row.locator("[hx-patch]")
    expect(picker).to_have_count(1)
    picker.evaluate("""el => {
        el.value = '1130-OB';
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    page.wait_for_timeout(300)
    assert dialogs and f"OLD-{tag}" in dialogs[0], dialogs  # the final choice asks first
    # Dismissed: nothing was recorded, the lot is still waiting for its account.
    page.goto(f"{ui_server}/settings/accounting?tab=posting-accounts", wait_until="domcontentloaded")
    expect(page.locator("table.posting-accounts tr", has_text=f"OLD-{tag}").locator("td").nth(2)) \
        .to_have_text("Valued at $260.00")


def test_a_notice_asking_for_action_leads_the_bell_and_stands_out(page, ui_server, fresh_company):
    from playwright.sync_api import expect

    company = _company_id(fresh_company)
    tag = uuid.uuid4().hex[:6]
    _db("INSERT INTO notifications (id, company_id, category, title, body, priority, read, created_at)"
        " VALUES (gen_random_uuid(), %s, 'accounting', %s, 'Act on this', 'high', false,"
        " now() - interval '1 hour')", company, f"Act {tag}")
    for n in (1, 2):
        _db("INSERT INTO notifications (id, company_id, category, title, body, priority, read, created_at)"
            " VALUES (gen_random_uuid(), %s, 'ai', %s, 'Later news', 'medium', false, now())",
            company, f"News {n} {tag}")
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.click(".notif-bell-btn")
    items = page.locator("#notif-panel .notif-item")
    expect(items.first).to_contain_text(f"Act {tag}")
    first = items.first
    assert "notif-item--high" in (first.get_attribute("class") or "")
    news = page.locator("#notif-panel .notif-item", has_text=f"News 1 {tag}")
    assert "notif-item--high" not in (news.get_attribute("class") or "")
    colour = first.evaluate("el => getComputedStyle(el).borderLeftColor")
    plain = news.evaluate("el => getComputedStyle(el).borderLeftColor")
    assert colour != plain, (colour, plain)
