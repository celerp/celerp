# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every report end to end, and a golden of what each one renders.

The rest of the UI suite for these pages replaces the API client with mocks that
return their fixture whatever they are asked for. That proves a fixture renders; it
cannot prove the chain is wired. Nothing is mocked here except the transport: the
UI's HTTP client is pointed at the API application in-process, so each request goes
through the real route, the real query and the real projection before it becomes
HTML. One case per report, because it is the wiring being proven; the arithmetic
has its own tests next door.

The rendered HTML is also held against a golden file per report. A view builder
plumbed into the wrong caller still returns valid HTML and still produces identical
API JSON, so figures alone will not catch it, and nobody eyeballs six pages after
every refactor. The ids the database allocates are normalised out first, so what is
compared is the report and not the run. After a deliberate change to a report, run
with UPDATE_REPORT_GOLDENS=1, read the diff, and commit it with the change.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ui.routes.financial_reports import REPORTS, ledger_path, subject_token

GOLDEN_DIR = Path(__file__).parent / "report_goldens"

# The period every report is asked for, so no report falls back to a default that
# moves with the calendar and takes the golden with it.
PERIOD = [("from", "2026-01-01"), ("to", "2026-03-31")]
AS_OF = "2026-03-31"


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _seed(client) -> tuple[str, str]:
    """A small set of books with something in every report.

    An invoice part paid puts a customer on the statement and cash through the bank;
    a second invoice leaves a balance outstanding at the period end; the rent entry
    gives the P&L an expense; the manual entry against receivables carries a contact
    key, which is what keeps a hand-posted line attributed on the subledger.
    """
    addr = f"e2e-{uuid.uuid4().hex[:8]}@reports.test"
    r = await client.post("/auth/register", json={
        "company_name": "Northbridge Trading", "email": addr,
        "name": "Owner", "password": "testpass123"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]

    r = await client.patch("/companies/me", headers=_h(tok),
                           json={"settings": {"currency": "THB"}})
    assert r.status_code == 200, r.text

    r = await client.post("/crm/contacts", headers=_h(tok),
                          json={"name": "Riverside Supplies", "contact_type": "customer"})
    assert r.status_code == 200, r.text
    contact = r.json()["id"]

    async def _invoice(total: float, issue_date: str) -> str:
        r = await client.post("/docs", headers=_h(tok), json={
            "doc_type": "invoice", "contact_id": contact, "issue_date": issue_date,
            "line_items": [{"name": "Widget", "quantity": 1, "unit_price": total,
                            "line_total": total}],
            "subtotal": total, "tax": 0.0, "total": total})
        assert r.status_code == 200, r.text
        doc_id = r.json()["id"]
        assert (await client.post(f"/docs/{doc_id}/finalize",
                                  headers=_h(tok))).status_code == 200
        return doc_id

    paid = await _invoice(100.0, "2026-01-05")
    r = await client.post(f"/docs/{paid}/payment", headers=_h(tok), json={
        "payment_date": "2026-01-20", "amount": 40.0, "method": "transfer",
        "bank_account": "1111"})
    assert r.status_code == 200, r.text
    await _invoice(30.0, "2026-02-10")

    r = await client.post("/accounting/journal-entries", headers=_h(tok), json={
        "ts": "2026-02-01", "memo": "February rent",
        "entries": [{"account": "6200", "debit": 25.0, "credit": 0},
                    {"account": "1111", "debit": 0, "credit": 25.0}],
        "idempotency_token": uuid.uuid4().hex})
    assert r.status_code == 200, r.text

    r = await client.post("/accounting/journal-entries", headers=_h(tok), json={
        "ts": "2026-02-15", "memo": "Late delivery charge",
        "entries": [{"account": "1120", "debit": 15.0, "credit": 0, "contact": contact},
                    {"account": "4100", "debit": 0, "credit": 15.0}],
        "idempotency_token": uuid.uuid4().hex})
    assert r.status_code == 200, r.text

    return tok, contact


# Every report that gets a golden. Read from REPORTS rather than written out, so a
# report that moves again moves here with it and cannot be quietly dropped from the
# sweep; the account ledger is added because it is a page a reader lands on from
# every one of them.
GOLDEN_KEYS = list(REPORTS) + ["ledger"]


def _urls(contact: str) -> dict[str, str]:
    """The URL each report is read at."""
    period = "&".join(f"{k}={v}" for k, v in PERIOD)
    urls = {}
    for key in REPORTS:
        path = REPORTS[key][0]
        if key == "balance-sheet":
            urls[key] = f"{path}?as_of={AS_OF}"
        elif key == "statement":
            urls[key] = (f"{path}?{period}"
                         f"&account={subject_token('c', contact)}"
                         f"&account={subject_token('a', '1120')}")
        else:
            urls[key] = f"{path}?{period}"
    urls["ledger"] = f"{ledger_path('1120')}?{period}"
    return urls


_VOLATILE = [
    # Ids the database allocates. Everything else on the page is the report.
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<uuid>"),
    (re.compile(r"[0-9a-f]{32}"), "<hex>"),
    (re.compile(r"\b(contact|doc|item):[0-9A-Za-z_-]+"), r"\1:<id>"),
    (re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?"), "<ts>"),
    # Document references carry the month they were raised in, which is the month
    # the test runs, not the month the invoice is dated.
    (re.compile(r"\b([A-Z]{2,4})-\d{4}-\d{4}\b"), r"\1-<ref>"),
]


def _normalise(html: str) -> str:
    for pattern, replacement in _VOLATILE:
        html = pattern.sub(replacement, html)
    return html.strip() + "\n"


@pytest_asyncio.fixture
async def seeded(client):
    """The books, and a UI client whose API calls reach the real API."""
    from celerp.main import app as api_app
    from ui.app import app as ui_app

    tok, contact = await _seed(client)

    def _bridged(token, timeout=10.0):
        return AsyncClient(
            transport=ASGITransport(app=api_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as ui:
            yield ui, tok, contact


async def _fetch(ui, tok, url: str, fragment: bool = False) -> str:
    headers = {"HX-Request": "true"} if fragment else {}
    r = await ui.get(url, cookies={"celerp_token": tok}, headers=headers)
    assert r.status_code == 200, f"{url}: {r.status_code} {r.text[:400]}"
    return r.text


@pytest.mark.asyncio
async def test_every_report_renders_its_own_figures_end_to_end(seeded):
    """Each report, from the URL a reader clicks to the figures on the page.

    The figures asserted are the ones the seeded books produce and no other report
    produces by accident: the outstanding receivable, the rent, the cash left in the
    bank. A page that reached the API with the wrong period or the wrong account
    fails here even though it still returns 200 and still renders a table.
    """
    ui, tok, contact = seeded
    urls = _urls(contact)

    pnl = await _fetch(ui, tok, urls["pnl"])
    assert "145.00" in pnl, "revenue of 100 + 30 + 15"
    assert "25.00" in pnl, "the rent entry"

    bs = await _fetch(ui, tok, urls["balance-sheet"])
    assert "105.00" in bs, "receivables: 100 + 30 + 15 less the 40 paid"

    tb = await _fetch(ui, tok, urls["trial-balance"])
    # Gross, not net: the trial balance shows what went through the account, so
    # receivables reads 145 raised against 40 settled rather than the 105 left.
    assert "1120" in tb and "145.00" in tb and "40.00" in tb

    gl = await _fetch(ui, tok, urls["general-ledger"])
    assert "6200" in gl and "25.00" in gl

    cf = await _fetch(ui, tok, urls["cash-flow"])
    assert "15.00" in cf, "cash: 40 received less 25 paid out"

    st = await _fetch(ui, tok, urls["statement"])
    assert "Riverside Supplies" in st
    assert "Late delivery charge" in st, "a hand-posted line, attributed by its contact key"

    ledger = await _fetch(ui, tok, urls["ledger"])
    assert "Late delivery charge" in ledger


@pytest.mark.asyncio
async def test_the_hand_posted_line_reaches_the_statement_by_its_contact_key(seeded):
    """The contact key is what puts a manual entry on a party's statement.

    Posting straight to receivables is how an adjustment gets made; without the key
    the amount lands in the control account and on nobody's statement, and the
    subledger stops adding up to the control account it is supposed to explain.
    """
    ui, tok, contact = seeded
    statement = await _fetch(ui, tok, _urls(contact)["statement"])
    sections = statement.split('<h3 class="report-section-title">')[1:]
    assert len(sections) == 2, "one section per selected subject"
    party = next(s for s in sections if s.startswith("Riverside Supplies"))
    control = next(s for s in sections if s.startswith("1120"))
    assert "Late delivery charge" in party, "the customer's own statement carries it"
    assert "Late delivery charge" in control, "and so does the control account's"


@pytest.mark.parametrize("key", GOLDEN_KEYS)
@pytest.mark.asyncio
async def test_report_html_matches_its_golden(seeded, key):
    """The rendered report, against the last render that was read and agreed.

    Compared as the HTMX fragment rather than the whole page, so the golden holds
    the report and not the sidebar: a nav entry added elsewhere in the application
    is not a change to the balance sheet and should not have to be re-agreed as one.
    """
    ui, tok, contact = seeded
    url = _urls(contact)[key]
    rendered = _normalise(await _fetch(ui, tok, url, fragment=True))

    golden = GOLDEN_DIR / f"{key}.html"
    if os.environ.get("UPDATE_REPORT_GOLDENS"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered)
        return
    assert golden.exists(), (
        f"no golden for {key}; run with UPDATE_REPORT_GOLDENS=1, read it, commit it")
    assert rendered == golden.read_text(), (
        f"{key} renders differently from {golden}. If the change was intended, "
        f"rerun with UPDATE_REPORT_GOLDENS=1 and commit the new golden with it.")
