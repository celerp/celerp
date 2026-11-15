# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Browser journey for the bank page: drop a statement, check the preview, import, filter
through the URL, untick a line, answer one line on its own with the searchable Answer,
see the parts refusal, Post ticked and Undo, Escape out of a panel, the stacked layout
on a phone, and the Questions list printed with "--" for an empty field."""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_CSV = (b"Date,Description,Amount\n"
        b"2026-01-05,NETFLIX.COM,-15.99\n2026-01-06,NETFLIX.COM,-15.99\n2026-01-07,NETFLIX.COM,-15.99\n"
        b"2026-01-09,WITHDRAWAL,-200.00\n2026-01-12,MIXED PURCHASE,-100.00\n")


def _bank(api) -> str:
    r = api.post("/accounting/bank-accounts", json={
        "bank_name": f"Journey {uuid.uuid4().hex[:6]}", "account_number": "****4242", "bank_type": "checking",
        "currency": api.get("/companies/me").json().get("currency") or "USD", "opening_balance": 0})
    assert r.status_code == 200, r.text
    r = api.post("/accounting/rules", json={"match_pattern": "NETFLIX", "target_account_code": "5100",
                                            "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    # A client to ask: a viewer whose role answers bank questions.
    r = api.patch("/companies/me/role-permissions",
                  json={"perm_key": "answer_bank_questions", "role_key": "viewer", "granted": True})
    assert r.status_code == 200, r.text
    r = api.post("/companies/me/users", json={"email": f"client-{uuid.uuid4().hex[:6]}@example.com",
                                               "name": "Client", "role": "viewer", "password": "clientpw123"})
    assert r.status_code == 200, r.text
    return api.get("/accounting/bank-accounts").json()["items"][-1]["id"]


def test_bank_page_browser_journey(page: Page, api, tmp_path):
    bank_id = _bank(api)
    page.goto(f"/accounting/reconcile/bank/{bank_id}")
    expect(page.get_by_text("Start without a file")).to_be_visible()

    # Drop a statement: the preview shows its lines and asks for the header figures.
    f = tmp_path / "jan.csv"
    f.write_bytes(_CSV)
    page.locator('input[type="file"]').set_input_files(str(f))
    preview = page.locator("[data-testid=statement-preview]")
    expect(preview).to_be_visible(timeout=15000)
    expect(preview.get_by_text("MIXED PURCHASE")).to_be_visible()
    for name, value in (("opening_balance", "0.00"), ("closing_balance", "-347.97"),
                        ("period_start", "2026-01-01"), ("period_end", "2026-01-31")):
        cell = preview.locator(f"[data-field={name}]")
        cell.click()
        cell.locator("input").fill(value)
        with page.expect_response(lambda r: "/preview/" in r.url and r.request.method == "POST"):
            cell.locator("input").press("Enter")
    expect(preview.get_by_text("The statement ties")).to_be_visible()
    preview.get_by_role("button", name="Import").click()
    page.get_by_role("button", name="Confirm").click()

    # Suggest, then the URL carries the filters and the view.
    page.get_by_role("button", name="Suggest").click()
    page.get_by_role("link", name="Suggested").click()
    expect(page).to_have_url(re.compile(rf"/accounting/reconcile/bank/{bank_id}\?view=suggested"))
    page.goto(f"/accounting/reconcile/bank/{bank_id}?view=all&q=NETFLIX")
    netflix = page.locator("[data-group]").filter(has_text="NETFLIX")
    expect(netflix.locator("input[type=checkbox][data-role=group]")).to_be_checked()

    # Untick one line of the pre-ticked group.
    netflix.get_by_role("button", name="Show lines").click()
    lines = netflix.locator("[data-line]")
    expect(lines).to_have_count(3)
    lines.nth(2).locator("input[type=checkbox]").uncheck()
    expect(page.locator(".bulk-toolbar")).to_contain_text("2 lines")

    # Answer one line on its own with the searchable Answer dropdown.
    page.goto(f"/accounting/reconcile/bank/{bank_id}?view=all")
    withdrawal = page.locator("[data-line]").filter(has_text="WITHDRAWAL")
    withdrawal.get_by_role("button", name="Answer separately").click()
    answer = withdrawal.locator(".searchable-select .combobox-input").first
    answer.fill("Office")
    page.get_by_role("option", name=re.compile("6800")).first.click()
    withdrawal.get_by_role("button", name="Post").click()
    expect(withdrawal).to_contain_text("Posted")

    # The parts refusal on a split that does not add up.
    mixed = page.locator("[data-line]").filter(has_text="MIXED PURCHASE")
    mixed.get_by_role("button", name="Split").click()
    split = page.locator("form[data-form=split]")
    split.locator(".combobox-input").first.fill("Office")
    page.get_by_role("option", name=re.compile("6800")).first.click()
    split.locator("input[name=amount]").first.fill("60")
    split.get_by_role("button", name="Post").click()
    expect(split).to_contain_text("add up to")
    # Escape closes the split form without saving.
    split.locator("input[name=amount]").first.press("Escape")
    expect(page.locator("form[data-form=split]")).to_have_count(0)

    # Post ticked, then Undo one posted line.
    page.get_by_role("button", name="Post ticked").click()
    page.get_by_role("button", name="Confirm").click()
    expect(page.locator(".bulk-toolbar, [data-testid=outcome]")).to_contain_text("Posted 2")
    page.goto(f"/accounting/reconcile/bank/{bank_id}?view=posted")
    first = page.locator("[data-line]").first
    first.get_by_role("button", name="Undo").click()
    page.get_by_role("button", name="Confirm").click()
    expect(first).to_contain_text("Open")

    # Ask the client: the note panel closes on Escape and sends nothing.
    page.goto(f"/accounting/reconcile/bank/{bank_id}?view=all")
    ask = page.get_by_role("button", name="Ask the client")
    page.locator("[data-line]").filter(has_text="NETFLIX").filter(has=ask).first.locator(ask).click()
    note = page.locator("[data-panel=ask] textarea")
    note.fill("What is this?")
    note.press("Escape")
    expect(page.locator("[data-panel=ask]")).to_have_count(0)
    # Then ask for real: the line shows Remind instead.
    asked = page.locator("[data-line]").filter(has_text="NETFLIX").filter(has=ask).first
    asked_id = asked.get_attribute("data-line")
    asked.locator(ask).click()
    note.fill("What is this?")
    page.locator("[data-panel=ask]").get_by_role("button", name="Ask", exact=True).click()
    expect(page.locator(f"[data-line='{asked_id}']")).to_contain_text("Remind")

    # Thumbnails keep one fixed dimension.
    for img in page.locator("img.receipt-thumb").all():
        assert img.evaluate("e => getComputedStyle(e).height") == "48px"
        assert img.evaluate("e => e.style.width") in ("", "auto")

    # A phone stacks the group table.
    page.set_viewport_size({"width": 390, "height": 800})
    page.reload()
    box = page.locator("[data-group]").first.bounding_box()
    assert box and box["width"] <= 390
    page.set_viewport_size({"width": 1280, "height": 900})

    # Questions print with "--" for an empty field.
    page.goto(f"/accounting/reconcile/bank/{bank_id}?view=questions")
    page.emulate_media(media="print")
    expect(page.locator("[data-line]").first).to_contain_text("--")
    page.emulate_media(media="screen")
