# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The daily reconciliation runs only connections whose first sync is done and that
are not set to sync by hand, with each kind of record in its own direction."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from celerp.connectors.base import ConnectorContext, SyncEntity, SyncResult
from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
from celerp.db import get_session_ctx
from celerp.models.connector_config import ConnectorConfig
from celerp.models.connector_first_sync import ConnectorFirstSync
from connector_test_support import record_first_sync
from migration_support import real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

_WOO = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}


@pytest.fixture(autouse=True)
def _active_company_guard(monkeypatch):
    monkeypatch.setattr(
        "celerp.connectors.ownership._lock_active_company", AsyncMock(return_value=None)
    )


@pytest.fixture
def run(monkeypatch):
    mock = AsyncMock(return_value=[SyncResult(entity=SyncEntity.ORDERS, created=1)])
    monkeypatch.setattr("celerp.connectors.sync_runner.run_connector_sync", mock)
    return mock


@pytest.fixture
def _config(real_engine):
    """Add one company's connection, the only one for its connector while the test runs;
    real_engine truncates what the test commits."""
    import sqlalchemy as sa

    async def add(connector: str, *, first_sync: str, **fields) -> str:
        company_id = str(uuid.uuid4())
        async with get_session_ctx() as session:
            await session.execute(sa.delete(ConnectorConfig).where(ConnectorConfig.connector == connector))
            await session.execute(sa.delete(ConnectorFirstSync).where(ConnectorFirstSync.connector == connector))
            await session.commit()
            config = ConnectorConfig(company_id=company_id, connector=connector, **fields)
            session.add(config)
            await session.flush()
            await record_first_sync(session, config, first_sync)
            await session.commit()
        return company_id

    return add


async def test_no_reconcile_before_first_sync(_config, run):
    company_id = await _config("woocommerce", entity_directions=_WOO, first_sync="pending")
    synced = await check_and_run_daily_syncs(company_id)
    assert synced == []
    run.assert_not_awaited()


async def test_manual_frequency_not_reconciled(_config, run):
    company_id = await _config(
        "quickbooks", sync_frequency="manual", first_sync="completed",
        entity_directions={"products": "inbound", "invoices": "both", "contacts": "inbound"},
    )
    synced = await check_and_run_daily_syncs(company_id)
    assert synced == []
    run.assert_not_awaited()


async def test_reconcile_uses_entity_directions(_config, monkeypatch):
    ran: list[str] = []

    async def run_sync(connector, company_id, entity, **kwargs):
        ran.append(entity)
        assert kwargs["reconcile"] is True
        return SyncResult(entity=entity, created=1)

    monkeypatch.setattr("celerp.connectors.sync_runner.run_sync", run_sync)
    company_id = await _config(
        "woocommerce", entity_directions=_WOO, first_sync="completed",
    )
    ctx = ConnectorContext(company_id=company_id, access_token="k:s", store_handle="https://shop.example.com")
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(return_value=ctx))
    synced = await check_and_run_daily_syncs(company_id)
    assert synced == ["woocommerce"]
    assert ran == ["orders", "products_out", "inventory_out"]
