# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Browser journeys for the two document flows the assistant page showcases.

Receipts: photos go to a reading job, the thread shows the job's progress, and
the finished job offers one card per proposed vendor and bill; Confirm all
writes them. Statements: a CSV runs the agent inline, and each step of the
bank workflow (read the file, import the statement, post a missing entry,
complete) is a card the user confirms. The model is scripted the same way as
`test_ai_chat_journey.py`; only the reading job's model call is faked here,
every write still lands through the real routes.
"""
from __future__ import annotations

import contextvars
import json
import re
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from playwright.sync_api import Page, expect

from celerp.ai import batch
from celerp.ai.files import load_file_for_llm
from celerp.ai.llm import ModelResult
from ui.i18n import t

from .test_ai_chat_journey import (  # noqa: F401  (fixtures by name)
    _SCRIPT, _answer, _open_chat, _send, _tool_call, agent_caps, agent_env, item_create_cap,
)

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("tools_open_when_called")]

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


_reading: contextvars.ContextVar[str] = contextvars.ContextVar("reading")


async def _fake_complete(messages, *, before_send=None, **_kw) -> ModelResult:
    """The reading model: answers with the receipt JSON for the file being read."""
    await before_send(len(json.dumps(messages)))
    extraction = {**_RECEIPTS, **_BULK_RECEIPTS}[_reading.get()]
    return ModelResult(
        message={"role": "assistant", "content": f"```json\n{json.dumps(extraction)}\n```"},
        model_used="fake-reader", usage={"credits": 1}, reservation_id=None, remaining=None,
    )


_real_read_file = batch.read_file


async def _read_file(run, file_id, operation_id, **kw):
    """Each file's step runs in a task of its own: the model call reads its name here."""
    _reading.set(load_file_for_llm(file_id, run.company_id, run.user_id)["filename"])
    return await _real_read_file(run, file_id, operation_id, **kw)


@contextmanager
def _fake_read():
    with patch.object(batch, "read_file", _read_file), patch("celerp.ai.llm.complete", _fake_complete):
        yield


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
    # The confirmed write hands back to the assistant; its reply lands before the next turn.
    expect(page.locator(".ai-msg--pending")).to_have_count(0, timeout=15000)


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
    with _fake_read():
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
    expect(page.locator(".ai-action-group__summary")).to_contain_text(t("ai.confirm_all_done", "en", completed=4))

    bills = api.get("/docs", params={"doc_type": "bill", "q": "Northwind"}).json()["items"]
    assert [b["contact_name"] for b in bills] == ["Northwind Paper Co"]
    assert bills[0]["status"] == "draft"
    vendors = api.get("/crm/contacts", params={"q": "Quick Parts Ltd"}).json()["items"]
    assert [v["name"] for v in vendors] == ["Quick Parts Ltd"]


def test_receipts_bulk_table_journey(page: Page, ui_server, api):
    """Past five proposals the thread shows the review table: the flagged bill
    starts unticked, Confirm selected from the bulk toolbar applies the rest,
    and the tally links to exactly the drafts this batch created."""
    with _fake_read():
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
    expect(page.locator(".ai-action-group__summary")).to_contain_text(t("ai.confirm_all_done", "en", completed=5), timeout=20_000)
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


def _db_one(sql: str, *args) -> str:
    """The first column of the one row ``sql`` finds in the app's database."""
    from .conftest import _test_db
    conn = _test_db()
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    conn.close()
    assert row, sql
    return str(row[0])


def _reading_job(api, bank_id: str) -> str:
    """The reading job the assistant started for ``bank_id``: its id is derived from the
    request key the tool call fills in, so the test finds it by the bank it reads for."""
    return _db_one("SELECT entity_id FROM projections WHERE company_id = %s AND entity_id LIKE 'bankread:%%' "
                   "AND state::jsonb->>'bank_account_id' = %s ORDER BY created_at DESC LIMIT 1",
                   api.get("/companies/me").json()["id"], bank_id).split(":", 1)[1]


def _preview(api, job_id: str) -> dict:
    """The statement preview once the reading job has finished."""
    deadline = time.monotonic() + 30
    while True:
        r = api.get(f"/accounting/reconciliation/statement-preview/{job_id}")
        if r.status_code != 409 or time.monotonic() > deadline:
            assert r.status_code == 200, r.text
            return r.json()
        time.sleep(0.2)


def test_statement_reconcile_journey(page: Page, ui_server, api, agent_caps):
    api_root = "/accounting/reconciliation"
    read_cap = agent_caps[("POST", f"{api_root}/read-statement")]
    preview_cap = agent_caps[("GET", f"{api_root}/statement-preview/{{job_id}}")]
    import_cap = agent_caps[("POST", f"{api_root}/import-statement")]
    groups_cap = agent_caps[("GET", f"{api_root}/bank/{{bank_id}}/groups")]
    resolve_cap = agent_caps[("POST", f"{api_root}/bank/{{bank_id}}/resolve")]
    complete_cap = agent_caps[("POST", f"{api_root}/bank/{{bank_id}}/complete")]

    bank = api.post("/accounting/bank-accounts", json={
        "bank_name": "Journey Bank", "account_number": "****4242", "bank_type": "checking",
        "currency": "USD", "opening_balance": 100000.0,
    }).json()
    bank_id = bank["id"]

    _open_chat(page, ui_server)
    file_id = _attach(page, [{"name": "march.csv", "mimeType": "text/csv", "buffer": _STATEMENT}])[0]

    _SCRIPT.append(_call(read_cap, body={"file_ids": [file_id], "bank_id": bank_id},
                         content="I'll read march.csv for Journey Bank once you confirm."))
    _send(page, "Reconcile this statement against our books")
    # The card names the file by its name, never by its upload id.
    card = page.locator(".ai-action__card", has_text="march.csv")
    expect(card).to_be_visible()
    expect(card).not_to_contain_text(file_id)
    _confirm_card(page, "march.csv")
    job_id = _reading_job(api, bank_id)
    preview = _preview(api, job_id)
    found = preview["statements"][0]
    assert [(l["description"], float(l["amount"])) for l in found["lines"]] == [("Bank fee", -500.0)]

    # A read runs on its own and the model answers from the preview it got back.
    _SCRIPT.append(_call(preview_cap, path={"job_id": job_id}))
    _SCRIPT.append(_answer("The statement has one line: Bank fee, -500.00 on 2026-03-01. "
                           "Give me the opening and closing balances and I will import it."))
    _send(page, "What did it find?")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("The statement has one line")

    header = {**found["header"], "opening_balance": "100000.00", "closing_balance": "99500.00",
              "period_start": "2026-03-01", "period_end": "2026-03-31"}
    _SCRIPT.append(_call(import_cap, body={"job_id": job_id, "bank_id": bank_id,
                                           "statements": [{"header": header, "lines": found["lines"]}]},
                         content="I'll import the March statement onto Journey Bank once you confirm."))
    _send(page, "Opening 100,000, closing 99,500")
    # The card names the statement reading by its file and shows the balances, never the job id.
    card = page.locator(".ai-action__card", has_text="Closing balance: 99500.00")
    expect(card).to_contain_text("march.csv")
    expect(card).not_to_contain_text(job_id)
    _confirm_card(page, "Closing balance: 99500.00")

    lines = [l for g in api.get(f"{api_root}/bank/{bank_id}/groups").json()["groups"] for l in g["lines"]]
    assert [(l["description"], float(l["amount"])) for l in lines] == [("Bank fee", -500.0)]

    _SCRIPT.append(_call(groups_cap, path={"bank_id": bank_id}))
    _SCRIPT.append(_answer("One line has no book entry: Bank fee, -500.00. "
                           "Say which expense account it belongs to and I will propose the entry."))
    _send(page, "What is still open?")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("One line has no book entry")

    _SCRIPT.append(_call(resolve_cap, path={"bank_id": bank_id}, body={"decisions": [
        {"line_ids": [lines[0]["id"]], "kind": "post", "account_code": "6100"}]},
                         content="I'll post the bank fee to 6100 once you confirm."))
    _send(page, "Post it to 6100")
    _confirm_card(page, "6100")
    assert api.get(f"{api_root}/bank/{bank_id}/groups").json()["groups"] == []

    _SCRIPT.append(_call(complete_cap, path={"bank_id": bank_id},
                         content="Every line is settled. Confirm to complete the March statement."))
    _send(page, "Close it")
    _confirm_card(page, "Journey Bank")
    sid = _db_one("SELECT id FROM reconciliation_sessions WHERE bank_account_id = %s", bank_id)
    assert api.get(f"{api_root}/{sid}").json()["status"] == "completed"
