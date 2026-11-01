# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A control that asks before acting asks in the app's own dialog, never the browser's plain
box: Cancel sends nothing, Confirm runs the action."""
from __future__ import annotations

import uuid

import pytest

from .conftest import answer_in_app

pytestmark = pytest.mark.browser


def test_a_question_opens_the_app_dialog_and_cancel_keeps_the_record(page, ui_server, api):
    cid = api.post("/crm/contacts", json={"name": f"Confirm {uuid.uuid4().hex[:6]}",
                                          "contact_type": "customer"}).json()["id"]
    for city in ("Primary Town", "Second Town"):
        r = api.post(f"/crm/contacts/{cid}/addresses", json={"line1": "1 Road", "city": city,
                                                            "is_default": city == "Primary Town"})
        assert r.status_code in (200, 201), r.text
    native: list[str] = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    page.goto(f"{ui_server}/contacts/{cid}", wait_until="domcontentloaded")
    remove = page.locator("#addresses-section .addr-actions .btn--danger")
    remove.wait_for(timeout=8000)

    remove.click()
    dialog = page.locator("dialog.confirm-dialog[open]")
    dialog.wait_for(timeout=3000)
    assert dialog.locator(".confirm-dialog__question").inner_text().strip()
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached", timeout=3000)
    remove.click()
    answer_in_app(page, accept=False)
    page.wait_for_timeout(500)
    assert len(api.get(f"/crm/contacts/{cid}").json()["addresses"]) == 2

    remove.click()
    with page.expect_response(lambda r: r.request.method == "DELETE"):
        answer_in_app(page)
    page.wait_for_timeout(300)
    assert [a["city"] for a in api.get(f"/crm/contacts/{cid}").json()["addresses"]] == ["Primary Town"]
    assert native == []
