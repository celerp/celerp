# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Refusals reach a German reader in German on the page that raised them."""
from __future__ import annotations

import contextlib
import uuid

import pytest

pytestmark = pytest.mark.browser


@contextlib.contextmanager
def _german(page, ui_server):
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "domain": host, "path": "/"}])
    try:
        yield
    finally:
        page.context.clear_cookies(name="celerp_lang")


def _error_toast(page) -> str:
    page.wait_for_selector(".toast-container .toast--error", timeout=8000)
    return page.locator(".toast-container .toast--error").first.inner_text()


def test_receiving_on_a_draft_bill_is_refused_in_german(page, ui_server, fresh_company):
    tag = uuid.uuid4().hex[:6].upper()
    r = fresh_company.post("/docs", json={"doc_type": "bill", "line_items": [
        {"sku": f"DB-{tag}", "name": "Goods", "quantity": 2, "unit_price": 10, "line_total": 20}]})
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    with _german(page, ui_server):
        page.goto(f"{ui_server}/docs/{bill}", wait_until="load")
        # A draft bill offers no Receive button; the refusal is what an older page or a
        # second tab that still shows it would get.
        page.evaluate("""id => htmx.ajax('POST', `/docs/${id}/receive`, {swap: 'none', values: {
            sku_0: 'x', qty_0: '2'}})""", bill)
        toast = _error_toast(page)
    assert "Diese Rechnung ist noch ein Entwurf" in toast, toast
    assert "Finalize" not in toast, toast
