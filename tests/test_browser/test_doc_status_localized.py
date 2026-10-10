# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document's status reads in the reader's language on its page title and its badge."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def test_a_void_invoice_reads_anulado_in_spanish(page, ui_server, fresh_company):
    r = fresh_company.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"description": "Service", "quantity": 1, "unit_price": 10.0, "line_total": 10.0}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert fresh_company.post(f"/docs/{doc_id}/finalize").status_code == 200
    assert fresh_company.post(f"/docs/{doc_id}/void", json={}).status_code == 200

    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "es", "domain": host, "path": "/"}])
    try:
        page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
        title = page.locator("h1").first.inner_text()
        badge = page.locator(".badge--void").first.text_content()
    finally:
        page.context.clear_cookies(name="celerp_lang")
    assert "Anulado" in title, title
    assert "Void" not in title, title
    assert badge.strip() == "Anulado", badge
