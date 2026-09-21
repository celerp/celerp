# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Browser journeys for the two document flows the assistant page showcases.

Receipts: photos go to a reading job, the thread shows the job's progress, and
the finished job offers one card per proposed vendor and bill; Confirm all
writes them. Statements: a CSV runs the agent inline, and each step of the
reconciliation (start, import, auto-match, post a missing entry, complete) is
a card the user confirms. The model is scripted the same way as
`test_ai_chat_journey.py`; only the reading job's `call_llm` is faked here,
every write still lands through the real routes.
"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
from playwright.sync_api import Page, expect

from celerp.ai.llm import ModelResult

from .test_ai_chat_journey import (  # noqa: F401  (fixtures by name)
    _SCRIPT, _answer, _open_chat, _send, _tool_call, agent_caps, agent_env, item_create_cap,
)

pytestmark = pytest.mark.browser

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

_RECEIPTS = {
    "northwind.png": {
        "document_kind": "receipt", "vendor_name": "Northwind Paper Co", "date": "2026-09-14",
        "currency": "THB", "total": 27.5, "tax": 2.5, "reference": "R-1001",
        "line_items": [
            {"description": "Paper A4", "quantity": 2, "unit_price": 10},
            {"description": "Stapler", "quantity": 1, "unit_price": 5},
        ],
    },
    "quickparts.png": {
        "document_kind": "receipt", "vendor_name": "Quick Parts Ltd", "date": "2026-09-15",
        "currency": "THB", "total": 20.0, "tax": 1.5, "reference": "QP-77",
        "line_items": [
            {"description": "Bolt M6", "quantity": 1, "unit_price": 10},
            {"description": "Washer", "quantity": 1, "unit_price": 5},
        ],
    },
}


def _bulk_receipt(n: int, vendor: str, *, short: bool = False) -> dict:
    """A receipt whose lines and tax reach its total, or fall short when asked."""
    return {
        "document_kind": "receipt", "vendor_name": vendor, "date": f"2026-09-1{n}",
        "currency": "THB", "total": 13.0 if short else 11.0, "tax": 1.0, "reference": f"B-{n}",
        "line_items": [{"description": "Tape", "quantity": 1, "unit_price": 10}],
    }


_BULK_RECEIPTS = {
    "bulk-a.png": _bulk_receipt(1, "Alder Supplies"),
    "bulk-b.png": _bulk_receipt(2, "Birch Traders", short=True),
    "bulk-c.png": _bulk_receipt(3, "Cedar Goods"),
}


async def _fake_read(model, system, prompt, *, files=None, **_kw) -> ModelResult:
    """The reading model: answers with the receipt JSON for the attached file."""
    extraction = {**_RECEIPTS, **_BULK_RECEIPTS}[files[0]["filename"]]
    return ModelResult(
        message={"role": "assistant", "content": f"```json\n{json.dumps(extraction)}\n```"},
        model_used="fake-reader", usage={"credits": 1}, reservation_id=None, remaining=None,
    )


def _attach(page: Page, files: list[dict]) -> list[str]:
    """Attach files through the page's own upload path; returns their upload ids."""
    page.locator("#ai-file-input").set_input_files(files)
    chips = page.locator(".ai-file-chip")
    expect(chips).to_have_count(len(files))
    receipt_button = page.locator(".ai-input__receipts")
    receipt_eligible = all(
        f.get("mimeType") in {"image/jpeg", "image/png", "image/webp", "application/pdf"}
        for f in files
    )
    if receipt_eligible:
        expect(receipt_button).to_be_enabled()
    else:
        expect(receipt_button).to_be_disabled()
    return [chips.nth(i).get_attribute("data-file-id") for i in range(len(files))]


def _confirm_card(page: Page, text: str) -> None:
    """Confirm the one open card mentioning ``text`` and wait for its done line."""
    card = page.locator(".ai-action__card", has_text=text)
    expect(card).to_be_visible()
    done_before = page.locator(".ai-action__done").count()
    card.get_by_role("button", name="Confirm").click()
    expect(page.locator(".ai-action__done")).to_have_count(done_before + 1)
    expect(page.locator(".ai-action__error")).to_have_count(0)


def _send_receipts(page: Page, text: str) -> None:
    page.locator("#ai-query-input").fill(text)
    page.locator(".ai-input__receipts").click()


def _call(cap: dict, path: dict | None = None, body: dict | None = None, content: str = "") -> ModelResult:
    arguments: dict = {}
    if path:
        arguments["path"] = path
    if cap["expects_body"]:
        arguments["body"] = body or {}
    return _tool_call(cap["name"], arguments, content)


# ── Receipts to bills ─────────────────────────────────────────────────────────

def test_receipts_to_bills_journey(page: Page, ui_server, api):
    with patch("celerp.ai.batch.call_llm", _fake_read):
        _open_chat(page, ui_server)
        _attach(page, [
            {"name": name, "mimeType": "image/png", "buffer": _PNG} for name in _RECEIPTS
        ])
        _send_receipts(page, "Enter these as bills")

        job = page.locator(".ai-job")
        expect(job).to_be_visible()
        expect(page.locator(".ai-job--done")).to_be_visible(timeout=20_000)
        expect(page.locator(".ai-job--done")).to_contain_text("2 of 2 files were read.")

    cards = page.locator(".ai-action__card")
    expect(cards).to_have_count(4)
    titles = [cards.nth(i).locator(".ai-action__title").inner_text() for i in range(4)]
    assert sorted(titles) == sorted([
        "Create vendor Northwind Paper Co", "Create bill from Northwind Paper Co",
        "Create vendor Quick Parts Ltd", "Create bill from Quick Parts Ltd",
    ])

    # The receipt whose lines and tax do not reach its total carries the check.
    flagged = page.locator(".ai-action__card", has_text="Create bill from Quick Parts Ltd")
    expect(flagged.locator(".ai-action__warnings")).to_contain_text(
        "The lines and tax add up to 16.50 but the receipt total is 20.00."
    )
    clean = page.locator(".ai-action__card", has_text="Create bill from Northwind Paper Co")
    expect(clean.locator(".ai-action__warnings")).to_have_count(0)

    page.locator(".ai-action-group__footer button[type=submit]").click()
    expect(page.locator(".ai-action-group__summary")).to_contain_text("4 applied, 0 failed.")

    bills = api.get("/docs", params={"doc_type": "bill", "q": "Northwind"}).json()["items"]
    assert [b["contact_name"] for b in bills] == ["Northwind Paper Co"]
    assert bills[0]["status"] == "draft"
    vendors = api.get("/crm/contacts", params={"q": "Quick Parts Ltd"}).json()["items"]
    assert [v["name"] for v in vendors] == ["Quick Parts Ltd"]


def test_receipts_bulk_table_journey(page: Page, ui_server, api):
    """Past five proposals the thread shows the review table: the flagged bill
    starts unticked, Confirm selected from the bulk toolbar applies the rest,
    and the tally links to exactly the drafts this batch created."""
    with patch("celerp.ai.batch.call_llm", _fake_read):
        _open_chat(page, ui_server)
        _attach(page, [
            {"name": name, "mimeType": "image/png", "buffer": _PNG} for name in _BULK_RECEIPTS
        ])
        _send_receipts(page, "Enter these as bills")
        expect(page.locator(".ai-job--done")).to_be_visible(timeout=20_000)
        expect(page.locator(".ai-job--done")).to_contain_text("3 of 3 files were read.")

    table = page.locator(".ai-action-table")
    expect(table).to_be_visible()
    rows = table.locator("tbody tr.data-row")
    expect(rows).to_have_count(6)
    expect(page.locator(".ai-action__card")).to_have_count(0)

    flagged = rows.filter(has_text="Create bill from Birch Traders")
    expect(flagged.locator(".ai-action-table__checks")).to_contain_text(
        "The lines and tax add up to 11.00 but the receipt total is 13.00."
    )
    expect(flagged.locator(".bulk-select")).not_to_be_checked()
    expect(rows.filter(has_text="Create bill from Alder Supplies").locator(".bulk-select")).to_be_checked()

    # A row expands to the card's detail without leaving the thread.
    rows.filter(has_text="Create bill from Cedar Goods").locator(".ai-action-table__toggle").click()
    expect(table.locator(".ai-action-table__details.is-open")).to_contain_text("Cedar Goods")

    toolbar = page.locator(".bulkbar")
    expect(toolbar).to_be_visible()
    expect(toolbar.locator(".bulk-count")).to_contain_text("5")
    toolbar.locator(".bulk-action-select").select_option("confirm")
    expect(page.locator(".ai-action-group__summary")).to_contain_text("5 applied, 0 failed.", timeout=20_000)
    expect(table.locator(".bulk-select")).to_have_count(1)
    expect(rows.filter(has_text="Create bill from Alder Supplies").locator(".table-link")).to_have_attribute(
        "href", re.compile(r"^/docs/")
    )

    drafts = page.locator(".ai-action-group__footer a", has_text="Open these 2 drafts")
    expect(drafts).to_be_visible()
    drafts.click()
    expect(page).to_have_url(re.compile(r"/docs\?view=drafts&ids="))
    listed = page.locator(".data-table tbody tr.data-row")
    expect(listed).to_have_count(2)
    expect(page.locator(".data-table")).to_contain_text("Alder Supplies")
    expect(page.locator(".data-table")).to_contain_text("Cedar Goods")
    expect(page.locator(".data-table")).not_to_contain_text("Birch Traders")


# ── Statement reconciliation ──────────────────────────────────────────────────

_STATEMENT = b"Date,Description,Amount\n2026-03-01,Bank fee,-500\n"


def test_statement_reconcile_journey(page: Page, ui_server, api, agent_caps):
    recon = "/accounting/reconciliation/{session_id}"
    start_cap = agent_caps[("POST", "/accounting/reconciliation/start")]
    import_cap = agent_caps[("POST", f"{recon}/import-file")]
    match_cap = agent_caps[("POST", f"{recon}/auto-match")]
    workbench_cap = agent_caps[("GET", f"{recon}/workbench")]
    create_cap = agent_caps[("POST", f"{recon}/lines/{{line_id}}/create")]
    complete_cap = agent_caps[("POST", f"{recon}/complete")]

    bank = api.post("/accounting/bank-accounts", json={
        "bank_name": "Journey Bank", "account_number": "****4242", "bank_type": "checking",
        "currency": "THB", "opening_balance": 100000.0,
    }).json()
    start_body = {"bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 99500.0}

    _open_chat(page, ui_server)
    file_id = _attach(page, [{"name": "march.csv", "mimeType": "text/csv", "buffer": _STATEMENT}])[0]

    _SCRIPT.append(_call(start_cap, body=start_body,
                         content="I'll open the March reconciliation for Journey Bank once you confirm."))
    _send(page, "Reconcile this statement against our books")
    _confirm_card(page, bank["id"])

    # Starting again for the same statement returns the open session, so the
    # test learns the id the same way the assistant does.
    sid = api.post("/accounting/reconciliation/start", json=start_body).json()["id"]

    _SCRIPT.append(_call(import_cap, path={"session_id": sid}, body={"file_id": file_id},
                         content="Next I'll import the statement lines from march.csv."))
    _send(page, "Import the statement")
    _confirm_card(page, file_id)
    lines = api.get(f"/accounting/reconciliation/{sid}/statement-lines").json()["items"]
    assert [(l["description"], l["amount"]) for l in lines] == [("Bank fee", -500.0)]

    _SCRIPT.append(_call(match_cap, path={"session_id": sid},
                         content="I'll match the lines against the recorded entries."))
    _send(page, "Match them")
    _confirm_card(page, sid)

    # A read runs on its own and the model answers from the workbench it got back.
    _SCRIPT.append(_call(workbench_cap, path={"session_id": sid}))
    _SCRIPT.append(_answer("One line has no book entry: Bank fee, -500.00 on 2026-03-01. "
                           "Say which expense account it belongs to and I will propose the entry."))
    _send(page, "What is still open?")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("One line has no book entry")

    _SCRIPT.append(_call(create_cap, path={"session_id": sid, "line_id": lines[0]["id"]},
                         body={"account_code": "6100", "memo": "Bank fee"},
                         content="I'll post the bank fee to 6100 once you confirm."))
    _send(page, "Post it to 6100")
    _confirm_card(page, "6100")
    workbench = api.get(f"/accounting/reconciliation/{sid}/workbench").json()
    assert workbench["statement_lines"] == []
    assert abs(workbench["difference"]) < 0.01

    _SCRIPT.append(_call(complete_cap, path={"session_id": sid},
                         content="The difference is zero. Confirm to close the reconciliation."))
    _send(page, "Close it")
    _confirm_card(page, sid)
    assert api.get(f"/accounting/reconciliation/{sid}").json()["status"] == "completed"
