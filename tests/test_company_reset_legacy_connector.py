# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A company reset never leaves one company standing beside a connector set up before
companies had their own, since the next startup would hand it to that company."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio
LEGACY = "inst-legacy-connector"
NAMES = ["Harbor Goods Ltd", "Hillside Supply Co", "Riverside Traders"]


async def _rows(engine):
    async with engine.connect() as c:
        return [tuple(r) for r in (await c.execute(text(
            "SELECT company_id, connector, webhook_secret FROM connector_configs ORDER BY id"))).all()]


@pytest.fixture
async def legacy_install(real_engine, monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.connectors import outbound_queue
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())
    monkeypatch.setattr(settings, "gateway_instance_id", LEGACY)

    @asynccontextmanager
    async def _ctx():
        async with maker(real_engine)() as s:
            yield s
    monkeypatch.setattr(outbound_queue, "get_session_ctx", _ctx)

    async def make(n_companies: int):
        from celerp.models.connector_config import ConnectorConfig
        boss = await owner(real_engine)
        ids = [await company(real_engine, boss, NAMES[i], f"m{i}") for i in range(n_companies)]
        async with maker(real_engine)() as s:
            s.add(ConnectorConfig(company_id=LEGACY, connector="woocommerce", webhook_secret="legacy-secret"))
            await s.commit()
        return boss, ids
    return make


@pytest.mark.parametrize("n_companies", [1, 2])
async def test_reset_that_would_leave_one_company_beside_a_legacy_connector_is_refused(
        real_engine, real_client, legacy_install, n_companies):
    from celerp.connectors import outbound_queue
    boss, ids = await legacy_install(n_companies)
    r = await real_client.post("/companies/me/reset", json={"company_name": NAMES[0]},
                               headers=auth(await token(real_engine, boss, ids[0])))
    assert r.status_code == 409, r.text
    assert "woocommerce" in r.json()["detail"] and "Nothing was deleted" in r.json()["detail"]
    await outbound_queue.adopt_legacy_connector_configs()
    # With one company the connector can only be that company's (the normal upgrade); with
    # two it waits for whoever connects it.
    owner_now = str(ids[0]) if n_companies == 1 else LEGACY
    assert await _rows(real_engine) == [(owner_now, "woocommerce", "legacy-secret")]


async def test_reset_that_leaves_two_companies_keeps_the_legacy_connector_unassigned(
        real_engine, real_client, legacy_install):
    from celerp.connectors import outbound_queue
    boss, ids = await legacy_install(3)
    r = await real_client.post("/companies/me/reset", json={"company_name": NAMES[0]},
                               headers=auth(await token(real_engine, boss, ids[0])))
    assert r.status_code == 200, r.text
    await outbound_queue.adopt_legacy_connector_configs()
    assert await _rows(real_engine) == [(LEGACY, "woocommerce", "legacy-secret")]
