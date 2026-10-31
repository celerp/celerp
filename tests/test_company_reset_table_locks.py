# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A reset holds its company's key exclusively before it locks the tables, so it never waits
for a table: a transaction of another company writing one of them may go on to share the
reset company's key (a notice naming it, a row referring to it), and the two would wait on
each other. The reset finds the table busy, changes nothing and asks the owner to try again."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from celerp.services import company_reset
from company_backup_support import snapshot, token
from migration_support import auth, count, maker, real_client, real_engine  # noqa: F401
from test_company_reset import RESET, _local_files, _two_companies

pytestmark = pytest.mark.asyncio

NAME = "Harbor Goods Ltd"
_WAITING_ON_A_TABLE = ("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                       "AND wait_event_type = 'Lock' AND wait_event = 'relation'")


async def _waits_on_a_table(engine, task: asyncio.Task) -> bool:
    """Whether *task* is seen waiting on a table lock before it finishes."""
    while not task.done():
        async with engine.connect() as conn:
            if (await conn.execute(text(_WAITING_ON_A_TABLE))).scalar_one():
                return True
        await asyncio.sleep(0.02)
    return False


async def test_a_reset_never_waits_for_a_table_another_company_is_writing(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    writer = maker(real_engine)()
    try:
        await writer.execute(text("UPDATE locations SET name = name WHERE company_id = :b"), {"b": str(b)})
        before = await snapshot(real_engine)
        reset = asyncio.create_task(real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok)))
        waited = await _waits_on_a_table(real_engine, reset)
        r = await reset
        await writer.commit()
    finally:
        await writer.close()

    assert not waited
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == company_reset._busy().detail
    assert await snapshot(real_engine) == before
    again = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok))
    assert again.status_code == 200, again.text
    assert await count(real_engine, "companies", "id = :c", c=str(a)) == 0


_WAITING_ON_ANY_LOCK = ("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock'")


async def test_a_reset_never_waits_for_a_row_another_transaction_holds(
        real_engine, real_client, tmp_path, monkeypatch):
    """A transaction holding one of the company's rows, with nothing written yet, may go on to
    write it, which waits on the reset's table lock: the reset refuses at once instead of
    waiting on the row."""
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    holder = maker(real_engine)()
    try:
        await holder.execute(text("SELECT 1 FROM locations WHERE company_id = :a FOR UPDATE"), {"a": str(a)})
        before = await snapshot(real_engine)
        reset = asyncio.create_task(real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok)))
        waited = False
        while not reset.done() and not waited:
            async with real_engine.connect() as conn:
                waited = bool((await conn.execute(text(_WAITING_ON_ANY_LOCK))).scalar_one())
            await asyncio.sleep(0.02)
        await holder.commit()
        r = await reset
    finally:
        await holder.close()

    assert not waited
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == company_reset._busy().detail
    assert await snapshot(real_engine) == before
