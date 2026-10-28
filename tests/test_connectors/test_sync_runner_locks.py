# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A connector run holds no lock on the company while it syncs, so the company's own
writes (a person editing an item, an order import, a publish) go ahead during it.

Real Postgres: the probe runs in its own session while the sync method is running,
with the request lock timeout every app connection carries.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext, SyncEntity, SyncResult
from celerp.connectors.sync_runner import run_sync
from celerp.models.connector_config import ConnectorConfig
from celerp.models.connector_source import ConnectorSource
from connector_test_support import record_first_sync
from celerp.services.company_lock import lock_company
from connector_test_support import add_item, seed_company

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def company(committed_engine, monkeypatch):
    """A committed company in a database of the test's own. Every app session opens its
    own connection there, with the lock timeout every app connection carries, so the
    sync and the probe contend as they do in production."""
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.db import REQUEST_CONNECT_ARGS

    engine = create_async_engine(committed_engine.url, poolclass=NullPool,
                                 connect_args=REQUEST_CONNECT_ARGS)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def session_ctx():
        async with factory() as session:
            yield session

    monkeypatch.setattr("celerp.db.get_session_ctx", session_ctx)
    monkeypatch.setattr("celerp.db.SessionLocal", factory)
    async with session_ctx() as session:
        cid = await seed_company(session, f"Lock{uuid.uuid4().hex[:6]}")
        await add_item(session, cid, "item:edit", "EDIT-1")
        await session.commit()
    yield cid
    await engine.dispose()


def get_session_ctx():
    import celerp.db

    return celerp.db.get_session_ctx()


async def _connect(cid, connector: str, store: str) -> ConnectorContext:
    async with get_session_ctx() as session:
        await session.execute(sa.delete(ConnectorConfig).where(ConnectorConfig.connector == connector))
        config = ConnectorConfig(company_id=str(cid), connector=connector)
        session.add(config)
        await session.flush()
        await record_first_sync(session, config, "completed")
        await session.commit()
    return ConnectorContext(company_id=str(cid), access_token="k:s", store_handle=store)


async def _probe_company_lock(cid) -> None:
    async with get_session_ctx() as session:
        await lock_company(session, cid)
        await session.rollback()


async def _run_with(monkeypatch, connector, ctx, entity: str, probe) -> SyncResult:
    seen: dict = {}

    async def sync(self, ctx, since=None, **kwargs):
        try:
            await probe()
        except Exception as exc:  # the probe's failure is the finding
            seen["error"] = exc
        return SyncResult(entity=SyncEntity(entity), created=1)

    monkeypatch.setattr(type(connector), f"sync_{entity}", sync)
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(return_value=ctx))
    result = await run_sync(connector, ctx, entity)
    assert "error" not in seen, seen.get("error")
    return result


async def test_sync_method_runs_without_company_row_lock(company, monkeypatch):
    from celerp.connectors.woocommerce import WooCommerceConnector

    ctx = await _connect(company, "woocommerce", "https://shop.example.com")
    result = await _run_with(monkeypatch, WooCommerceConnector(), ctx, "orders",
                             lambda: _probe_company_lock(company))
    assert result.ok
    async with get_session_ctx() as session:
        bound = await session.get(ConnectorSource, (str(company), "woocommerce"))
    assert bound is not None and bound.store_handle == "https://shop.example.com"


async def test_quickbooks_sync_holds_no_company_lock(company, monkeypatch):
    from celerp.connectors.quickbooks import QuickBooksConnector

    ctx = await _connect(company, "quickbooks", "1234567890")
    result = await _run_with(monkeypatch, QuickBooksConnector(), ctx, "orders",
                             lambda: _probe_company_lock(company))
    assert result.ok


async def test_item_edit_during_sync_succeeds(company, monkeypatch):
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.events.engine import emit_event

    async def edit_item() -> None:
        async with get_session_ctx() as session:
            await lock_company(session, company)
            await emit_event(
                session, company_id=company, entity_id="item:edit", entity_type="item",
                event_type="item.updated",
                data={"fields_changed": {"name": {"old": "EDIT-1", "new": "Edited during sync"}}},
                actor_id=None, location_id=None, source="api",
                idempotency_key=f"edit:{uuid.uuid4()}", metadata_={},
            )
            await session.commit()

    ctx = await _connect(company, "woocommerce", "https://shop.example.com")
    await _run_with(monkeypatch, WooCommerceConnector(), ctx, "orders", edit_item)
    async with get_session_ctx() as session:
        state = await session.scalar(sa.text(
            "SELECT state FROM projections WHERE company_id = :c AND entity_id = 'item:edit'"
        ), {"c": company})
    assert state["name"] == "Edited during sync"


async def test_a_bound_store_is_not_checked_again(company, monkeypatch):
    """Once a connection is bound to its store, later runs only read the binding."""
    from celerp.connectors.woocommerce import WooCommerceConnector

    ctx = await _connect(company, "woocommerce", "https://shop.example.com")
    await _run_with(monkeypatch, WooCommerceConnector(), ctx, "orders", AsyncMock())
    same_store = AsyncMock(return_value=True)
    monkeypatch.setattr(WooCommerceConnector, "same_store", same_store)
    await _run_with(monkeypatch, WooCommerceConnector(), ctx, "orders", AsyncMock())
    same_store.assert_not_awaited()
