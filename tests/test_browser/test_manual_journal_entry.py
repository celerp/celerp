# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test: the manual journal entry journey end to end.

Proves:
1. The Journal tab renders with a New entry action.
2. The entry form's balance chip flips to Balanced as amounts are typed.
3. Posting lands back on the Journal tab with the entry visible.
4. A line posted with a party reaches that party's statement.
5. Voiding the entry marks it voided on the journal.
6. Foreign currency is collapsed out of the way until it is asked for.
7. Each line converts at its own rate, and an entry that does not balance in
   the company currency is refused rather than plugged.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def seeded_chart(api):
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), f"Chart seed failed: {r.text}"


@pytest.fixture(scope="module")
def party(api):
    """A contact for a hand-posted line to be attributed to."""
    r = api.post("/crm/contacts",
                 json={"name": "Browser Party Co", "contact_type": "customer"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _pick_account(page, idx: int, code: str) -> None:
    """Choose the account on journal line `idx`.

    Scoped to that line's own account cell rather than counting comboboxes down
    the page: every line carries a party picker as well, so an index into all of
    them lands on the wrong control.
    """
    cell = page.locator("#je-lines tr").nth(idx).locator("td").first
    box = cell.locator(".combobox-input")
    box.click()
    cell.locator(".combobox-list.open").wait_for(state="visible", timeout=3000)
    box.fill(code)
    opt = cell.locator(f'.combobox-list.open .combobox-option[data-value="{code}"]').first
    opt.wait_for(state="visible", timeout=3000)
    opt.click()


def _pick_party(page, idx: int, contact_id: str, name: str) -> None:
    """Choose the party on journal line `idx`, the cell next to the account."""
    cell = page.locator("#je-lines tr").nth(idx).locator("td").nth(1)
    box = cell.locator(".combobox-input")
    box.click()
    cell.locator(".combobox-list.open").wait_for(state="visible", timeout=3000)
    box.fill(name)
    opt = cell.locator(
        f'.combobox-list.open .combobox-option[data-value="{contact_id}"]').first
    opt.wait_for(state="visible", timeout=5000)
    opt.click()


def _pick_currency(page, idx: int, code: str) -> None:
    """Choose the currency on journal line `idx`, in the cell after the credit."""
    cell = page.locator("#je-lines tr").nth(idx).locator("td").nth(4)
    box = cell.locator(".combobox-input")
    box.click()
    cell.locator(".combobox-list.open").wait_for(state="visible", timeout=3000)
    box.fill(code)
    opt = cell.locator(f'.combobox-list.open .combobox-option[data-value="{code}"]').first
    opt.wait_for(state="visible", timeout=3000)
    opt.click()


def _clear_currency(page, idx: int) -> None:
    """Empty the currency on journal line `idx`, making it a company-currency line."""
    cell = page.locator("#je-lines tr").nth(idx).locator("td").nth(4)
    cell.locator(".combobox-input").fill("")
    page.fill(f'#je-lines [name="rate_{idx}"]', "")


def test_manual_je_post_and_void_journey(page, ui_server, seeded_chart, party):
    page.goto(f"{ui_server}/accounting/journal/new", wait_until="domcontentloaded")
    page.wait_for_selector("#je-lines", timeout=10000)

    # Foreign currency is a reveal, not a column set everybody carries: an
    # accountant who never posts in another currency sees the form they always
    # saw, and the currency and rate cells are not on it until they ask.
    assert page.locator("#je-lines tr").first.locator("td").nth(4).is_hidden()

    page.fill('input[name="ts"]', "2026-06-15")
    page.fill('input[name="memo"]', "Browser test adjustment")

    # Two rows render by default; fill account + amount on each. Options are
    # targeted by data-value: clicking .first races the filter re-render.
    # Receivables against sales, with the customer named on the receivable line,
    # which is the entry a party picker exists for.
    _pick_account(page, 0, "1120")
    _pick_party(page, 0, party, "Browser Party Co")
    page.fill('#je-lines [name="debit_0"]', "42.50")

    chip = page.locator("#je-balance-chip")
    assert "val-chip--alert" in (chip.get_attribute("class") or "")

    _pick_account(page, 1, "4100")
    page.fill('#je-lines [name="credit_1"]', "42.50")
    page.wait_for_function(
        "document.getElementById('je-balance-chip').className === 'val-chip'", timeout=3000)

    page.click('button[type="submit"]')
    page.wait_for_url("**/accounting?tab=journal*", timeout=10000)
    page.wait_for_selector("text=Browser test adjustment", timeout=10000)

    # The party chosen on the form is what puts the line on that customer's
    # statement; without it the amount sits in the control account and on
    # nobody's statement.
    page.goto(f"{ui_server}/reports/statement?from=2026-01-01&to=2026-12-31"
              f"&account=c:{party}", wait_until="domcontentloaded")
    page.wait_for_selector("text=Browser test adjustment", timeout=10000)

    page.goto(f"{ui_server}/accounting?tab=journal", wait_until="domcontentloaded")
    page.wait_for_selector("text=Browser test adjustment", timeout=10000)

    # Void: act on this entry's own row. The journal is newest-first and shared
    # with every other entry the suite has posted, and the void control is shown
    # on document-generated rows too so the server can explain the refusal, so
    # "the first Void on the page" is not necessarily this entry's.
    row = page.locator("tr", has_text="Browser test adjustment").first
    row.locator("details summary", has_text="Void").first.click()
    reason = row.locator('details[open] input[name="reason"]').first
    reason.fill("browser test cleanup")
    row.locator("details[open] button", has_text="Confirm").first.click()
    page.wait_for_selector("tr.payment-voided .badge--void", timeout=10000)


def test_manual_je_fx_journey_converts_per_line_and_refuses_to_plug(
    page, ui_server, seeded_chart
):
    """The foreign-currency journey.

    Each line converts at its own rate and shows what it will post before it
    posts. When the converted figures do not foot, nothing is written to make
    them foot: the gap is named, the entry is refused, and the difference line
    is the author's own to write.
    """
    page.goto(f"{ui_server}/accounting/journal/new", wait_until="domcontentloaded")
    page.wait_for_selector("#je-lines", timeout=10000)

    page.fill('input[name="ts"]', "2026-06-16")
    page.fill('input[name="memo"]', "Browser test foreign entry")

    # Opening the reveal is what puts the per-line currency and rate cells on
    # the form; this fixture's company is USD, so EUR is a foreign line.
    page.locator("details.je-fx-reveal summary").first.click()
    page.wait_for_selector("#je-lines tr td:nth-child(5):visible", timeout=3000)

    # Three foreign debits against one credit: they foot in EUR but not once
    # each line converts, which is exactly the case that used to be plugged.
    page.click('button:has-text("Add line")')
    page.click('button:has-text("Add line")')
    for idx, (code, amount) in enumerate(
        [("6100", "33.33"), ("6200", "33.33"), ("6300", "33.34")]
    ):
        _pick_account(page, idx, code)
        page.fill(f'#je-lines [name="debit_{idx}"]', amount)
    _pick_account(page, 3, "1111")
    page.fill('#je-lines [name="credit_3"]', "100.00")

    # One currency typed once: the rest of the lines pick it up, so an entry in
    # a single foreign currency is not four identical decisions.
    _pick_currency(page, 0, "EUR")
    page.fill('#je-lines [name="rate_0"]', "3.0025")
    page.wait_for_function(
        "document.querySelector('#je-lines [name=\"rate_3\"]').value === '3.0025'",
        timeout=3000,
    )

    # The book columns show what will actually be posted, before posting.
    page.wait_for_function(
        "Array.from(document.querySelectorAll('.je-base-debit, .je-base-credit'))"
        ".some(function(c) { return c.textContent.indexOf('100.07') !== -1; })",
        timeout=3000,
    )
    # GDR 2d: the gap is named where it can still be fixed, with the side that
    # is short and the amount, in the currency the books are kept in.
    note = page.locator("#je-imbalance-note")
    page.wait_for_function(
        "document.getElementById('je-imbalance-note').textContent.indexOf('0.01') !== -1",
        timeout=3000,
    )
    assert "Debit" in note.text_content()
    assert "6960" not in note.text_content(), "nothing names an account to post to"

    # Posting anyway is refused, and the refusal names the same gap. Nothing is
    # written to the books to make the entry foot.
    page.click('button[type="submit"]')
    page.wait_for_selector("text=out of balance in USD", timeout=10000)
    assert page.locator('#je-lines [name="debit_0"]').input_value() == "33.33", \
        "a refused entry comes back with what was typed, not an empty form"

    # The author writes the difference line themselves, in the company currency.
    page.click('button:has-text("Add line")')
    _pick_account(page, 4, "6960")
    _clear_currency(page, 4)
    page.fill('#je-lines [name="debit_4"]', "0.01")
    page.wait_for_function(
        "document.getElementById('je-balance-chip').className === 'val-chip'", timeout=3000)

    page.click('button[type="submit"]')
    page.wait_for_url("**/accounting?tab=journal*", timeout=10000)
    page.wait_for_selector("text=Browser test foreign entry", timeout=10000)
    # The typed foreign figure and the rate ride the line they were typed on.
    page.wait_for_selector("text=3.0025", timeout=10000)
