# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A request's database work does not grow with the history behind it. Each case runs the
same request once over a short history and once over 10,000 history rows and counts, with an
``after_cursor_execute`` listener, the statements it sends and the rows they return: a bank's
suggest and resolve (a post and a match) next to years of matched statement lines and their
book entries, and an assistant conversation's older-actions read behind finished actions."""

from __future__ import annotations

import contextlib
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert
from sqlalchemy.engine import Engine

import bank_statement_helpers as B
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

_SHORT, _LONG = 10, 10_000


@contextlib.contextmanager
def _work():
    """Statements sent and rows returned on any engine during the block."""
    seen = {"statements": 0, "rows": 0}

    def _after(conn, cursor, statement, parameters, context, executemany):
        seen["statements"] += 1
        if statement.lstrip()[:6].upper() in ("SELECT", "WITH ("):
            seen["rows"] += max(cursor.rowcount, 0)

    event.listen(Engine, "after_cursor_execute", _after)
    try:
        yield seen
    finally:
        event.remove(Engine, "after_cursor_execute", _after)


async def _bank_history(session, co: dict, bk: dict, n: int, rng: random.Random) -> None:
    """``n`` past book entries on the bank, each matched or posted by a statement line of one of
    n/10 completed past statements, other counterparties than this month's, and an invoice paid
    in full for every fifth: the history a bank has after years of reconciling."""
    from celerp_accounting.models import BankStatementLine, ReconciliationSession

    company_id, bank_id = uuid.UUID(co["company_id"]), uuid.UUID(bk["id"])
    statements = [{"id": uuid.uuid4(), "company_id": company_id, "bank_account_id": bank_id, "status": "completed",
                   "statement_date": (date(2023, 1, 1) + timedelta(days=i)).isoformat(), "statement_balance": 0,
                   "reconciled_je_ids": []} for i in range(max(1, n // 10))]
    entries, lines, docs = [], [], []
    for i in range(n):
        st = statements[i * len(statements) // n]
        je, amount = str(uuid.uuid4()), round(rng.uniform(5, 900), 2)
        st["reconciled_je_ids"].append(je)
        entries.append({"company_id": company_id, "entity_id": je, "entity_type": "journal_entry", "version": 1,
                        "updated_at": datetime.now(timezone.utc), "state": {
                            "ts": st["statement_date"], "status": "posted", "memo": f"past {i}",
                            "entries": [{"account": bk["chart_account_code"], "debit": amount, "credit": 0.0},
                                        {"account": "4100", "debit": 0.0, "credit": amount}]}})
        lines.append({"id": uuid.uuid4(), "company_id": company_id, "reconciliation_id": st["id"],
                      "bank_account_id": bank_id, "fingerprint": f"{i:016x}", "line_date": st["statement_date"],
                      "description": f"CUSTOMER {rng.randrange(400)} PAYMENT", "amount": amount,
                      "reference": f"INV{i:06d}", "status": ("matched", "posted")[i % 2], "matched_je_ids": [je],
                      "counterparty_key": f"CUSTOMER {rng.randrange(400)}"})
        if i % 5 == 0:
            docs.append({"company_id": company_id, "entity_id": f"doc:paid-{i}", "entity_type": "doc", "version": 1,
                         "updated_at": datetime.now(timezone.utc), "state": {
                             "doc_type": "invoice", "status": "paid", "doc_number": f"INV{i:06d}",
                             "total": amount, "amount_outstanding": 0, "currency": "USD"}})
    for table, rows in ((ReconciliationSession, statements), (BankStatementLine, lines), (Projection, entries + docs)):
        for start in range(0, len(rows), 2000):
            await session.execute(insert(table), rows[start:start + 2000])
    await session.commit()


async def _bank_requests(client, session, n: int) -> dict[str, dict]:
    """The work of a suggest, a post and a match on this month's statement, over ``n`` history rows."""
    rng = random.Random(n)
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    rows = [("2026-03-05", "ACME SUPPLIES", "-120.00"), ("2026-03-09", "CITY POWER", "-80.00"),
            ("2026-03-12", "SHOP SALE", "300.00")]
    _sid, lines = await B.lines_on(client, co, bk["id"], rows, opening="0.00", closing="100.00",
                                   start="2026-03-01", end="2026-03-31")
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": "2026-03-12", "memo": "sale", "idempotency_token": B.key(),
        "entries": [{"account": bk["chart_account_code"], "debit": 300.0, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": 300.0}]})
    assert r.status_code == 200, r.text
    je = r.json()["je_id"]
    await _bank_history(session, co, bk, n, rng)
    by_text = {line["description"]: line["id"] for line in lines}
    work = {}
    with _work() as work["suggest"]:
        r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    with _work() as work["post"]:
        r = await B.resolve(client, co, bk["id"], decisions=[
            {"line_ids": [by_text["ACME SUPPLIES"]], "kind": "post", "account_code": "5100"}])
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.text
    with _work() as work["match"]:
        r = await B.resolve(client, co, bk["id"], decisions=[
            {"line_ids": [by_text["SHOP SALE"]], "kind": "match", "je_ids": [je]}])
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.text
    return work


@pytest.fixture
def every_drain_read(monkeypatch):
    """The drain flag read on every request, never from a cache a request's timing decides."""
    from celerp.services import runtime_state

    monkeypatch.setattr(runtime_state, "_DRAIN_TTL_S", -1)


async def test_a_bank_request_does_the_same_database_work_over_ten_thousand_history_rows(client, session,
                                                                                          every_drain_read):
    short = await _bank_requests(client, session, _SHORT)
    long = await _bank_requests(client, session, _LONG)
    print({k: (short[k], long[k]) for k in short})
    assert {k: v["statements"] for k, v in long.items()} == {k: v["statements"] for k, v in short.items()}
    assert {k: v["rows"] for k, v in long.items()} == {k: v["rows"] for k, v in short.items()}


async def _older_actions(client, session, n: int) -> dict:
    """The work of one older-actions read behind ``n`` finished actions."""
    from celerp.ai.conversations import create_conversation
    from celerp.gateway.state import set_session_token
    from celerp.models.ai import AIMessage

    co = await B.company(client)
    set_session_token("s" * 64)  # the assistant runs on a connected installation; the client fixture restores it
    conv = await create_conversation(session, uuid.UUID(co["company_id"]), uuid.UUID(co["user_id"]))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"id": uuid.uuid4(), "conversation_id": conv.id, "role": "assistant", "content": "done",
             "created_at": start + timedelta(seconds=i), "tools_called": []} for i in range(n)]
    for i in range(0, n, 2000):
        await session.execute(insert(AIMessage), rows[i:i + 2000])
    await session.commit()
    with _work() as seen:
        r = await client.get(f"/ai/conversations/{conv.id}/older-actions", headers={**co["h"], "X-Session-Token": "s" * 64}, params={
            "before_at": (start + timedelta(days=1)).isoformat(), "before_id": str(uuid.UUID(int=2 ** 128 - 1))})
    assert r.status_code == 200, r.text
    return seen


async def test_an_older_actions_read_does_the_same_database_work_however_long_the_finished_history(client, session):
    from celerp.ai.conversations import PROTECTED_SCAN_BUDGET

    budget = await _older_actions(client, session, 2 * PROTECTED_SCAN_BUDGET)
    long = await _older_actions(client, session, _LONG)
    assert long == budget
    assert long["rows"] <= 2 * PROTECTED_SCAN_BUDGET
