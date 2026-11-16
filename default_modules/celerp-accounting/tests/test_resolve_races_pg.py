# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A resolve against other writers of the same company, on real PostgreSQL connections
against committed data in a database of the test's own.

Every step of a resolve that writes takes the company's lock before any statement line, as
every other writer does, so an undo of the same line never deadlocks with it. A decision reads
the entries keyed since the request's read of the books under that lock, so an entry keyed by
someone else in between is matched, never posted a second time."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

import bank_statement_helpers as B
from race_support import _until_waiting_or_done
from test_bank_complete_race_pg import own_client  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _keyed(client, co, bk, amount: float, day: str) -> str:
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": day, "memo": "keyed", "idempotency_token": B.key(),
        "entries": [{"account": "5100", "debit": amount, "credit": 0.0},
                    {"account": bk["chart_account_code"], "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _bank_entries(committed_engine, co, bk) -> list[str]:
    """The company's posted entries with a line on the bank's account."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from celerp.services.ledger_rows import journal_rows

    async with AsyncSession(committed_engine) as s:
        return sorted({je for je, _, _ in await journal_rows(s, uuid.UUID(co["company_id"]),
                                                              account=bk["chart_account_code"])})


async def _rebuilt_equals_stored(committed_engine, co) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    cid = uuid.UUID(co["company_id"])

    async def stored(s):
        rows = (await s.execute(select(Projection).where(Projection.company_id == cid))).scalars()
        return {(r.entity_type, r.entity_id): r.state for r in rows}

    async with AsyncSession(committed_engine) as s:
        before = await stored(s)
        await lock_company(s, cid)
        await ProjectionEngine.rebuild(s, company_id=cid)
        await s.commit()
        s.expire_all()
        assert await stored(s) == before


async def test_an_undo_during_a_several_twins_refusal_never_deadlocks(committed_engine, own_client, monkeypatch):
    """The refused line's suggestion is written under the company lock taken before the line's,
    so an undo of the same line (company, then line) waits for it instead of deadlocking."""
    import celerp_accounting.bank_statements as BS

    c = own_client
    co = await B.company(c)
    bk = await B.bank(c, co["h"])
    await B.statement(c, co, bk["id"], [("2026-01-20", "TWO", "-80.00")], opening="0.00", closing="-80.00",
                      start="2026-01-01", end="2026-01-31")
    await _keyed(c, co, bk, 80.0, "2026-01-19")
    await _keyed(c, co, bk, 80.0, "2026-01-21")
    line = B.line_by_text(await B.groups(c, co, bk["id"]), "TWO")
    real, paused, go = BS._suggest_twin, asyncio.Event(), asyncio.Event()

    async def suggest_twin(*a, **kw):
        await real(*a, **kw)
        paused.set()
        await go.wait()

    monkeypatch.setattr(BS, "_suggest_twin", suggest_twin)
    first = asyncio.create_task(B.resolve(c, co, bk["id"], decisions=[
        {"line_ids": [line["id"]], "kind": "post", "account_code": "5100"}]))
    await asyncio.wait_for(paused.wait(), timeout=20)
    undo = asyncio.create_task(c.post(f"{B.API}/bank/{bk['id']}/unmatch", headers=co["h"],
                                      json={"line_ids": [line["id"]], "idempotency_key": B.key()}))
    await _until_waiting_or_done(committed_engine, undo)
    go.set()
    one, two = await asyncio.wait_for(asyncio.gather(first, undo), timeout=60)
    assert one.status_code == 200 and not one.json()["results"][0]["ok"], one.text
    assert two.status_code < 500, two.text
    after = B.line_by_text(await B.groups(c, co, bk["id"], view="all"), "TWO")
    assert after["status"] == "unmatched" and (after["suggestion"] or {}).get("source") == "twins", after


async def test_an_entry_keyed_after_the_read_of_the_books_is_matched_never_posted_twice(
        committed_engine, own_client, monkeypatch):
    """A group accept reads the books near its lines once; an entry keyed by someone else after
    that read and before the line's decision is matched to the line instead of the line posting
    a second entry for the same money, and the books equal their rebuild from the ledger."""
    import celerp_accounting.routes as R

    c = own_client
    co = await B.company(c)
    bk = await B.bank(c, co["h"])
    await B.statement(c, co, bk["id"], [("2026-01-05", "NETFLIX", "-15.00")], opening="0.00",
                      closing="-15.00", start="2026-01-01", end="2026-01-31")
    r = await c.post("/accounting/rules", json={"match_pattern": "NETFLIX", "target_account_code": "5100",
                                                "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    r = await c.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    table = await B.groups(c, co, bk["id"])
    accept = B.accept(table, [g["key"] for g in table["groups"]])
    assert len(accept["line_ids"]) == 1, table
    real, keyed = R._je_entries_for_account, []

    async def read_then_key(*a, **kw):
        rows = await real(*a, **kw)
        if not keyed:  # the request's read of the books; someone keys the money now
            keyed.append(asyncio.create_task(_keyed(c, co, bk, 15.0, "2026-01-05")))
            await _until_waiting_or_done(committed_engine, keyed[0])
        return rows

    monkeypatch.setattr(R, "_je_entries_for_account", read_then_key)
    out = await asyncio.wait_for(B.resolve(c, co, bk["id"], accept=accept), timeout=60)
    je = await asyncio.wait_for(keyed[0], timeout=30)
    assert out.status_code == 200, out.text
    row = out.json()["results"][0]
    assert row["ok"] and row["matched_instead"] and row["je_ids"] == [je], row
    assert await _bank_entries(committed_engine, co, bk) == [je]
    await _rebuilt_equals_stored(committed_engine, co)
