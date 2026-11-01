# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One pacing authority: execution._pace is the only code that writes when deferred
connector work is tried again, or how many times it was tried.

Held at run time in every test (pacing_guard.py, from the root conftest): a statement that
sets a column of execution.PACED outside _pace fails the test that sent it, whatever form
wrote it. Here: the guard sees each form, and every such column of a connector table is in
PACED, so a new loop that schedules its own retries fails."""
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
async def test_the_guard_sees_each_way_a_pacing_column_is_written(real_engine):
    import pacing_guard
    from celerp.connectors import execution
    from celerp.models.connector_config import OutboundQueue

    table, company = OutboundQueue.__table__, str(uuid.uuid4())
    mine = table.c.company_id == company
    row = {"company_id": company, "connector": "woocommerce", "entity_type": "item_stock"}

    async with get_session_ctx() as s:
        async def setattr_flush():
            queued = (await s.execute(sa.select(OutboundQueue).where(
                OutboundQueue.company_id == company, OutboundQueue.entity_id == "a"))).scalar_one()
            setattr(queued, "retry_count", 3)
            await s.flush()

        async def many_rows():
            s.add_all([OutboundQueue(**row, entity_id="e", payload_json="{}"),
                       OutboundQueue(**row, entity_id="f", payload_json="{}", retry_count=2)])
            await s.flush()

        s.add(OutboundQueue(**row, entity_id="a", payload_json="{}"))
        s.add_all([OutboundQueue(**row, entity_id=key, payload_json="{}") for key in ("x", "y")])
        await s.flush()
        await s.execute(sa.update(table).where(mine, table.c.retry_count == 0).values(status="pending"))
        await s.execute(sa.select(table).where(mine, table.c.next_retry_at <= sa.func.now()))
        await execution._pace(s, "outbound_queue", "retry", mine)
        assert pacing_guard.FOUND == [], "default inserts, a read, a filter and _pace itself are not writes"

        writes = {
            "values(**dict)": lambda: s.execute(sa.update(table).where(mine).values(**{"next_retry_at": sa.func.now()})),
            "values({column: ...})": lambda: s.execute(
                sa.update(table).where(mine).values({table.c.retry_count: table.c.retry_count + 1})),
            "concatenated SQL": lambda: s.execute(sa.text(
                "UPDATE outbound_queue SET " + "next_retry_at" + " = now() WHERE company_id = :c"), {"c": company}),
            "insert with a value": lambda: s.execute(sa.insert(table).values(
                **row, entity_id="b", next_retry_at=sa.func.now())),
            "SQL insert": lambda: s.execute(sa.text(
                "INSERT INTO outbound_queue (company_id, connector, entity_type, entity_id, retry_count) "
                "VALUES (:c, 'woocommerce', 'item_stock', 'c', 2)"), {"c": company}),
            "upsert": lambda: s.execute(pg_insert(table).values(**row, entity_id="d").on_conflict_do_update(
                index_elements=["id"], set_={"retry_count": 2})),
            "setattr": setattr_flush,
            "many rows at once": many_rows,
        }
        missed = []
        for form, write in writes.items():
            before = len(pacing_guard.FOUND)
            await write()
            if len(pacing_guard.FOUND) != before + 1:
                missed.append(form)
        await s.rollback()
    pacing_guard.FOUND.clear()
    assert not missed, f"the guard missed: {missed}"


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
