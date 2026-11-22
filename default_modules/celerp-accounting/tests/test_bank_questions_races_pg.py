# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Asking the client about bank lines against other writers of the same company, on real
PostgreSQL connections against committed data in a database of the test's own.

The Ask records its email attempt and commits before the email goes out, so no writer of
the company waits on the transport, and a second request with the same key while the first
is still sending answers the stored attempt instead of sending again."""

from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bank_statement_helpers as B
from celerp.services.email import EmailResult
from test_bank_complete_race_pg import own_client  # noqa: F401

pytestmark = pytest.mark.asyncio

# Past the 3-second lock timeout every other writer of the company waits for.
SLOW_S = 4.5


async def _setup(c, engine):
    from test_helpers import grant_permission
    co = await B.company(c)
    bk = await B.bank(c, co["h"])
    rows = [("2026-01-05", "WITHDRAWAL", "-200.00"), ("2026-01-06", "TRANSFER", "-50.00")]
    closing = f"{sum(Decimal(a) for _, _, a in rows):.2f}"
    await B.statement(c, co, bk["id"], rows, opening="0.00", closing=closing, start="2026-01-01", end="2026-01-31")
    await grant_permission(c, co["h"], "answer_bank_questions", "viewer")
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as s:
        await B.member(c, s, co, "viewer")
        await s.commit()
    lines = {l["description"]: l for l in B.all_lines(await B.groups(c, co, bk["id"]))}
    return co, bk, lines


def _ask(c, co, bk, line_id, idem):
    return c.post(f"{B.API}/bank/{bk['id']}/ask", json={
        "line_ids": [line_id], "note": "What was this for?", "idempotency_key": idem}, headers=co["h"])


def _slow_transport(monkeypatch, entered: asyncio.Event, calls: list):
    """A relay that takes SLOW_S to accept each email, refusing like the real transport
    when its caller holds a transaction."""
    async def slow(to, subject, body_html, body_text="", **kw):
        from celerp.services.company_lock import refuse_held_transaction
        refuse_held_transaction("an email is sent")
        calls.append(kw["operation_id"])
        entered.set()
        await asyncio.sleep(SLOW_S)
        return EmailResult(True, "sent")
    monkeypatch.setattr("celerp.services.email.send_email", slow)


async def test_a_slow_email_never_blocks_a_write_to_the_company(committed_engine, own_client, monkeypatch):
    c = own_client
    co, bk, lines = await _setup(c, committed_engine)
    entered, calls = asyncio.Event(), []
    _slow_transport(monkeypatch, entered, calls)
    asking = asyncio.create_task(_ask(c, co, bk, lines["WITHDRAWAL"]["id"], B.key()))
    await asyncio.wait_for(entered.wait(), 20)
    began = time.monotonic()
    other = await B.bank(c, co["h"], name="Other Bank")
    waited = time.monotonic() - began
    assert other["id"]
    assert waited < SLOW_S - 1, f"the write waited {waited:.1f}s on the email"
    assert not asking.done()
    r = await asyncio.wait_for(asking, 30)
    assert r.status_code == 200 and r.json()["email"]["state"] == "sent", r.text


async def test_the_same_ask_sent_twice_at_once_emails_once(committed_engine, own_client, monkeypatch):
    c = own_client
    co, bk, lines = await _setup(c, committed_engine)
    entered, calls = asyncio.Event(), []
    _slow_transport(monkeypatch, entered, calls)
    idem = B.key()
    first = asyncio.create_task(_ask(c, co, bk, lines["WITHDRAWAL"]["id"], idem))
    await asyncio.wait_for(entered.wait(), 20)
    second = await _ask(c, co, bk, lines["WITHDRAWAL"]["id"], idem)
    assert second.status_code == 200, second.text
    # Still with the first run's transport: it says so and is not sent again.
    assert second.json()["email"]["state"] == "sending", second.json()
    done = await asyncio.wait_for(first, 30)
    assert done.status_code == 200 and done.json()["email"]["state"] == "sent", done.text
    assert len(calls) == 1
    again = await _ask(c, co, bk, lines["WITHDRAWAL"]["id"], idem)
    assert again.json() == done.json() and len(calls) == 1


async def test_a_receipt_answered_while_a_line_attachment_is_saved_keeps_both(committed_engine, own_client,
                                                                               monkeypatch):
    """Every writer of a line holds the company lock, so an answer with a receipt waits for a file
    being saved to the same line from the reconciliation page and keeps it, never writes over it."""
    from celerp_accounting import statement_lines
    from celerp_accounting.models import BankStatementLine
    from race_support import _until_waiting_or_done

    c = own_client
    co, bk, lines = await _setup(c, committed_engine)
    lid = lines["WITHDRAWAL"]["id"]
    _slow_transport(monkeypatch, asyncio.Event(), [])
    monkeypatch.setitem(globals(), "SLOW_S", 0)
    assert (await _ask(c, co, bk, lid, B.key())).status_code == 200
    async with async_sessionmaker(bind=committed_engine)() as s:
        client_h = await B.member(c, s, co, "viewer")
        await s.commit()
        sid = await s.scalar(select(BankStatementLine.reconciliation_id).where(BankStatementLine.id == uuid.UUID(lid)))
    entered, go, real = asyncio.Event(), asyncio.Event(), statement_lines.attach_file

    async def paused(*args, **kwargs):
        meta = await real(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            await go.wait()
        return meta
    monkeypatch.setattr(statement_lines, "attach_file", paused)
    pdf = b"%PDF-1.4\n%%EOF\n"
    attach = asyncio.create_task(c.post(f"/accounting/reconciliation/{sid}/lines/{lid}/attach", headers=co["h"],
                                        files={"file": ("statement.pdf", pdf + b"%1\n", "application/pdf")}))
    await asyncio.wait_for(entered.wait(), 10)
    fid = B.upload(co["company_id"], B.user_id(client_h), pdf, "slip.pdf", "application/pdf")
    answer = asyncio.create_task(c.post(f"/accounting/bank-questions/{lid}/answer", headers=client_h, json={
        "answer": "receipt", "file_ids": [fid], "idempotency_key": B.key()}))
    await _until_waiting_or_done(committed_engine, answer)
    go.set()
    a, r = await asyncio.wait_for(asyncio.gather(attach, answer), 30)
    assert a.status_code == 200 and r.status_code == 200, (a.text, r.text)
    async with async_sessionmaker(bind=committed_engine)() as s:
        held = await s.scalar(select(BankStatementLine.attachment_ids).where(BankStatementLine.id == uuid.UUID(lid)))
    assert len(held) == 2, held


async def test_the_same_answer_sent_while_the_first_stores_its_receipt_stores_one_file(committed_engine, own_client,
                                                                                         monkeypatch):
    """A second request with the key of an answer still storing its receipt waits for the company
    lock, then gives the first answer and stores no file of its own: the line holds one receipt and
    no file is left on disk without a record."""
    from celerp_accounting import statement_lines
    from celerp_accounting.models import BankStatementLine
    from race_support import _until_waiting_or_done

    c = own_client
    co, bk, lines = await _setup(c, committed_engine)
    lid = lines["WITHDRAWAL"]["id"]
    _slow_transport(monkeypatch, asyncio.Event(), [])
    monkeypatch.setitem(globals(), "SLOW_S", 0)
    assert (await _ask(c, co, bk, lid, B.key())).status_code == 200
    async with async_sessionmaker(bind=committed_engine)() as s:
        client_h = await B.member(c, s, co, "viewer")
        await s.commit()
    entered, go, real, stored = asyncio.Event(), asyncio.Event(), statement_lines.attach_file, []

    async def paused(*args, **kwargs):
        stored.append(await real(*args, **kwargs))
        if not entered.is_set():
            entered.set()
            await go.wait()
        return stored[-1]
    monkeypatch.setattr(statement_lines, "attach_file", paused)
    fid = B.upload(co["company_id"], B.user_id(client_h), b"%PDF-1.4\n%%EOF\n", "slip.pdf", "application/pdf")
    body = {"answer": "receipt", "file_ids": [fid], "idempotency_key": B.key()}
    first = asyncio.create_task(c.post(f"/accounting/bank-questions/{lid}/answer", headers=client_h, json=body))
    await asyncio.wait_for(entered.wait(), 10)
    second = asyncio.create_task(c.post(f"/accounting/bank-questions/{lid}/answer", headers=client_h, json=body))
    await _until_waiting_or_done(committed_engine, second)
    go.set()
    one, two = await asyncio.wait_for(asyncio.gather(first, second), 30)
    assert one.status_code == 200 and two.status_code == 200, (one.text, two.text)
    assert one.json() == two.json()
    async with async_sessionmaker(bind=committed_engine)() as s:
        held = await s.scalar(select(BankStatementLine.attachment_ids).where(BankStatementLine.id == uuid.UUID(lid)))
    assert len(held) == 1 and len(stored) == 1, (held, stored)
