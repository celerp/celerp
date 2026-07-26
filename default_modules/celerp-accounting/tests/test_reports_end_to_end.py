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

import asyncio
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

# The print sheet and the CSV of the two journal pages get goldens of their own.
# The consolidation rewrites what those pages render, and a reader who prints the
# journal or opens it in a spreadsheet is as entitled to an unchanged render as one
# reading it on screen; neither surface is covered by the on-screen golden.
_EXPORT_KEYS = [f"{key}:{kind}" for key in ("journal", "extended-journal")
                for kind in ("print", "csv")]
GOLDEN_KEYS += _EXPORT_KEYS


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
    # A print sheet and a CSV take the period as date_from/date_to, the names
    # report_kit.date_params writes into the links a reader clicks; the on-screen
    # pages take from/to. Asked with the wrong names, an export renders the whole
    # book and its golden pins something nobody asked for.
    export_period = "&".join(f"date_{k}={v}" for k, v in PERIOD)
    for key in _EXPORT_KEYS:
        report, kind = key.split(":")
        urls[key] = f"{REPORTS[report][1 if kind == 'print' else 2]}?{export_period}"
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
    # A CSV goes out with CRLF line endings, which reading the golden back in text
    # mode turns into LF. Compare the rows, not the bytes that end them.
    return html.replace("\r\n", "\n").strip() + "\n"


def _golden_path(key: str) -> Path:
    """Where a key's golden lives. A colon is not a filename, and a CSV is not HTML."""
    suffix = ".csv" if key.endswith(":csv") else ".html"
    return GOLDEN_DIR / f"{key.replace(':', '-')}{suffix}"


_TBODY = re.compile(r"<tbody.*?</tbody>", re.S)

# Every seeded report renders at least this many rows of figures. Without a floor, a
# golden generated against a period holding nothing would be a set of column
# headings, would match itself forever, and would report a report that stopped
# working as unchanged.
_ROW_FLOOR = 2


def _rows(key: str, text: str) -> int:
    """Rows of figures in a render: table rows for HTML, lines under the header for CSV."""
    if key.endswith(":csv"):
        return max(len([line for line in text.splitlines() if line.strip()]) - 1, 0)
    return sum(block.count("<tr") for block in _TBODY.findall(text))


@pytest_asyncio.fixture
async def seeded(client):
    """The books, and a UI client whose API calls reach the real API."""
    from celerp.main import app as api_app
    from ui.app import app as ui_app

    tok, contact = await _seed(client)
    one_at_a_time = asyncio.Lock()

    class _SerialTransport(ASGITransport):
        """One API request in flight at a time.

        The API under test answers every request from the single session the `client`
        fixture holds open, so two requests at once make SQLAlchemy refuse the second.
        A UI route that fans its API calls out concurrently, as the CSV exports do, is
        still exercised whole here; its calls just queue. Nothing in production shares
        a session between requests, so this queue exists only in the harness.
        """

        async def handle_async_request(self, request):
            async with one_at_a_time:
                return await super().handle_async_request(request)

    def _bridged(token, timeout=10.0):
        return AsyncClient(
            transport=_SerialTransport(app=api_app),
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


_CLOSING = re.compile(r'class="report-total"[^>]*>.*?(-?[\d,]+\.\d\d)', re.S)


def _closing(section: str) -> float:
    """The closing balance a reader sees at the foot of one statement section."""
    found = _CLOSING.search(section)
    assert found, f"no closing balance rendered in section: {section[:300]}"
    return float(found.group(1).replace(",", ""))


@pytest.mark.asyncio
async def test_the_subledger_ties_to_its_control_account_on_the_rendered_page(seeded, client):
    """The figures on the statement page add up to the control account.

    This is the identity the whole subledger rests on: every party's statement,
    plus the lines that belong to no party, equals what the balance sheet reports
    for receivables. If it fails, a customer is being shown a balance the books do
    not agree with, which is the one error in a statement that cannot be argued
    about with the reader.

    Read off the rendered page rather than from the API, because the page is what
    the change added and what somebody acts on. The party and control figures come
    out of the HTML; the unattributed bucket has no page of its own, so it is read
    from the ledger endpoint and has to account for the whole difference between
    the two rendered figures, to the cent.
    """
    ui, tok, contact = seeded

    # A line posted to receivables with no party on it, which is how a deposit
    # nobody has allocated yet gets onto the books.
    r = await client.post("/accounting/journal-entries", headers=_h(tok), json={
        "ts": "2026-02-20", "memo": "Unallocated deposit",
        "entries": [{"account": "1120", "debit": 12.50, "credit": 0},
                    {"account": "4100", "debit": 0, "credit": 12.50}],
        "idempotency_token": uuid.uuid4().hex})
    assert r.status_code == 200, r.text

    statement = await _fetch(ui, tok, _urls(contact)["statement"])
    sections = statement.split('<h3 class="report-section-title">')[1:]
    party = next(s for s in sections if s.startswith("Riverside Supplies"))
    control = next(s for s in sections if s.startswith("1120"))

    assert "Unallocated deposit" not in party, "it belongs to no party, so no party shows it"
    assert "Unallocated deposit" in control, "the control account carries every line"

    r = await client.get("/accounting/ledger/1120", headers=_h(tok),
                         params={"from": PERIOD[0][1], "to": PERIOD[1][1], "contact_id": ""})
    assert r.status_code == 200, r.text
    unattributed = r.json()["closing_balance"]
    assert unattributed == pytest.approx(12.50), "the bucket holds the unallocated line"

    assert _closing(party) == pytest.approx(105.0), "100 raised + 30 raised + 15 charged - 40 paid"
    assert _closing(control) == pytest.approx(_closing(party) + unattributed), (
        "the statement page and the control account disagree, so the subledger "
        "does not explain the balance sheet")


@pytest.mark.parametrize("key", GOLDEN_KEYS)
@pytest.mark.asyncio
async def test_report_html_matches_its_golden(seeded, key):
    """The rendered report, against the last render that was read and agreed.

    On-screen reports are compared as the HTMX fragment rather than the whole page,
    so the golden holds the report and not the sidebar: a nav entry added elsewhere
    in the application is not a change to the balance sheet and should not have to be
    re-agreed as one. A print sheet and a CSV have no fragment to ask for and are
    compared whole, which is what a reader gets.
    """
    ui, tok, contact = seeded
    url = _urls(contact)[key]
    rendered = _normalise(await _fetch(ui, tok, url, fragment=":" not in key))
    assert _rows(key, rendered) >= _ROW_FLOOR, (
        f"{key} rendered {_rows(key, rendered)} rows of figures from books that hold "
        f"several, so this render is not the report and must not become its golden")

    golden = _golden_path(key)
    if os.environ.get("UPDATE_REPORT_GOLDENS"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered)
        return
    assert golden.exists(), (
        f"no golden for {key}; run with UPDATE_REPORT_GOLDENS=1, read it, commit it")
    assert rendered == golden.read_text(), (
        f"{key} renders differently from {golden}. If the change was intended, "
        f"rerun with UPDATE_REPORT_GOLDENS=1 and commit the new golden with it.")
