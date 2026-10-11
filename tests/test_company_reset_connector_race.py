# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company reset and connector work never overlap, whichever starts first: a connector
connected while the reset waits makes the reset refuse, and connector work that starts
while a reset runs waits for it and then finds the company gone."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector_ownership
from celerp.services.company_lock import hold_company
from company_backup_support import snapshot, token
from migration_support import auth, count, maker, real_client, real_engine  # noqa: F401
from test_company_reset import RESET, _local_files, _two_companies

pytestmark = pytest.mark.asyncio

NAME = "Harbor Goods Ltd"
_WAITING = ("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND wait_event IN ({events})")


async def _queued(engine, task: asyncio.Task, events: str) -> None:
    """Wait until *task* is blocked on a lock of one of the given wait *events*."""
    async with engine.connect() as conn:
        for _ in range(250):
            if task.done():
                pytest.fail(f"finished without waiting: {task.result()!r}")
            if (await conn.execute(text(_WAITING.format(events=events)))).scalar_one():
                return
            await conn.rollback()
            await asyncio.sleep(0.02)
    pytest.fail("never queued on a lock")


async def test_a_connector_connected_while_the_reset_waits_makes_it_refuse(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    connecting = maker(real_engine)()
    try:
        await claim_connector_ownership(connecting, a, "shopify")
        before = await snapshot(real_engine)
        reset = asyncio.create_task(real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok)))
        await _queued(real_engine, reset, "'advisory'")
        await connecting.commit()
        r = await reset
    finally:
        await connecting.close()

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Disconnect shopify before resetting this company. Nothing was deleted."
    after = await snapshot(real_engine)
    assert {t: v for t, v in after.items() if t != "connector_configs"} == \
        {t: v for t, v in before.items() if t != "connector_configs"}
    assert await count(real_engine, "connector_configs", "company_id = :c", c=str(a)) == 1


async def test_connector_work_started_during_a_reset_waits_and_finds_the_company_gone(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)

    async def connect() -> str:
        async with maker(real_engine)() as s:
            try:
                await claim_connector_ownership(s, a, "shopify")
            except ConnectorOwnershipError as exc:
                await s.rollback()
                return str(exc)
            await s.commit()
            return "connected"

    holder = maker(real_engine)()
    try:
        # A file being stored for the company keeps the reset waiting after it has
        # taken the connector maintenance lock.
        assert await hold_company(holder, a)
        reset = asyncio.create_task(real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok)))
        await _queued(real_engine, reset, "'transactionid', 'tuple'")
        connector = asyncio.create_task(connect())
        await _queued(real_engine, connector, "'advisory'")
        await holder.commit()
        r = await reset
    finally:
        await holder.close()

    assert r.status_code == 200, r.text
    from ui.i18n import t
    assert await connector == t("error.connector_company_inactive", "en", service="Shopify")
    assert await count(real_engine, "companies", "id = :c", c=a) == 0
    assert await count(real_engine, "connector_configs") == 0
