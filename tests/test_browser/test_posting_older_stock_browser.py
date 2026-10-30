# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock placed at start reads cleanly on its tab on a laptop screen, moves in bulk
and the move undoes; a notice that asks the user to act leads the bell and stands out."""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from .conftest import css_class
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


def _older_stock(client, tag: str, costs: list[float]) -> list[str]:
    """Lots brought in by the batch import, then left as an older release left them: no lot
    records its inventory account, and no event records one."""
    records = [{"entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": "csv",
                "idempotency_key": f"older:{uuid.uuid4()}",
                "data": {"sku": f"OLD-{tag}-{i}", "name": "Older ring", "quantity": 1, "sell_by": "piece",
                         "status": "available", "cost_total": cost}} for i, cost in enumerate(costs)]
    r = client.post("/items/import/batch", json={"records": records})
    assert r.status_code == 200, r.text
    ids = [rec["entity_id"] for rec in records]
    _db("UPDATE projections SET state = (state::jsonb - 'inventory_account_code')::json"
        " WHERE company_id = %s AND entity_id = ANY(%s)", _company_id(client), ids)
    _db("DELETE FROM ledger WHERE company_id = %s AND entity_id = ANY(%s)"
        " AND event_type = 'item.inventory_account.recorded'", _company_id(client), ids)
    return ids


def _start(client) -> None:
    """What the next start does for the company: place its older stock. Run on its own
    thread, since the page's sync driver holds this thread's event loop."""
    import asyncio
    import threading

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from celerp.services.lot_origin import place_older_stock

    async def run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                placed = await place_older_stock(s, uuid.UUID(_company_id(client)))
                await s.commit()
                return placed
        finally:
            await engine.dispose()

    placed: list[bool] = []
    worker = threading.Thread(target=lambda: placed.append(asyncio.run(run())))
    worker.start()
    worker.join()
    assert placed == [True]


def test_placed_older_stock_moves_in_bulk_and_the_move_undoes(page, ui_server, fresh_company, tmp_path):
    from playwright.sync_api import expect

    tag = uuid.uuid4().hex[:6].upper()
    _older_stock(fresh_company, tag, [260.0, 40.0])
    _start(fresh_company)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{ui_server}/settings/accounting?tab=older-stock", wait_until="domcontentloaded")

    table = page.locator("#older-stock-table")
    expect(table.locator("tbody tr", has_text=f"OLD-{tag}")).to_have_count(2)
    # Nothing on the tab is cut off: the table fits its card and the page fits the window.
    fit = page.evaluate("""() => {
        const wrap = document.querySelector('#older-stock-table').closest('.table-scroll-wrap');
        return {wrap: wrap.scrollWidth - wrap.clientWidth,
                page: document.documentElement.scrollWidth - window.innerWidth};
    }""")
    assert fit == {"wrap": 0, "page": 0}, fit
    page.screenshot(path=str(tmp_path / "older-stock-1280.png"), full_page=True)

    page.locator("#older-stock-table .bulk-select-all").check()
    page.locator("#bulkbar-older-stock-table .bulk-action-select").select_option("move")
    picker = page.locator("#bulkbar-older-stock-table .bulk-fields .combobox-wrap")
    picker.locator(".combobox-input").fill("1130-P")
    picker.locator('.combobox-option[data-value="1130-P"]').click()
    page.locator("#bulkbar-older-stock-table .bulk-apply").click()
    expect(page.locator("#older-stock-card")).to_contain_text("Moved 2 lots to 1130-P.")
    rows = page.locator("#older-stock-table tbody tr", has_text=f"OLD-{tag}")
    for i in range(2):
        expect(rows.nth(i)).to_contain_text("1130-P")

    page.locator("#older-stock-moves button", has_text="Undo").first.click()
    for i in range(2):
        expect(page.locator("#older-stock-table tbody tr", has_text=f"OLD-{tag}").nth(i)).to_contain_text(
            "1130-OB")


def test_a_notice_asking_for_action_leads_the_bell_and_stands_out(page, ui_server, fresh_company):
    from playwright.sync_api import expect

    company = _company_id(fresh_company)
    tag = uuid.uuid4().hex[:6]
    _db("INSERT INTO notifications (id, company_id, category, title, body, priority, created_at)"
        " VALUES (gen_random_uuid(), %s, 'accounting', %s, 'Act on this', 'high',"
        " now() - interval '1 hour')", company, f"Act {tag}")
    for n in (1, 2):
        _db("INSERT INTO notifications (id, company_id, category, title, body, priority, created_at)"
            " VALUES (gen_random_uuid(), %s, 'ai', %s, 'Later news', 'medium', now())",
            company, f"News {n} {tag}")
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.click(".notif-bell-btn")
    items = page.locator("#notif-panel .notif-item")
    expect(items.first).to_contain_text(f"Act {tag}")
    news = page.locator("#notif-panel .notif-item", has_text=f"News 1 {tag}")
    expect(news).to_be_visible()
    expect(items.first).to_have_class(css_class("notif-item--high"))
    expect(news).not_to_have_class(css_class("notif-item--high"))
    # Opening the panel and loading the page each render the list again, so both colours
    # are read in one step: a read split in two can land on a notice the panel replaced.
    colours = page.evaluate("""news => {
        const items = [...document.querySelectorAll('#notif-panel .notif-item')];
        const colour = el => getComputedStyle(el).borderLeftColor;
        return [colour(items[0]), colour(items.find(el => el.textContent.includes(news)))];
    }""", f"News 1 {tag}")
    assert colours[0] != colours[1], colours
