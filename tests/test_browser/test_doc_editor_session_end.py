# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line edit made after the session has ended takes the editor to sign in, with the way back to the
document: it never shows the saved tick or a refused save, and nothing is written."""
from __future__ import annotations

from urllib.parse import quote

import pytest

pytestmark = pytest.mark.browser


def test_an_edit_after_the_session_ends_goes_to_sign_in(page, browser_context, ui_server, api, seeded_user):
    r = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"name": "Fitting", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    cell = page.locator('table.doc-lines tbody tr input[data-name="quantity"]').first
    cell.wait_for(timeout=8000)
    browser_context.clear_cookies()
    browser_context.add_cookies([{"name": "celerp_token", "value": "ended", "domain": "127.0.0.1", "path": "/"}])
    try:
        with page.expect_navigation(url=lambda u: "/login" in u, timeout=10000):
            cell.fill("5")
            cell.dispatch_event("input")
            cell.dispatch_event("change")
            cell.blur()
        assert page.url.endswith("/login?reason=expired&next=" + quote(f"/docs/{doc_id}", safe=""))
    finally:
        browser_context.clear_cookies()
        browser_context.add_cookies([{"name": "celerp_token", "value": seeded_user["access_token"],
                                      "domain": "127.0.0.1", "path": "/"}])
    assert api.get(f"/docs/{doc_id}").json()["line_items"][0]["quantity"] == 1
