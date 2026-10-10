# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line action sent from the page shows the server's success toast after the page reloads."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser

_DONE = "1 line set as reserved."


def test_a_line_action_shows_its_toast_after_the_reload(page, ui_server, api):
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft",
                                "line_items": [{"name": "Row", "quantity": 1, "unit_price": 10.0,
                                                "line_total": 10.0}]})
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    page.route("**/reserve-lines", lambda route: route.fulfill(
        status=204, headers={"HX-Trigger": json.dumps({"celerpToast": {"message": _DONE, "type": "info"}})}))
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.locator("#line-body tr").first.wait_for(timeout=8000)
    ok = page.evaluate(f"celerpLineAction('/docs/{doc_id}/reserve-lines', [], [], 'k', 'failed')")
    assert ok is True
    page.reload(wait_until="domcontentloaded")
    page.locator(".toast--info .toast__msg", has_text=_DONE).wait_for(timeout=8000)
