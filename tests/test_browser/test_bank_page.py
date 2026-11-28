# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Browser journey for the bank page: drop a statement, check the preview, import, filter
through the URL, untick a line, answer one line on its own with the searchable Answer,
see the parts refusal, Post ticked and Undo, Escape out of a panel, the stacked layout
on a phone, and the Questions list printed with "--" for an empty field. A second journey
names the columns of a file the reader does not know, matches its line to the book entry
Suggest finds for it and takes the match back with Undo."""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_CSV = (b"Date,Description,Amount\n"
        b"2026-01-05,NETFLIX.COM,-15.99\n2026-01-06,NETFLIX.COM,-15.99\n2026-01-07,NETFLIX.COM,-15.99\n"
        b"2026-01-09,WITHDRAWAL,-200.00\n2026-01-12,MIXED PURCHASE,-100.00\n")


def _new_bank(api) -> dict:
    r = api.post("/accounting/bank-accounts", json={
        "bank_name": f"Journey {uuid.uuid4().hex[:6]}", "account_number": "****4242", "bank_type": "checking",
        "currency": api.get("/companies/me").json().get("currency") or "USD", "opening_balance": 0})
    assert r.status_code == 200, r.text
    return api.get("/accounting/bank-accounts").json()["items"][-1]


def _bank(api) -> str:
    bank_id = _new_bank(api)["id"]
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
    return bank_id


def _header(page: Page, preview, values: tuple[str, str, str, str]) -> None:
    """Type the opening and closing balance and the period into the preview's header."""
    for name, value in zip(("opening_balance", "closing_balance", "period_start", "period_end"), values):
        cell = preview.locator(f"[data-field={name}]")
        cell.click()
        cell.locator("input").fill(value)
        with page.expect_response(lambda r: "/preview/" in r.url and r.request.method == "POST"):
            cell.locator("input").press("Enter")


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
    _header(page, preview, ("0.00", "-347.97", "2026-01-01", "2026-01-31"))
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
    # With nothing ticked the bulk bar goes away; ticking brings it back.
    for i in (0, 1):
        lines.nth(i).locator("input[type=checkbox]").uncheck()
    expect(page.locator(".bulk-toolbar")).to_be_hidden()
    for i in (0, 1):
        lines.nth(i).locator("input[type=checkbox]").check()
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
    # Nothing is ticked after the post, yet the bar stays up while it carries the outcome and its Undo.
    expect(page.locator(".bulk-toolbar[data-testid=outcome]")).to_be_visible()
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
    row = page.locator(".bank-group .bank-row--group").first
    assert row.evaluate("e => getComputedStyle(e).display") == "block"
    page.set_viewport_size({"width": 1280, "height": 900})

    # Questions print with "--" for an empty field.
    page.goto(f"/accounting/reconcile/bank/{bank_id}?view=questions")
    page.emulate_media(media="print")
    expect(page.locator("[data-line]").first).to_contain_text("--")
    page.emulate_media(media="screen")


def test_bank_page_match_and_undo(page: Page, api, tmp_path):
    bank = _new_bank(api)
    r = api.post("/accounting/journal-entries", json={
        "ts": "2026-03-01", "memo": "Wire to ACME supplier", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": bank["chart_account_code"], "debit": 0, "credit": 5000.0},
                    {"account": "5100", "debit": 5000.0, "credit": 0}]})
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]

    # A file whose columns the reader does not know: name them, then the header figures.
    f = tmp_path / "march.csv"
    f.write_bytes(b"Col A,Col B,Col C\n2026-03-02,Wire ACME,-5000.00\n")
    page.goto(f"/accounting/reconcile/bank/{bank['id']}")
    page.locator('input[type="file"]').set_input_files(str(f))
    mapper = page.locator("[data-testid=column-mapper]")
    expect(mapper).to_be_visible(timeout=15000)
    for column, field in (("Col A", "date"), ("Col B", "description"), ("Col C", "amount")):
        mapper.locator(f'select[name="map_{column}"]').select_option(field)
    mapper.get_by_role("button", name="Confirm Mapping").click()
    preview = page.locator("[data-testid=statement-preview]")
    expect(preview.get_by_text("Wire ACME")).to_be_visible()
    _header(page, preview, ("0.00", "-5000.00", "2026-03-01", "2026-03-31"))
    preview.get_by_role("button", name="Import").click()
    page.get_by_role("button", name="Confirm").click()
    expect(page.locator("[data-group]").filter(has_text="Wire ACME")).to_be_visible()

    # Suggest finds the book entry by amount and date, which is no proof the two are one
    # payment, so it is offered unticked; ticking it and Post ticked matches the line to it.
    page.goto(f"/accounting/reconcile/bank/{bank['id']}?view=all")
    page.get_by_role("button", name="Suggest").click()
    group = page.locator("[data-group]").filter(has_text="Wire ACME")
    tick = group.locator("input[type=checkbox][data-role=group]")
    expect(group).to_contain_text("A book entry of the same amount on 2026-03-01")
    expect(tick).not_to_be_checked()
    tick.check()
    page.get_by_role("button", name="Post ticked").click()
    page.get_by_role("button", name="Confirm").click()
    expect(page.locator(".bulk-toolbar, [data-testid=outcome]")).to_contain_text("Posted 1")
    page.goto(f"/accounting/reconcile/bank/{bank['id']}?view=posted")
    line = page.locator("[data-line]").filter(has_text="Wire ACME")
    expect(line).to_contain_text("Matched")
    table = api.get(f"/accounting/reconciliation/bank/{bank['id']}/groups", params={"view": "all"}).json()
    assert [l["matched_je_ids"] for g in table["groups"] for l in g["lines"]] == [[je_id]]

    # Undo frees the entry: the line is open again and Suggest offers the entry once more.
    line.get_by_role("button", name="Undo").click()
    page.get_by_role("button", name="Confirm").click()
    expect(line).to_contain_text("Open")
    page.goto(f"/accounting/reconcile/bank/{bank['id']}?view=all")
    page.get_by_role("button", name="Suggest").click()
    expect(page.locator("[data-group]").filter(has_text="Wire ACME")).to_contain_text("A book entry of the same amount on 2026-03-01")
