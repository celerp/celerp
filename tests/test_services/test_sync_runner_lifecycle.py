# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Real-DB lifecycle tests for run_sync.

These prove the in-progress SyncRun lifecycle against the REAL test database (they do
NOT mock get_session_ctx the way the connector unit tests do): an in-progress row is
committed and visible mid-sync, begin+finish collapse to a single row updated to a
terminal status, a second concurrent run for the same entity is refused, and a failing
sync records a 'failed' row. Each test uses a unique company_id for isolation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext, SyncDirection, SyncEntity, SyncResult
from celerp.connectors.sync_runner import run_sync
from celerp.db import get_session_ctx
from celerp.models.sync_run import SyncRun

pytestmark = pytest.mark.asyncio


def _cid() -> str:
    return f"co-{uuid.uuid4().hex[:12]}"


class _Stub:
    name = "stub_lifecycle"
    direction = SyncDirection.BOTH

    def __init__(self, on_run=None, boom=False):
        self._on_run = on_run
        self._boom = boom

    async def sync_orders(self, ctx, since=None):
        if self._on_run:
            await self._on_run()
        if self._boom:
            raise RuntimeError("api 500")
        return SyncResult(entity=SyncEntity.ORDERS, created=2, updated=1)


async def _rows(cid):
    async with get_session_ctx() as s:
        return (await s.execute(sa.select(SyncRun).where(SyncRun.company_id == cid))).scalars().all()


async def test_in_progress_row_visible_then_single_terminal_row(_db_engine):
    cid = _cid()
    seen = {}

    async def _mid():
        # Separate connection: proves the in-progress row was committed and is visible
        # to other readers while the sync body runs.
        async with get_session_ctx() as s:
            row = await s.scalar(
                sa.select(SyncRun).where(SyncRun.company_id == cid, SyncRun.entity == "orders")
            )
            seen["status"] = getattr(row, "status", None)
            seen["finished"] = getattr(row, "finished_at", "norow")

    res = await run_sync(_Stub(on_run=_mid), ConnectorContext(company_id=cid, access_token="t"), "orders")
    assert not res.errors
    assert seen["status"] == "running"
    assert seen["finished"] is None
    rows = await _rows(cid)
    assert len(rows) == 1  # begin + finish collapse to ONE row (update, not a second insert)
    assert rows[0].status == "success" and rows[0].finished_at is not None
    assert rows[0].created_count == 2 and rows[0].updated_count == 1


async def test_concurrency_guard_refuses_second_run(_db_engine):
    cid = _cid()
    async with get_session_ctx() as s:
        s.add(SyncRun(
            company_id=cid, connector="stub_lifecycle", entity="orders", started_at=datetime.now(timezone.utc), finished_at=None,
            created_count=0, updated_count=0, skipped_count=0, errors_json=None, status="running",
        ))
        await s.commit()

    ran = {"v": False}

    async def _mark():
        ran["v"] = True

    res = await run_sync(_Stub(on_run=_mark), ConnectorContext(company_id=cid, access_token="t"), "orders")
    assert ran["v"] is False  # the sync body never ran - a run is already in progress
    assert res.errors and "already in progress" in res.errors[0]
    rows = await _rows(cid)
    assert len(rows) == 1  # no second row created


async def test_failure_records_failed_status(_db_engine):
    cid = _cid()
    res = await run_sync(_Stub(boom=True), ConnectorContext(company_id=cid, access_token="t"), "orders")
    assert res.errors and "api 500" in res.errors[0]
    rows = await _rows(cid)
    assert len(rows) == 1
    assert rows[0].status == "failed" and rows[0].finished_at is not None
