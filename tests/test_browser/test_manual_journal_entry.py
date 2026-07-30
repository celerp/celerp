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
8. The selection toolbar counts entries, not the postings under them.
9. Voiding a selection asks first, then voids every entry in it at once.
"""
from __future__ import annotations

import uuid

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


@pytest.fixture(scope="module")
def two_posted_entries(api, seeded_chart):
    """Two hand-posted entries, and the search term that isolates them.

    Posted through the API rather than the form: these tests are about selecting
    and voiding, and the form has its own journey above. Both carry a tag of
    their own in the memo, which the tests search on, so what the page holds is
    these two entries and nothing else the suite happened to post that day.
    """
    tag = f"bulkvoid{uuid.uuid4().hex[:8]}"
    ids = []
    for amount in ("11.11", "22.22"):
        r = api.post("/accounting/journal-entries", json={
            "ts": "2026-06-20", "memo": f"Browser bulk void {tag} {amount}",
            "entries": [{"account": "6200", "debit": float(amount), "credit": 0},
                        {"account": "1111", "debit": 0, "credit": float(amount)}],
            "idempotency_token": uuid.uuid4().hex})
        assert r.status_code == 200, r.text
        ids.append(r.json()["je_id"])
    return tag, ids


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

    # Void: tick this entry's own row and act through the selection toolbar. The
    # journal is newest-first and shared with every other entry the suite has
    # posted, so tick by this entry's memo rather than the first box on the page.
    row = page.locator("tr", has_text="Browser test adjustment").first
    row.locator(".bulk-select").check()
    bar = page.locator(".bulkbar").first
    bar.locator(".bulk-action-select").select_option("void")
    reason = bar.locator(".bulk-field--reason")
    reason.wait_for(state="visible", timeout=3000)
    reason.fill("browser test cleanup")
    # Confirm asks before it strikes: a mis-ticked box is caught here, not after.
    page.once("dialog", lambda d: d.accept())
    bar.locator(".bulk-apply").click()
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
    # A refusal re-renders the whole page, and the script that builds a new row is
    # parsed after the button that calls it, so the refusal text being on screen
    # does not yet mean the button does anything. Wait for the parse to finish.
    page.wait_for_load_state("domcontentloaded")
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


def test_journal_bulk_toolbar_counts_entries_not_postings(
    page, ui_server, two_posted_entries
):
    """An entry is one thing to void, however many postings it is made of.

    The journal shows an entry row and then a row per posting under it, so a
    toolbar counting rows would offer to void six things where there are two.
    """
    tag, _ = two_posted_entries
    page.goto(f"{ui_server}/accounting?from=2026-06-20&to=2026-06-20&q={tag}",
              wait_until="domcontentloaded")
    page.wait_for_selector("#journal-table", timeout=10000)

    boxes = page.locator("#journal-table .bulk-select")
    rows = page.locator("#journal-table tbody tr")
    assert boxes.count() == 2, f"Expected a box per entry, got {boxes.count()}"
    assert rows.count() > boxes.count(), (
        f"Expected postings to be rows too, got {rows.count()} rows for "
        f"{boxes.count()} entries"
    )

    bar = page.locator("#bulkbar-journal-table")
    # The action list is usable before anything is ticked so its options can be read (GDR 2e).
    assert not bar.locator(".bulk-action-select").is_disabled(), \
        "the action list should be usable with nothing selected"

    page.locator("#journal-table thead .bulk-select-all").click()
    assert bar.locator(".bulk-count").inner_text().strip() == "2 selected"
    assert not bar.locator(".bulk-action-select").is_disabled()


def test_journal_bulk_void_confirms_then_posts(page, ui_server, api, two_posted_entries):
    """Voiding a selection asks once, then voids the whole selection in place.

    A void cannot be undone, so the count is named before anything is written.
    Afterwards the page swaps the table it posted from rather than reloading, and
    says what happened.
    """
    tag, je_ids = two_posted_entries
    page.goto(f"{ui_server}/accounting?from=2026-06-20&to=2026-06-20&q={tag}",
              wait_until="domcontentloaded")
    page.wait_for_selector("#journal-table", timeout=10000)
    for je_id in je_ids:
        page.locator(f'#journal-table .bulk-select[value="{je_id}"]').check()

    bar = page.locator("#bulkbar-journal-table")

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

    # The optional reason stays hidden below the bar until void is chosen, then
    # appears with its own confirm rather than firing the instant void is picked.
    reason = bar.locator('.bulk-field[name="reason"]')
    assert not reason.is_visible(), "the reason field showed before void was chosen"
    bar.locator(".bulk-action-select").select_option("void")
    reason.wait_for(state="visible", timeout=3000)
    reason.fill("Duplicate batch")
    bar.locator(".bulk-apply").click()

    page.wait_for_selector(".toast-container .toast", timeout=10000)
    assert len(dialogs) == 1, f"Expected one confirm, got {dialogs}"
    assert "2" in dialogs[0], f"the confirm did not name the count: {dialogs[0]!r}"
    assert "2" in page.locator(".toast-container .toast").inner_text()
    assert "from=2026-06-20" in page.url, "the void reloaded the page instead of swapping"

    # Both entries are voided, with the reason typed in the bar, and both rows say
    # so where they stood.
    assert page.locator("#journal-table tr.payment-voided .badge--void").count() == 2
    entries = api.get("/accounting/journal",
                      params={"date_from": "2026-06-20", "date_to": "2026-06-20",
                              "q": tag}).json()
    voided = {e["je_id"]: e for e in entries["entries"]}
    assert sorted(voided) == sorted(je_ids), f"Expected both entries back, got {list(voided)}"
    for je_id in je_ids:
        assert voided[je_id]["status"] == "void", voided[je_id]
        assert voided[je_id].get("void_reason") == "Duplicate batch", voided[je_id]
