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
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _active_company_guard(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "celerp.connectors.ownership._lock_active_company",
        AsyncMock(return_value=None),
    )


class _Stub:
    name = "stub_lifecycle"
    store_scoped_ids = False
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


class _AttentionStub:
    """A connector whose sync takes carried attention and returns a new list."""
    name = "stub_attention"
    store_scoped_ids = False
    direction = SyncDirection.BOTH

    def __init__(self, returns):
        self._returns = list(returns)
        self.received = []

    async def sync_orders(self, ctx, since=None, attention=None):
        self.received.append(attention)
        return SyncResult(entity=SyncEntity.ORDERS, created=1, attention=self._returns.pop(0))


async def test_attention_is_persisted_and_carried_to_the_next_run(_db_engine):
    """Orders waiting on a person ride along on the run row, reach the next run
    as its carried list, and the run itself still succeeds so the watermark can
    advance; a run that clears them leaves no attention behind."""
    cid = _cid()
    waiting = [{"id": "7", "label": "Order 7", "reason": "no stock"}]
    stub = _AttentionStub([waiting, []])
    ctx = ConnectorContext(company_id=cid, access_token="t")

    first = await run_sync(stub, ctx, "orders")
    assert not first.errors
    rows = await _rows(cid)
    assert len(rows) == 1 and rows[0].status == "success"
    assert rows[0].attention == waiting

    second = await run_sync(stub, ctx, "orders")
    assert not second.errors
    assert stub.received == [[], waiting]
    latest = max(await _rows(cid), key=lambda r: r.started_at)
    assert latest.status == "success" and latest.attention == []


class _FlakyAttentionStub(_AttentionStub):
    """Like _AttentionStub, but a ``RuntimeError`` in the returns list is raised,
    the way a transport error escapes a connector's sync."""

    async def sync_orders(self, ctx, since=None, attention=None):
        self.received.append(attention)
        value = self._returns.pop(0)
        if isinstance(value, Exception):
            raise value
        return SyncResult(entity=SyncEntity.ORDERS, created=1, attention=value)


async def test_a_run_that_produces_no_list_keeps_the_previous_one(_db_engine):
    """A run stopped by the ownership guard, or by an error escaping the sync,
    returns no list; the orders already waiting stay on the list and reach the
    next run instead of being forgotten."""
    from celerp.connectors.sync_runner import attention_entries

    cid = _cid()
    waiting = [{"id": "7", "label": "Order 7", "reason": "no stock"}]
    stub = _FlakyAttentionStub([waiting, RuntimeError("connection refused"), []])
    ctx = ConnectorContext(company_id=cid, access_token="t")

    await run_sync(stub, ctx, "orders")
    guarded = await run_sync(stub, ctx, "orders", expected_config_id=uuid.uuid4())
    assert guarded.errors and "connection changed" in guarded.errors[0]
    crashed = await run_sync(stub, ctx, "orders")
    assert crashed.errors and "connection refused" in crashed.errors[0]
    assert await attention_entries(cid, stub.name, "orders") == waiting
    assert await attention_entries(cid, stub.name) == waiting

    await run_sync(stub, ctx, "orders")
    assert stub.received == [[], waiting, waiting]
    assert await attention_entries(cid, stub.name) == []


async def test_attention_read_failure_fails_the_run(_db_engine, monkeypatch):
    """A sync that cannot read the list fails without calling the connector, so
    it never replaces the list with an empty one."""
    from unittest.mock import AsyncMock

    from celerp.connectors.sync_runner import attention_entries

    cid = _cid()
    waiting = [{"id": "7", "label": "Order 7", "reason": "no stock"}]
    stub = _AttentionStub([waiting])
    ctx = ConnectorContext(company_id=cid, access_token="t")
    await run_sync(stub, ctx, "orders")

    monkeypatch.setattr(
        "celerp.connectors.sync_runner.attention_entries",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    failed = await run_sync(stub, ctx, "orders")

    assert failed.errors and "database unavailable" in failed.errors[0]
    assert stub.received == [[]]
    assert await attention_entries(cid, stub.name, "orders") == waiting


async def test_a_reset_starts_the_attention_list_over(_db_engine):
    """Disconnect or reconnect records a reset; entries from before it are no
    longer shown or carried (the run's carried list comes from the same reader)."""
    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY, attention_entries

    cid = _cid()
    stub = _AttentionStub([[{"id": "7", "label": "Order 7", "reason": "no stock"}]])
    ctx = ConnectorContext(company_id=cid, access_token="t")
    await run_sync(stub, ctx, "orders")
    assert await attention_entries(cid, stub.name, "orders")
    async with get_session_ctx() as s:
        s.add(SyncRun(
            company_id=cid, connector=stub.name, entity=CONNECTOR_RESET_ENTITY,
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
            created_count=0, updated_count=0, skipped_count=0, status="success",
        ))
        await s.commit()

    assert await attention_entries(cid, stub.name) == []
    assert await attention_entries(cid, stub.name, "orders") == []


async def test_update_attention_entry_edits_the_current_list(_db_engine):
    """One entry of the current list changes in the caller's transaction; an
    unknown id changes nothing, and an update that raises saves nothing."""
    from celerp.connectors.sync_runner import attention_entries, update_attention_entry

    cid = _cid()
    waiting = [
        {"id": "7", "label": "Order 7", "reason": "refund"},
        {"id": "8", "label": "Order 8", "reason": "no stock"},
    ]
    stub = _AttentionStub([waiting])
    await run_sync(stub, ConnectorContext(company_id=cid, access_token="t"), "orders")

    def _mark(entry):
        entry["reconciled"] = True

    def _refuse(entry):
        raise ValueError("refused")

    async with get_session_ctx() as s:
        assert await update_attention_entry(s, cid, stub.name, "orders", "9", _mark) is None
        with pytest.raises(ValueError):
            await update_attention_entry(s, cid, stub.name, "orders", "8", _refuse)
        await s.rollback()
    async with get_session_ctx() as s:
        entry = await update_attention_entry(s, cid, stub.name, "orders", "7", _mark)
        await s.commit()
    assert entry == {**waiting[0], "reconciled": True}
    assert await attention_entries(cid, stub.name, "orders") == [entry, waiting[1]]
