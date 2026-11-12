# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Turning funds off and a posting that carries a fund, as real concurrent requests.

Both requests queue behind a transaction holding the company row, in a known order, then
run against committed data. The only acceptable outcome is a serial one: the posting
commits and turning funds off is refused naming the fund it left a balance in, or funds
go off first and the posting is refused for carrying a fund, never a fund line committed
after funds went off.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _books(engine, client):
    from test_helpers import provision_company_books

    user = await owner(engine)
    cid = await company(engine, user, "Fund Race", "fund-race", settings={"currency": "USD"})
    async with maker(engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    tok = await token(engine, user, cid)
    r = await client.put("/accounting/funds/setting", headers=auth(tok), json={"enabled": True})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/funds", headers=auth(tok), json={
        "request_key": uuid.uuid4().hex, "name": "Roof appeal", "restricted": True, "purpose": "Roof"})
    assert r.status_code == 200, r.text
    return cid, tok, r.json()["id"]


async def _held(engine, cid):
    conn = await engine.connect()
    tx = await conn.begin()
    await conn.execute(text("SELECT id FROM companies WHERE id = :c FOR UPDATE"), {"c": uuid.UUID(str(cid))})
    return conn, tx


async def _blocked(engine, n: int) -> None:
    for _ in range(400):
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"))).scalar_one()
        if waiting >= n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{n} requests never queued on the company lock")


async def _fund_lines(engine, cid, fund: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT count(*) FROM projections p, jsonb_array_elements(p.state->'entries') e "
            "WHERE p.company_id = :c AND p.entity_type = 'journal_entry' AND e->>'fund_id' = :f"),
            {"c": uuid.UUID(str(cid)), "f": fund})).scalar_one()


@pytest.mark.parametrize("posting_first", [True, False])
async def test_funds_off_vs_posting_serialized(real_engine, real_client, posting_first):
    cid, tok, fund = await _books(real_engine, real_client)
    posting = {"ts": "2026-03-01", "memo": "Gift", "idempotency_token": uuid.uuid4().hex, "entries": [
        {"account": "1111", "debit": 60, "credit": 0, "fund_id": fund},
        {"account": "4100", "debit": 0, "credit": 60, "fund_id": fund}]}

    def post():
        return asyncio.create_task(real_client.post("/accounting/journal-entries", headers=auth(tok), json=posting))

    def off():
        return asyncio.create_task(real_client.put("/accounting/funds/setting", headers=auth(tok),
                                                   json={"enabled": False}))

    conn, tx = await _held(real_engine, cid)
    try:
        first = post() if posting_first else off()
        await _blocked(real_engine, 1)
        second = off() if posting_first else post()
        await _blocked(real_engine, 2)
    finally:
        await tx.commit()
        await conn.close()
    r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), timeout=60)
    r_post, r_off = (r1, r2) if posting_first else (r2, r1)

    if posting_first:
        assert r_post.status_code == 200, r_post.text
        assert r_off.status_code == 422, r_off.text
        assert r_off.json()["detail"]["message_key"] == "fund.balance_held"
        assert "Roof appeal" in r_off.json()["detail"]["params"]["funds"]
        assert await _fund_lines(real_engine, cid, fund) == 2
    else:
        assert r_off.status_code == 200, r_off.text
        assert r_post.status_code == 422, r_post.text
        assert r_post.json()["detail"]["message_key"] == "posting.fund.funds_off"
        assert await _fund_lines(real_engine, cid, fund) == 0
