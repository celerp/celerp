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

from celerp.connectors.base import ConnectorContext, EntityFlow, SyncEntity, SyncResult
from celerp.db import get_session_ctx
from celerp.models.sync_run import SyncRun
from connector_test_support import connected
from migration_support import real_engine  # noqa: F401

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
    monkeypatch.setattr(
        "celerp.connectors.relay_token.fetch_context",
        AsyncMock(side_effect=lambda company_id, *_a, **_k: ConnectorContext(
            company_id=str(company_id), access_token="t")),
    )


@pytest.fixture(autouse=True)
def _committed_state_cleared(real_engine):
    """A run commits through the app's own sessions (connections, runs, the execution
    lease); real_engine truncates all of it after each test, so a stub connector
    connected in one test is not connected to another company in the next."""


async def _run(stub, cid, entity="orders", **kwargs):
    """``stub`` synced for company ``cid``, connected on first use."""
    from celerp.connectors.sync_runner import run_sync
    from celerp.models.connector_first_sync import ConnectorFirstSync

    async with get_session_ctx() as s:
        new = await s.get(ConnectorFirstSync, (cid, stub.name)) is None
    if new:
        await connected(cid, stub, stub.default_directions)
    return await run_sync(stub, cid, entity, purpose="manual_sync", **kwargs)


class _Stub:
    name = "stub_lifecycle"
    store_scoped_ids = False
    entity_flows = {"orders": EntityFlow(inbound=("orders",))}
    default_directions = {"orders": "inbound"}

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

    res = await _run(_Stub(on_run=_mid), cid)
    assert not res.errors
    assert seen["status"] == "running"
    assert seen["finished"] is None
    rows = await _rows(cid)
    assert len(rows) == 1  # begin + finish collapse to ONE row (update, not a second insert)
    assert rows[0].status == "success" and rows[0].finished_at is not None
    assert rows[0].created_count == 2 and rows[0].updated_count == 1


async def test_concurrency_guard_refuses_second_run(_db_engine):
    """A second run of a resource whose lease is held is refused before its body runs."""
    import asyncio

    from celerp.connectors.execution import ExecutionBusy

    cid = _cid()
    ran = {"second": False}

    async def _mark():
        ran["second"] = True

    async def _second():
        with pytest.raises(ExecutionBusy):
            await asyncio.create_task(_run(_Stub(on_run=_mark), cid))

    res = await _run(_Stub(on_run=_second), cid)
    assert not res.errors
    assert ran["second"] is False  # the second body never ran
    rows = await _rows(cid)
    assert len(rows) == 1  # no second row created


async def test_failure_records_failed_status(_db_engine):
    cid = _cid()
    res = await _run(_Stub(boom=True), cid)
    assert res.errors and "api 500" in res.errors[0]
    rows = await _rows(cid)
    assert len(rows) == 1
    assert rows[0].status == "failed" and rows[0].finished_at is not None


class _AttentionStub:
    """A connector whose sync takes carried attention and returns a new list."""
    name = "stub_attention"
    store_scoped_ids = False
    entity_flows = {"orders": EntityFlow(inbound=("orders",))}
    default_directions = {"orders": "inbound"}

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

    first = await _run(stub, cid)
    assert not first.errors
    rows = await _rows(cid)
    assert len(rows) == 1 and rows[0].status == "success"
    assert rows[0].attention == waiting

    second = await _run(stub, cid)
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

    await _run(stub, cid)
    crashed = await _run(stub, cid)
    assert crashed.errors and "connection refused" in crashed.errors[0]
    assert await attention_entries(cid, stub.name, "orders") == waiting
    assert await attention_entries(cid, stub.name) == [{**waiting[0], "entity": "orders"}]

    await _run(stub, cid)
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
    await _run(stub, cid)

    monkeypatch.setattr(
        "celerp.connectors.sync_runner.attention_entries",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    failed = await _run(stub, cid)

    assert failed.errors and "database unavailable" in failed.errors[0]
    assert stub.received == [[]]
    assert await attention_entries(cid, stub.name, "orders") == waiting


async def test_a_reset_starts_the_attention_list_over(_db_engine):
    """Disconnect or reconnect records a reset; entries from before it are no
    longer shown or carried (the run's carried list comes from the same reader)."""
    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY, attention_entries

    cid = _cid()
    stub = _AttentionStub([[{"id": "7", "label": "Order 7", "reason": "no stock"}]])
    await _run(stub, cid)
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
    await _run(stub, cid)

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


class _ProductsStub:
    """Products both ways; records the context and ``since`` each call receives."""
    name = "stub_products"
    store_scoped_ids = False
    entity_flows = {"products": EntityFlow(inbound=("products",), outbound=("products_out",))}
    default_directions = {"products": "both"}

    def __init__(self):
        self.calls: list[tuple] = []

    async def sync_products(self, ctx, since=None):
        self.calls.append(("products", ctx.access_token, since))
        return SyncResult(entity=SyncEntity.PRODUCTS, created=1)

    async def sync_products_out(self, ctx):
        self.calls.append(("products_out", ctx.access_token))
        return SyncResult(entity=SyncEntity.PRODUCTS, created=1)


async def test_run_sync_feeds_the_watermark_as_since(_db_engine):
    """An incremental pull starts from the last successful run, so a scheduled or
    webhook sync never degrades to a full pull."""
    cid = _cid()
    stub = _ProductsStub()
    watermark = datetime(2026, 5, 1, tzinfo=timezone.utc)
    async with get_session_ctx() as s:
        s.add(SyncRun(company_id=cid, connector=stub.name, entity="products", direction="inbound",
                      started_at=watermark, finished_at=watermark, status="success"))
        await s.commit()
    await _run(stub, cid, "products")
    assert stub.calls == [("products", "t", watermark)]


async def test_run_sync_dispatches_the_outbound_entity_and_its_direction_gates_it(_db_engine):
    """An outbound entity runs its *_out method while its direction pushes, and is
    blocked, without the method running, once the direction is inbound only."""
    from celerp.connectors.sync_runner import run_sync
    from celerp.models.connector_config import ConnectorConfig

    cid = _cid()
    stub = _ProductsStub()
    pushed = await _run(stub, cid, "products_out")
    assert pushed.created == 1 and stub.calls == [("products_out", "t")]

    async with get_session_ctx() as s:
        await s.execute(sa.update(ConnectorConfig).where(
            ConnectorConfig.company_id == cid, ConnectorConfig.connector == stub.name,
        ).values(entity_directions={"products": "inbound"}))
        await s.commit()
    blocked = await run_sync(stub, cid, "products_out", purpose="manual_sync")
    assert blocked.errors and "blocked by direction" in blocked.errors[0]
    assert stub.calls == [("products_out", "t")]


async def test_run_sync_reads_the_store_context_from_its_execution(_db_engine, monkeypatch):
    """The sync method gets the context the execution fetched for this run, never
    one a caller carried in."""
    from unittest.mock import AsyncMock

    cid = _cid()
    fetch = AsyncMock(return_value=ConnectorContext(company_id=cid, access_token="fresh"))
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", fetch)
    stub = _ProductsStub()
    await _run(stub, cid, "products", use_watermark=False)
    assert stub.calls == [("products", "fresh", None)]
    fetch.assert_awaited_once_with(cid, stub.name)
