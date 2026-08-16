# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A manually created item starts as Draft on the inventory page: it wears the Draft
badge, the Drafts counter card shows it, and making it available flips both.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.browser


def _clear_session_registry() -> None:
    """Wipe session_registry rows so a second user can log in (the direct
    connection limit allows one active session; nonces stay valid)."""
    import psycopg2
    from urllib.parse import urlsplit
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM session_registry;")
    conn.close()


def _set_cookie(browser_context, token: str) -> None:
    browser_context.clear_cookies()
    browser_context.add_cookies([{
        "name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/",
    }])


def test_draft_lifecycle_in_ui(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    sku = f"DFT-{tag}"
    r = api.post("/items", json={"sku": sku, "name": "Draft Widget", "sell_by": "piece",
                                 "quantity": 3, "cost_price": 25.0})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    page.goto(f"{ui_server}/inventory?q={sku}", wait_until="domcontentloaded")
    page.wait_for_selector(".badge--draft", timeout=8000)

    # The Drafts counter card is on the page with a non-zero count.
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    card = page.locator('.status-card[href*="status=draft"], a[href*="status=draft"]').first
    card.wait_for(timeout=8000)
    assert card.inner_text().strip() != ""

    r = api.post("/items/bulk/make-available", json={"entity_ids": [item_id]})
    assert r.status_code == 200, r.text
    page.goto(f"{ui_server}/inventory?q={sku}", wait_until="domcontentloaded")
    page.wait_for_selector(".badge--available", timeout=8000)
    assert page.locator(".badge--draft").count() == 0


def test_draft_amounts_editable_for_restricted_role(page, ui_server, api, api_server,
                                                    browser_context, seeded_user):
    """An operator holding edit_inventory but not edit_inventory_amounts sees a
    draft row's quantity as double-click editable, while the same cell on an
    available row stays a plain read-only value: the lock attaches at commit."""
    tag = uuid.uuid4().hex[:6]
    draft_sku, avail_sku = f"OPD-{tag}", f"OPA-{tag}"
    r = api.patch("/companies/me/role-permissions",
                  json={"perm_key": "edit_inventory_amounts", "role_key": "operator",
                        "granted": False})
    assert r.status_code == 200, r.text
    r = api.post("/companies/me/users",
                 json={"email": f"op-{tag}@celerp.test", "name": "Operator",
                       "role": "operator", "password": "pw12345"})
    assert r.status_code == 200, r.text

    for sku, make_available in ((draft_sku, False), (avail_sku, True)):
        r = api.post("/items", json={"sku": sku, "name": "Role Widget", "sell_by": "piece",
                                     "quantity": 4, "cost_price": 15.0})
        assert r.status_code == 200, r.text
        if make_available:
            r2 = api.post("/items/bulk/make-available", json={"entity_ids": [r.json()["id"]]})
            assert r2.status_code == 200, r2.text

    _clear_session_registry()
    lr = httpx.post(f"{api_server}/auth/login",
                    json={"email": f"op-{tag}@celerp.test", "password": "pw12345"}, timeout=10)
    assert lr.status_code == 200, lr.text
    try:
        _set_cookie(browser_context, lr.json()["access_token"])

        page.goto(f"{ui_server}/inventory?q={draft_sku}", wait_until="domcontentloaded")
        page.wait_for_selector(".badge--draft", timeout=8000)
        qty = page.locator(f'tr:has-text("{draft_sku}") td[data-col="quantity"] .paired-primary').first
        qty.wait_for(timeout=8000)
        assert qty.get_attribute("title") == "Double-click to edit"
        assert "paired-primary--readonly" not in (qty.get_attribute("class") or "")

        page.goto(f"{ui_server}/inventory?q={avail_sku}", wait_until="domcontentloaded")
        page.wait_for_selector(".badge--available", timeout=8000)
        qty2 = page.locator(f'tr:has-text("{avail_sku}") td[data-col="quantity"] .paired-primary').first
        qty2.wait_for(timeout=8000)
        assert "paired-primary--readonly" in (qty2.get_attribute("class") or "")
        assert qty2.get_attribute("title") is None
    finally:
        _set_cookie(browser_context, seeded_user["access_token"])
        api.patch("/companies/me/role-permissions",
                  json={"perm_key": "edit_inventory_amounts", "role_key": "operator",
                        "granted": True})
