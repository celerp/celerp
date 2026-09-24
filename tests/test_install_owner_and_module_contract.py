# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.gateway.client import GatewayClient
from celerp.models.company import User
from celerp.modules.api import ai_query
from test_helpers import invite_user, register_admin


@pytest.mark.asyncio
async def test_install_owner_can_transfer_and_old_owner_loses_authority(client, session):
    admin_token = await register_admin(client)
    admin_h = {"Authorization": f"Bearer {admin_token}"}
    next_token = await invite_user(
        client, session, admin_h, "next-owner@example.test", "owner")
    users = (await client.get("/companies/me/users", headers=admin_h)).json()["items"]
    target = next(u for u in users if u["email"] == "next-owner@example.test")
    current = (await session.execute(
        select(User).where(User.email == "admin@perm.example")
    )).scalar_one()

    r = await client.post(
        f"/companies/me/users/{target['id']}/installation-owner", headers=admin_h)
    assert r.status_code == 200, r.text

    old_retry = await client.post(
        f"/companies/me/users/{current.id}/installation-owner", headers=admin_h)
    assert old_retry.status_code == 403

    new_h = {"Authorization": f"Bearer {next_token}"}
    deactivate_owner = await client.patch(
        f"/companies/me/users/{target['id']}",
        headers=new_h,
        json={"is_active": False},
    )
    assert deactivate_owner.status_code == 400
    assert "installation owner" in deactivate_owner.json()["detail"].lower()

    transfer_back = await client.post(
        f"/companies/me/users/{current.id}/installation-owner", headers=new_h)
    assert transfer_back.status_code == 200, transfer_back.text

    rows = (await session.execute(
        select(User).where(User.is_install_owner.is_(True))
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == current.id


@pytest.mark.asyncio
async def test_module_ai_api_keeps_explicit_session_contract(monkeypatch):
    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "session-1")
    run_query = AsyncMock(return_value=SimpleNamespace(
        answer="ok", model_used="test", tools_called=[]))
    monkeypatch.setattr("celerp.ai.service.run_query", run_query)

    result = await ai_query(
        query="hello", company_id="company-1",
        session_token="session-1", db_session=None)
    assert result["answer"] == "ok"
    run_query.assert_awaited_once()

    with pytest.raises(HTTPException) as exc:
        await ai_query(
            query="hello", company_id="company-1",
            session_token="wrong", db_session=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_invoice_delivery_ack_only_after_local_record(monkeypatch):
    client = GatewayClient("key", "instance", "ws://example.test")

    class _WS:
        def __init__(self):
            self.sent = []
        async def send(self, raw):
            import json
            self.sent.append(json.loads(raw))

    class _Session:
        async def get(self, _model, _key):
            return SimpleNamespace(state={"total": 10})

    @asynccontextmanager
    async def _session_ctx():
        yield _Session()

    record = AsyncMock(return_value=None)
    monkeypatch.setattr("celerp.db.get_session_ctx", _session_ctx)
    monkeypatch.setattr("celerp_docs.routes_payments.record_stripe_payment", record)
    client._ws = _WS()

    await client._handle_invoice_payment({
        "company_id": "company-1", "entity_id": "invoice-1",
        "reference": "pi_1", "amount_minor": 1000, "currency": "USD",
        "delivery_id": "delivery-1",
    })

    record.assert_awaited_once()
    assert client._ws.sent[-1]["type"] == "event.ack"
    assert client._ws.sent[-1]["payload"] == {"delivery_id": "delivery-1"}



@pytest.mark.asyncio
async def test_invoice_delivery_is_not_acked_when_recording_fails(monkeypatch):
    client = GatewayClient("key", "instance", "ws://example.test")

    class _WS:
        def __init__(self):
            self.sent = []
        async def send(self, raw):
            import json
            self.sent.append(json.loads(raw))

    class _Session:
        async def get(self, _model, _key):
            return SimpleNamespace(state={"total": 10})

    @asynccontextmanager
    async def _session_ctx():
        yield _Session()

    record = AsyncMock(side_effect=RuntimeError("write failed"))
    monkeypatch.setattr("celerp.db.get_session_ctx", _session_ctx)
    monkeypatch.setattr("celerp_docs.routes_payments.record_stripe_payment", record)
    client._ws = _WS()

    await client._handle_invoice_payment({
        "company_id": "company-1", "entity_id": "invoice-1",
        "reference": "pi_1", "amount_minor": 1000, "currency": "USD",
        "delivery_id": "delivery-1",
    })

    record.assert_awaited_once()
    assert client._ws.sent == []

def test_install_owner_backfill_uses_existing_data_reconcile_path():
    from celerp.migrations._data_reconcile import data_backfill_scripts

    revisions = {script.revision for script in data_backfill_scripts()}
    assert "h5c6d7e8f9a0" in revisions


def test_install_owner_migration_prefers_usable_owner_over_oldest_user(monkeypatch):
    import datetime as dt
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from celerp.migrations.versions import g4b5c6d7e8f9_add_install_owner as schema_migration
    from celerp.migrations.versions import h5c6d7e8f9a0_backfill_install_owner as data_migration

    engine = sa.create_engine("sqlite:///:memory:")
    meta = sa.MetaData()
    users = sa.Table(
        "users", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    links = sa.Table(
        "user_companies", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("company_id", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
    )
    meta.create_all(engine)
    now = dt.datetime(2026, 1, 1)
    with engine.begin() as conn:
        conn.execute(users.insert(), [
            {"id": "old", "is_active": False, "created_at": now},
            {"id": "admin", "is_active": True, "created_at": now + dt.timedelta(seconds=1)},
            {"id": "owner", "is_active": True, "created_at": now + dt.timedelta(seconds=2)},
        ])
        conn.execute(links.insert(), [
            {"id": "a", "user_id": "admin", "company_id": "c", "role": "admin", "is_active": True},
            {"id": "o", "user_id": "owner", "company_id": "c", "role": "owner", "is_active": True},
        ])
        ctx = MigrationContext.configure(conn)
        operations = Operations(ctx)
        monkeypatch.setattr(schema_migration, "op", operations)
        monkeypatch.setattr(data_migration, "op", operations)
        schema_migration.upgrade()
        data_migration.upgrade()
        data_migration.upgrade()
        selected = conn.execute(sa.text(
            "SELECT id FROM users WHERE is_install_owner IS TRUE"
        )).scalars().all()
    assert selected == ["owner"]
