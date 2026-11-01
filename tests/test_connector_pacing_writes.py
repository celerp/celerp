# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One pacing authority: execution._pace is the only code that writes when deferred
connector work is tried again, or how many times it was tried.

Held by the test database in every test (pacing_guard.py, from the root conftest): a
trigger on each table of execution.PACED refuses a change to its attempt or retry-time
column unless _pace, or a test seeding paced rows, marked the transaction. Here: the
database refuses each way such a write can be spelled, every table of PACED carries the
trigger, and every such column of a connector table is in PACED, so a new loop that
schedules its own retries fails."""
from __future__ import annotations

import re
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from celerp.connectors.execution import PACED
from celerp.db import get_session_ctx
from migration_support import real_engine  # noqa: F401 - fixture


@pytest.mark.asyncio
async def test_the_database_refuses_each_way_a_pacing_column_is_written(real_engine):
    import pacing_guard
    from celerp.connectors import execution
    from celerp.models.connector_config import OutboundQueue

    table, company = OutboundQueue.__table__, str(uuid.uuid4())
    mine = table.c.company_id == company
    row = {"company_id": company, "connector": "woocommerce", "entity_type": "item_stock"}
    key = {"c": company}

    async with get_session_ctx() as s:
        async def setattr_flush():
            queued = (await s.execute(sa.select(OutboundQueue).where(
                OutboundQueue.company_id == company, OutboundQueue.entity_id == "a"))).scalar_one()
            queued.retry_count = 3
            await s.flush()

        async def many_rows():
            s.add_all([OutboundQueue(**row, entity_id="e", payload_json="{}"),
                       OutboundQueue(**row, entity_id="f", payload_json="{}", retry_count=2)])
            await s.flush()

        def sql(text):
            return lambda: s.execute(sa.text(text), key)

        s.add(OutboundQueue(**row, entity_id="a", payload_json="{}"))
        s.add_all([OutboundQueue(**row, entity_id=k, payload_json="{}") for k in ("x", "y")])
        await s.flush()
        first = await s.scalar(sa.select(table.c.id).where(mine, table.c.entity_id == "a"))
        await s.execute(sa.update(table).where(mine, table.c.retry_count == 0).values(status="pending"))
        await s.execute(sa.update(table).where(mine).values(retry_count=0, next_retry_at=None))
        await s.execute(sa.select(table).where(mine, table.c.next_retry_at <= sa.func.now()))
        await execution._pace(s, "outbound_queue", "retry", mine)
        async with pacing_guard.seeding_pacing(s):
            await s.execute(sa.update(table).where(mine).values(retry_count=1))
        assert pacing_guard.FOUND == [], (
            "default inserts, an unchanged value, a read, a filter, _pace and a test's seed are not refused")

        writes = {
            "values(**dict)": lambda: s.execute(sa.update(table).where(mine).values(**{"next_retry_at": sa.func.now()})),
            "values({column: ...})": lambda: s.execute(
                sa.update(table).where(mine).values({table.c.retry_count: table.c.retry_count + 1})),
            "concatenated SQL": sql("UPDATE outbound_queue SET " + "next_retry_at" + " = now() WHERE company_id = :c"),
            "insert with a value": lambda: s.execute(sa.insert(table).values(
                **row, entity_id="b", next_retry_at=sa.func.now())),
            "SQL insert": sql("INSERT INTO outbound_queue (company_id, connector, entity_type, entity_id, retry_count) "
                              "VALUES (:c, 'woocommerce', 'item_stock', 'c', 2)"),
            "upsert": lambda: s.execute(pg_insert(table).values(id=first, **row, entity_id="a").on_conflict_do_update(
                index_elements=["id"], set_={"retry_count": 2})),
            "setattr": setattr_flush,
            "many rows at once": many_rows,
            "schema-qualified UPDATE": sql("UPDATE public.outbound_queue SET next_retry_at = now() WHERE company_id = :c"),
            "UPDATE ONLY": sql("UPDATE ONLY outbound_queue SET retry_count = 7 WHERE company_id = :c"),
            "tuple SET": sql("UPDATE outbound_queue SET (status, retry_count) = ('pending', 5) WHERE company_id = :c"),
            "schema-qualified INSERT": sql(
                "INSERT INTO public.outbound_queue (company_id, connector, entity_type, entity_id, retry_count) "
                "VALUES (:c, 'woocommerce', 'item_stock', 'g', 2)"),
            "MERGE": sql("MERGE INTO outbound_queue q USING (SELECT CAST(:c AS VARCHAR) AS c) v ON q.company_id = v.c "
                         "WHEN MATCHED THEN UPDATE SET retry_count = 9"),
        }
        missed = []
        for form, write in writes.items():
            before = len(pacing_guard.FOUND)
            try:
                async with s.begin_nested():
                    await write()
            except sa.exc.DBAPIError as exc:
                if "outside execution._pace" not in str(exc):
                    raise
            if len(pacing_guard.FOUND) != before + 1:
                missed.append(form)
        await s.rollback()
    pacing_guard.FOUND.clear()
    assert not missed, f"the database let through: {missed}"


@pytest.mark.asyncio
async def test_every_table_of_paced_carries_the_guard(real_engine):
    async with real_engine.connect() as conn:
        guarded = set((await conn.execute(sa.text(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = 'celerp_pacing_guard' AND t.tgenabled <> 'D'"))).scalars())
    assert set(PACED) <= guarded, f"PACED tables without the pacing trigger: {set(PACED) - guarded}"


def test_every_attempt_or_retry_column_of_a_connector_table_is_paced():
    import celerp.models  # noqa: F401 - every model registered
    from celerp.models.base import Base

    unpaced = sorted(f"{table.name}.{column.name}" for table in Base.metadata.tables.values()
                     if table.name.startswith("connector_") or table.name == "outbound_queue"
                     for column in table.columns
                     if re.search(r"retry|attempt", column.name) and column.name not in PACED.get(table.name, ())[:2])
    assert not unpaced, f"connector pacing columns missing from execution.PACED: {unpaced}"
    for table, paced in PACED.items():
        assert {paced.attempts, paced.retry_at} <= set(Base.metadata.tables[table].columns.keys()), table
