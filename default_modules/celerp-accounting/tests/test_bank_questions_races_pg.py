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
from decimal import Decimal

import pytest
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
