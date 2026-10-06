# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Merging from the Catalog: a product linked to a connected store is refused with a
toast naming the store; once the store is disconnected, the same merge goes through."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from ui.i18n import t

from .test_migration_wizard_browser import _pg_admin

pytestmark = pytest.mark.browser


def _db(sql: str, *params) -> list[tuple]:
    conn = _pg_admin(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def _start_merge(page, ui_server, tag: str, target_id: str) -> None:
    page.goto(f"{ui_server}/inventory?q=MAD-{tag}", wait_until="domcontentloaded")
    page.wait_for_selector("input.row-select", timeout=8000)
    rows = page.locator("input.row-select")
    assert rows.count() == 2
    # Reopening the same list restores the previous ticks; clear them so the toolbar follows.
    for i in range(2):
        rows.nth(i).uncheck()
    for i in range(2):
        rows.nth(i).check()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=5000)
    page.select_option("#bulk-action-select", "merge")
    page.wait_for_selector("#merge-target-select", timeout=5000)
    page.select_option("#merge-target-select", target_id)
    page.click("#merge-confirm button:has-text('Confirm')")


def test_inventory_merge_after_disconnect_browser(page, ui_server, api):
    from celerp.services.auth import decode_access_token
    tag = uuid.uuid4().hex[:6].upper()
    a = api.post("/items", json={"status": "available", "sku": f"MAD-{tag}-A", "name": "Linked A",
                                 "quantity": 5, "sell_by": "piece"})
    b = api.post("/items", json={"status": "available", "sku": f"MAD-{tag}-B", "name": "Local B",
                                 "quantity": 3, "sell_by": "piece"})
    assert a.status_code in {200, 201} and b.status_code in {200, 201}
    a_id = a.json()["id"]
    company_id = decode_access_token(api.headers["Authorization"].split(" ", 1)[1])["company_id"]
    state = _db("SELECT state FROM projections WHERE company_id = %s AND entity_id = %s", company_id, a_id)[0][0]
    state["external_links"] = {"shopify": {"product_id": "9001", "variant_id": "9002", "sync_enabled": True}}
    _db("UPDATE projections SET state = CAST(%s AS json) WHERE company_id = %s AND entity_id = %s",
        json.dumps(state), company_id, a_id)
    assert not _db("SELECT 1 FROM connector_configs WHERE connector = 'shopify'")
    _db("INSERT INTO connector_configs (company_id, connector, direction, sync_frequency, daily_sync_hour) "
        "VALUES (%s, 'shopify', 'both', 'realtime', 2)", company_id)
    try:
        page.set_viewport_size({"width": 1440, "height": 1000})
        _start_merge(page, ui_server, tag, a_id)
        page.wait_for_selector(".toast-container .toast--error", timeout=8000)
        toast = page.locator(".toast-container .toast--error").inner_text()
        assert t("inventory.err_merge_linked", "en", stores="Shopify") in toast, toast

        # Shopify is disconnected; the item keeps its old Shopify ids as history.
        _db("DELETE FROM connector_configs WHERE company_id = %s AND connector = 'shopify'", company_id)
        _start_merge(page, ui_server, tag, a_id)
        page.wait_for_url(f"**/inventory?q=MAD-{tag}-A", timeout=8000)
    finally:
        _db("DELETE FROM connector_configs WHERE company_id = %s AND connector = 'shopify'", company_id)
