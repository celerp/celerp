"""Item catalog peer synchronization must not import stock or money."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.routers.company_peers import PeerItemData
from celerp.services.peer_identity import receive_item


@pytest.mark.parametrize("key,value", [
    ("quantity", 11), ("cost_price", 25), ("status", "sold"),
    ("location_id", "warehouse"), ("idempotency_key", "bad"),
    ("source_company_id", str(uuid.uuid4())),
])
def test_peer_item_rejects_fields_that_carry_stock_or_identity(key, value):
    data = {"name": "One", "sku": "ONE", "sell_by": "piece", key: value}
    with pytest.raises(ValidationError):
        PeerItemData.model_validate(data)


@pytest.mark.asyncio
async def test_item_revision_remaps_once_and_updates_through_import_writer(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (Company, Projection, CompanyPeerIdentity):
            await conn.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source, target = uuid.uuid4(), uuid.uuid4()
    grant = SimpleNamespace(company_id=target, source_company_id=source)
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    called = []

    async def writer(session, company_id, actor, role, settings, request, **kwargs):
        assert company_id == target and role == "owner" and actor.id is None
        record = request.records[0]
        assert record.source == "connector"
        assert record.data["name"] and record.data["sku"]
        assert not any(k in record.data for k in ("cost_price", "cost_total", "location_id"))
        projection = await session.get(Projection, (target, record.entity_id))
        if projection is None:
            assert record.event_type == "item.created"
            assert record.data["quantity"] == 0
            assert record.data["status"] == "draft"
            session.add(Projection(company_id=target, entity_id=record.entity_id,
                        entity_type="item", state={k: v for k,v in record.data.items()},
                        version=1, updated_at=now))
            status = "created"
        else:
            assert record.event_type == "item.patched"
            assert "quantity" not in record.data and "status" not in record.data
            projection.state = dict(projection.state, **record.data)
            projection.version += 1
            status = "updated"
        called.append((record.entity_id, record.idempotency_key))
        return SimpleNamespace(records=[SimpleNamespace(status=status)]), None

    monkeypatch.setattr("celerp_inventory.services.write_import_batch", writer)
    base = {"name": "One", "sku": "ONE", "sell_by": "piece"}
    try:
        async with sessions() as session:
            session.add(Company(id=target, name="Firm", slug="firm", settings={},
                                is_active=True, is_migration_staged=False))
            await session.commit()
        async with sessions() as session:
            result = await receive_item(session, grant=grant, source_entity_id="item:origin",
                                        source_revision=3, data=base)
            assert result == "created"
            await session.commit()
        async with sessions() as session:
            assert await receive_item(session, grant=grant, source_entity_id="item:origin",
                                      source_revision=3, data=base) == "skipped"
            with pytest.raises(HTTPException, match="revision already used"):
                await receive_item(session, grant=grant, source_entity_id="item:origin",
                                   source_revision=3, data={**base, "name": "Modified"})
            assert await receive_item(session, grant=grant, source_entity_id="item:origin",
                                      source_revision=2, data=base) == "skipped"
            assert await receive_item(session, grant=grant, source_entity_id="item:origin",
                                      source_revision=4, data={**base, "description": "updated"}) == "updated"
            await session.commit()
        assert len(called) == 2
        assert called[0][0] == called[1][0]
        assert called[0][1] != called[1][1]
        async with sessions() as session:
            from sqlalchemy import select
            ids = (await session.execute(select(CompanyPeerIdentity))).scalars().all()
            assert len(ids) == 1 and ids[0].last_revision == 4
            assert ids[0].source_company_id == source and ids[0].company_id == target
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unavailable_peer_target_never_writes(monkeypatch):
    called = []
    async def writer(*args, **kwargs): called.append(True)
    monkeypatch.setattr("celerp_inventory.services.write_import_batch", writer)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (Company, Projection, CompanyPeerIdentity):
            await conn.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine)
    try:
        async with sessions() as session:
            with pytest.raises(HTTPException, match="Company unavailable"):
                await receive_item(session, grant=SimpleNamespace(
                    company_id=uuid.uuid4(), source_company_id=uuid.uuid4()),
                    source_entity_id="item:none", source_revision=1,
                    data={"name":"Missing","sku":"NA","sell_by":"piece"})
        assert not called
    finally:
        await engine.dispose()
