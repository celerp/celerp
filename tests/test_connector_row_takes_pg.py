# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every writer of a connection row takes it under its company's lock, taken first, and names
the company in the statement that locks it; a legacy row (keyed by the instance id) belongs to
no company. The lock checker's census is the witness: no take of the connector rows outside
the company lock (tests/lock_rank.py R4)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from connector_test_support import connect, connector_session, seed_company, ungated_connector_takes  # noqa: F401

pytestmark = pytest.mark.asyncio

_WRITERS = "celerp/connectors/"


async def test_a_connect_lapse_is_recorded_under_the_company_lock(connector_session):
    from celerp.connectors.relay_token import _note_connect

    session = connector_session
    cid = await seed_company(session, "Lapse")
    config = await connect(session, cid, "woocommerce", first_sync="completed")
    with ungated_connector_takes(_WRITERS) as ungated:
        await _note_connect(str(cid), "woocommerce", lapsed=True)

        await session.refresh(config)
        assert config.connect_lapsed_at is not None
    assert ungated == set()


async def test_a_daily_sync_is_marked_under_the_company_lock(connector_session, monkeypatch):
    from celerp.connectors.base import SyncEntity, SyncResult
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs

    session = connector_session
    cid = await seed_company(session, "Daily")
    config = await connect(session, cid, "woocommerce", first_sync="completed", sync_frequency="daily")
    monkeypatch.setattr("celerp.connectors.sync_runner.run_connector_sync",
                        AsyncMock(return_value=[SyncResult(entity=SyncEntity.PRODUCTS, updated=1)]))
    with ungated_connector_takes(_WRITERS) as ungated:
        assert await check_and_run_daily_syncs(str(cid)) == ["woocommerce"]

        await session.refresh(config)
        assert config.last_daily_sync_at is not None
    assert ungated == set()


async def test_an_unassigned_connector_is_released_without_a_company_row_take(connector_session, monkeypatch):
    from celerp.connectors.ownership import release_unassigned_connector
    from celerp.models.connector_config import ConnectorConfig

    session = connector_session
    await seed_company(session, "Bystander")
    legacy_id = "inst-unassigned"
    monkeypatch.setattr("celerp.config.ensure_instance_id", lambda: legacy_id)
    monkeypatch.setattr("celerp.connectors.ownership.ensure_instance_id", lambda: legacy_id)
    session.add(ConnectorConfig(company_id=legacy_id, connector="woocommerce"))
    await session.commit()
    with ungated_connector_takes(_WRITERS) as ungated:
        await release_unassigned_connector(session, "woocommerce")
        await session.commit()

        assert await session.scalar(sa.select(sa.func.count()).select_from(ConnectorConfig).where(
            ConnectorConfig.company_id == legacy_id)) == 0
    assert ungated == set()
