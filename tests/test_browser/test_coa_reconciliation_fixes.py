# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Chart-of-accounts add/edit and reconciliation CSV import, end to end:
accounts can be added, edited, and deactivated without corrupting posted
history; statement CSVs import via HX-Redirect (no nested document swap),
round-trip through the inline column mapper, and re-import without
double-counting lines."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

OUT = Path("context/reviews/coa-reconciliation")

AUTO_CSV = (
    b"Date,Description,Amount,Balance\n"
    b"2026-03-01,Wire ACME,-5000,95000\n"
    b"2026-03-05,Deposit,10000,105000\n"
)
AMBIGUOUS_CSV = b"Col A,Col B,Col C\n2026-03-01,Something,1000\n"


def _no_crash(page, ctx=""):
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: 500"
    assert "Traceback (most recent call last)" not in body, f"{ctx}: traceback"


def _shot(page, name):
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


@pytest.fixture(scope="module")
def seeded_chart(api):
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), r.text


def _upload_csv(page, csv_bytes, filename):
    page.set_input_files(
        'input[name="csv_file"]',
        files=[{"name": filename, "mimeType": "text/csv", "buffer": csv_bytes}],
    )


def test_chart_of_accounts_add_edit_deactivate(page, ui_server, api, seeded_chart):
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Add: the tab's Add Account button leads to a working form.
    page.goto(f"{ui_server}/settings/accounting?tab=chart", wait_until="domcontentloaded")
    _no_crash(page, "chart-tab")
    page.click('a[href="/settings/accounting/chart/new"]')
    page.wait_for_url("**/settings/accounting/chart/new")
    _no_crash(page, "add-account-form")
    page.fill('input[name="code"]', "1999")
    page.fill('input[name="name"]', "Test Clearing")
    page.select_option('select[name="account_type"]', "asset")
    page.click('button[type="submit"]')
    page.wait_for_url("**/settings/accounting?tab=chart")
    _no_crash(page, "chart-after-add")
    row = page.locator('tr:has(a[href="/settings/accounting/chart/1999/edit"])')
    assert row.count() == 1
    assert "Test Clearing" in row.inner_text()
    _shot(page, "01-chart-account-added")

    # Edit: rename in place.
    page.click('a[href="/settings/accounting/chart/1999/edit"]')
    page.wait_for_url("**/settings/accounting/chart/1999/edit")
    _no_crash(page, "edit-account-form")
    page.fill('input[name="name"]', "Test Clearing Renamed")
    page.click('button[type="submit"]')
    page.wait_for_url("**/settings/accounting?tab=chart")
    row = page.locator('tr:has(a[href="/settings/accounting/chart/1999/edit"])')
    assert "Test Clearing Renamed" in row.inner_text()
    _shot(page, "02-chart-account-renamed")

    # Duplicate code: the 409 renders inline and no second 1999 row appears.
    page.goto(f"{ui_server}/settings/accounting/chart/new", wait_until="domcontentloaded")
    page.fill('input[name="code"]', "1999")
    page.fill('input[name="name"]', "Duplicate Attempt")
    page.click('button[type="submit"]')
    page.wait_for_selector("#account-form-error .error-banner")
    assert "1999" in page.locator("#account-form-error .error-banner").inner_text()
    assert "/settings/accounting/chart/new" in page.url
    _shot(page, "03-duplicate-code-inline-error")
    page.goto(f"{ui_server}/settings/accounting?tab=chart", wait_until="domcontentloaded")
    assert page.locator('a[href="/settings/accounting/chart/1999/edit"]').count() == 1

    # Post a journal entry crediting 1999, then deactivate it: the account
    # must stay visible (Inactive badge), never disappear from the chart.
    je = api.post("/accounting/journal-entries", json={
        "ts": "2026-03-10", "memo": "Clearing test",
        "entries": [
            {"account": "1111", "debit": 50.0, "credit": 0},
            {"account": "1999", "debit": 0, "credit": 50.0},
        ],
        "idempotency_token": uuid.uuid4().hex,
    })
    assert je.status_code == 200, je.text

    page.goto(f"{ui_server}/settings/accounting/chart/1999/edit", wait_until="domcontentloaded")
    page.select_option('select[name="is_active"]', "false")
    page.click('button[type="submit"]')
    page.wait_for_url("**/settings/accounting?tab=chart")
    _no_crash(page, "chart-after-deactivate")
    row = page.locator('tr:has(a[href="/settings/accounting/chart/1999/edit"])')
    assert row.count() == 1, "deactivated account must remain listed"
    assert "inactive" in row.inner_text().lower()
    _shot(page, "04-account-deactivated-still-listed")


@pytest.fixture(scope="module")
def bank_account(api, seeded_chart):
    r = api.post("/accounting/bank-accounts", json={
        "bank_name": "E2E Bank",
        "account_number": "****1234",
        "bank_type": "checking",
        "currency": "THB",
        "opening_balance": 100000.0,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _start_session(api, bank_id):
    r = api.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank_id,
        "statement_date": "2026-03-31",
        "statement_balance": 105000.0,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_reconciliation_import_and_mapping(page, ui_server, api, bank_account):
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Money path: auto-detect CSV import answers 204 + HX-Redirect, and the
    # workspace never contains a nested document.
    sid = _start_session(api, bank_account["id"])
    page.goto(f"{ui_server}/accounting/reconcile/{sid}", wait_until="domcontentloaded")
    _no_crash(page, "workspace-empty")
    _upload_csv(page, AUTO_CSV, "statement.csv")
    with page.expect_response(f"**/accounting/reconcile/{sid}/import") as resp_info:
        page.click('button:has-text("Import CSV")')
    resp = resp_info.value
    assert resp.status == 204, f"import returned {resp.status}, expected 204"
    assert resp.headers.get("hx-redirect"), "import response missing HX-Redirect header"

    page.wait_for_url(f"**/accounting/reconcile/{sid}")
    page.wait_for_selector("#stmt-lines-panel")
    _no_crash(page, "workspace-imported")
    assert page.locator("#stmt-lines-panel").count() == 1
    body = page.locator("body").inner_text()
    assert "<html" not in body, "workspace contains a nested document"
    assert "5,000.00" in body and "10,000.00" in body
    _shot(page, "05-import-workspace-two-lines")

    # Column-mapping path: ambiguous CSV renders the inline mapper (200
    # fragment, same page), and the mapped line round-trips into the workspace.
    sid2 = _start_session(api, bank_account["id"])
    page.goto(f"{ui_server}/accounting/reconcile/{sid2}", wait_until="domcontentloaded")
    _upload_csv(page, AMBIGUOUS_CSV, "ambiguous.csv")
    with page.expect_response(f"**/accounting/reconcile/{sid2}/import") as resp_info:
        page.click('button:has-text("Import CSV")')
    assert resp_info.value.status == 200, "ambiguous CSV should render the inline mapper"
    page.wait_for_selector('#recon-workspace select[name="map_Col A"]')
    assert f"/accounting/reconcile/{sid2}" in page.url, "mapper must render on the same page"
    assert page.locator("#recon-workspace select").count() == 3
    _shot(page, "06-inline-column-mapper")

    page.select_option('select[name="map_Col A"]', "date")
    page.select_option('select[name="map_Col B"]', "description")
    page.select_option('select[name="map_Col C"]', "amount")
    with page.expect_response(f"**/accounting/reconcile/{sid2}/confirm-import") as resp_info:
        page.click('button:has-text("Confirm Mapping")')
    resp = resp_info.value
    assert resp.status == 204, f"confirm-import returned {resp.status}"
    assert resp.headers.get("hx-redirect")
    page.wait_for_url(f"**/accounting/reconcile/{sid2}")
    page.wait_for_selector("#stmt-lines-panel")
    _no_crash(page, "workspace-mapped")
    body = page.locator("body").inner_text()
    assert "1,000.00" in body, "mapped CSV line did not reach the workspace"
    assert "Something" in body
    _shot(page, "07-mapped-line-in-workspace")

    # Re-import idempotency: importing the same statement again replaces the
    # lines instead of appending. The workspace only offers the upload form
    # while empty, so the re-import exercises the same backend endpoint the
    # UI submits to.
    r = api.post(
        f"/accounting/reconciliation/{sid}/import-csv",
        files={"file": ("statement.csv", AUTO_CSV, "text/csv")},
    )
    assert r.status_code == 200, r.text
    page.goto(f"{ui_server}/accounting/reconcile/{sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#stmt-lines-panel")
    _no_crash(page, "workspace-reimported")
    assert page.locator("#stmt-lines-panel > .recon-row").count() == 2, \
        "re-import must replace, not append, statement lines"
    _shot(page, "08-reimport-still-two-lines")


def _post_bank_je(api, chart_code, memo, amount, ts="2026-03-01"):
    """Post a JE moving `amount` through the bank ledger account (negative = out)."""
    debit, credit = (0.0, -amount) if amount < 0 else (amount, 0.0)
    r = api.post("/accounting/journal-entries", json={
        "ts": ts, "memo": memo,
        "entries": [
            {"account": "1111", "debit": credit, "credit": debit},
            {"account": chart_code, "debit": debit, "credit": credit},
        ],
        "idempotency_token": uuid.uuid4().hex,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_select_to_match_end_to_end(page, ui_server, api, bank_account):
    """Xero-style manual match: select a statement line, click a book entry.

    Covers the full loop in the running UI: armed styling, the no-selection
    toast, the match itself, the book panel and stats refreshing without a
    reload, and Undo returning the freed entry to the panel."""
    page.set_viewport_size({"width": 1440, "height": 1000})
    _post_bank_je(api, bank_account["chart_account_code"], "Wire to ACME supplier", -5000.0)

    sid = _start_session(api, bank_account["id"])
    page.goto(f"{ui_server}/accounting/reconcile/{sid}", wait_until="domcontentloaded")
    _upload_csv(page, AUTO_CSV, "statement.csv")
    page.click('button:has-text("Import CSV")')
    page.wait_for_selector("#stmt-lines-panel")
    _no_crash(page, "select-match-workspace")

    entry = page.locator(".recon-book-entry", has_text="Wire to ACME supplier")
    assert entry.count() == 1, "seeded ledger entry must appear in the book panel"

    # Clicking an entry with nothing selected explains itself instead of failing.
    entry.click()
    toast = page.wait_for_selector(".toast")
    assert "Select a bank statement line" in toast.inner_text()
    page.click(".toast__close")
    page.wait_for_selector(".toast", state="detached")

    # Select the statement line: row highlights and the book panel arms.
    line = page.locator(".recon-row--selectable", has_text="Wire ACME")
    line.click()
    page.wait_for_selector(".recon-row--active")
    _shot(page, "09-line-selected-book-panel-armed")

    # The filter narrows the panel without touching the server.
    page.fill(".recon-book-filter", "zzz")
    assert entry.first.is_hidden()
    page.fill(".recon-book-filter", "acme")
    assert entry.first.is_visible()

    # Click the entry: the whole workspace refreshes in one swap.
    entry.click()
    page.wait_for_selector(".recon-stat--matched:has-text('Matched: 1')")
    _no_crash(page, "after-match")
    assert entry.count() == 0, \
        "a matched entry must leave the book panel without a reload"
    _shot(page, "10-line-matched-entry-left-panel")

    # Undo: the freed entry returns to the book panel, again without a reload.
    page.locator(".recon-row", has_text="Wire ACME").locator('button:has-text("Undo")').click()
    page.wait_for_selector('.recon-book-entry:has-text("Wire to ACME supplier")')
    assert page.locator(".recon-book-entry", has_text="Wire to ACME supplier").count() == 1
    page.wait_for_selector(".recon-stat--matched:has-text('Matched: 0')")
    _no_crash(page, "after-undo")
    _shot(page, "11-undo-entry-restored-to-panel")


def test_match_endpoint_validates_book_entry(api, bank_account):
    """Function-level validation on manual match: unknown entries are rejected,
    an entry cannot be claimed by two statement lines, and undo releases it."""
    amount = -1234.56
    _post_bank_je(api, bank_account["chart_account_code"], "Validation wire", amount,
                  ts="2026-03-02")
    sid = _start_session(api, bank_account["id"])
    r = api.post(f"/accounting/reconciliation/{sid}/import-csv",
                 files={"file": ("statement.csv", AUTO_CSV, "text/csv")})
    assert r.status_code == 200, r.text
    lines = api.get(f"/accounting/reconciliation/{sid}/statement-lines").json()["items"]
    assert len(lines) == 2
    entries = api.get(f"/accounting/reconciliation/{sid}").json()["unreconciled_entries"]
    je_id = next(e["je_id"] for e in entries if e["amount"] == amount)

    r = api.post(f"/accounting/reconciliation/{sid}/lines/{lines[0]['id']}/match",
                 json={"je_id": "je:does-not-exist"})
    assert r.status_code == 400, r.text

    r = api.post(f"/accounting/reconciliation/{sid}/lines/{lines[0]['id']}/match",
                 json={"je_id": je_id})
    assert r.status_code == 200, r.text

    r = api.post(f"/accounting/reconciliation/{sid}/lines/{lines[1]['id']}/match",
                 json={"je_id": je_id})
    assert r.status_code == 400, r.text
    assert "already reconciled" in r.json()["detail"]

    # Re-matching the same line to its own entry stays idempotent.
    r = api.post(f"/accounting/reconciliation/{sid}/lines/{lines[0]['id']}/match",
                 json={"je_id": je_id})
    assert r.status_code == 200, r.text

    # Undo releases the entry for the other line.
    r = api.post(f"/accounting/reconciliation/{sid}/lines/{lines[0]['id']}/unmatch")
    assert r.status_code == 200, r.text
    r = api.post(f"/accounting/reconciliation/{sid}/lines/{lines[1]['id']}/match",
                 json={"je_id": je_id})
    assert r.status_code == 200, r.text
