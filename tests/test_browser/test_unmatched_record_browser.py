# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: recording an unmatched online payment on an invoice from
Settings > Payments.

Proves, in the running app:
1. The payment's invoice cell shows "--" and opens the searchable invoice list on
   double-click, the shared click-to-edit trigger.
2. Esc puts the cell back unchanged and the row stays.
3. Searching for the invoice and pressing Enter records the payment there and the
   row leaves the list.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.browser

CELL = "td[hx-get$='/invoice/edit']"
SHOTS = os.environ.get("UNMATCHED_SCREENSHOTS")


def _db():
    import psycopg2
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    return conn


@pytest.fixture
def unmatched(fresh_company, monkeypatch):
    """An open invoice of 1,070.00 USD in a fresh company, and an online payment of
    the same amount for an invoice of that company that no longer exists."""
    from jose import jwt
    monkeypatch.setattr("ui.api_client.get_relay_status", AsyncMock(return_value={}))
    monkeypatch.setattr("ui.routes.settings_payments._relay_has_paid_access", lambda _status: True)
    r = fresh_company.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer", "currency": "USD",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 500.0}],
        "subtotal": 1000.0, "tax": 70.0, "total": 1070.0})
    assert r.status_code in (200, 201), r.text
    eid = r.json()["id"]
    assert fresh_company.post(f"/docs/{eid}/finalize").status_code == 200
    ref = fresh_company.get(f"/docs/{eid}").json()["ref_id"]
    company = jwt.get_unverified_claims(fresh_company.headers["Authorization"].split()[1])["company_id"]
    reference = f"pi_browser_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    conn = _db()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO unmatched_payments (reference, amount_minor, currency, former_company, "
                    "document, received_at, paid_at) VALUES (%s, 107000, 'usd', %s, 'doc:deleted', %s, %s)",
                    (reference, company, now, now))
    try:
        yield {"reference": reference, "entity_id": eid, "ref": ref, "api": fresh_company}
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM unmatched_payments WHERE reference = %s", (reference,))
        conn.close()


def _row(page, reference):
    return page.locator("tr", has_text=reference)


def _open(page, ui_server, reference):
    page.goto(f"{ui_server}/settings/payments", wait_until="domcontentloaded")
    cell = _row(page, reference).locator(CELL)
    cell.wait_for(state="visible", timeout=10000)
    assert cell.inner_text().strip() == "--"
    cell.dblclick()
    box = _row(page, reference).locator(".combobox-input")
    box.wait_for(state="visible", timeout=5000)
    # Visible is not ready: wait for the combobox script and HTMX to bind the editor.
    page.wait_for_function(
        "() => { const h = document.querySelector('input[type=hidden][hx-patch$=\"/invoice\"]');"
        " const w = h && h.closest('.combobox-wrap'); const d = h && h['htmx-internal-data'];"
        " return !!(w && w._comboboxInit && d && d.firstInitCompleted); }", timeout=4000)
    return box


def test_esc_leaves_the_payment_where_it_was(page, ui_server, unmatched):
    box = _open(page, ui_server, unmatched["reference"])

    box.press("Escape")

    cell = _row(page, unmatched["reference"]).locator(CELL)
    cell.wait_for(state="visible", timeout=5000)
    assert cell.inner_text().strip() == "--"
    assert not unmatched["api"].get(f"/docs/{unmatched['entity_id']}").json().get("payments")


def test_search_and_enter_records_it_on_the_invoice(page, ui_server, unmatched):
    box = _open(page, ui_server, unmatched["reference"])
    box.fill(unmatched["ref"])
    option = _row(page, unmatched["reference"]).locator(".combobox-option", has_text=unmatched["ref"])
    option.wait_for(state="visible", timeout=5000)

    box.press("Enter")

    _row(page, unmatched["reference"]).wait_for(state="detached", timeout=8000)
    [payment] = unmatched["api"].get(f"/docs/{unmatched['entity_id']}").json()["payments"]
    assert (payment["reference"], payment["amount"]) == (unmatched["reference"], 1070.0)


def test_invoice_list_stays_inside_a_phone_screen(page, ui_server, unmatched):
    page.set_viewport_size({"width": 390, "height": 844})
    box = _open(page, ui_server, unmatched["reference"])
    box.fill(unmatched["ref"])
    option = _row(page, unmatched["reference"]).locator(".combobox-option", has_text=unmatched["ref"])
    option.wait_for(state="visible", timeout=5000)

    r = _row(page, unmatched["reference"]).locator(".combobox-list").bounding_box()
    assert r["x"] >= 0 and r["x"] + r["width"] <= 390, r
    assert r["y"] >= 0 and r["y"] + r["height"] <= 844, r
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/picker-open-390.png")


@pytest.mark.skipif(not SHOTS, reason="screenshots only on request (UNMATCHED_SCREENSHOTS=<dir>)")
@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [390, 1440])
def test_screenshots(page, ui_server, unmatched, browser_context, lang, width):
    browser_context.add_cookies([{"name": "celerp_lang", "value": lang, "domain": "127.0.0.1", "path": "/"}])
    page.set_viewport_size({"width": width, "height": 900})
    try:
        page.goto(f"{ui_server}/settings/payments", wait_until="domcontentloaded")
        table = _row(page, unmatched["reference"]).locator("xpath=ancestor::table")
        table.wait_for(state="visible", timeout=10000)
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/list-{lang}-{width}.png", full_page=True)
        box = _open(page, ui_server, unmatched["reference"])
        box.fill(unmatched["ref"][:4])
        page.screenshot(path=f"{SHOTS}/editing-{lang}-{width}.png", full_page=True)
    finally:
        browser_context.clear_cookies(name="celerp_lang")
