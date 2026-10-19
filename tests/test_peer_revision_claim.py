"""Peer source progression is shared by every canonical receiving domain."""
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.services.peer_identity import (
    claim_peer_revision, commit_peer_revision, new_target_identity,
)


@pytest.mark.asyncio
async def test_claim_requires_live_target_before_acknowledging_replayed_revision():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Company.__table__.create)
            await conn.run_sync(Projection.__table__.create)
            await conn.run_sync(CompanyPeerIdentity.__table__.create)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        target, source = uuid.uuid4(), uuid.uuid4()
        original = "item:source"
        grant = SimpleNamespace(company_id=target, source_company_id=source)
        async with maker() as session:
            session.add(Company(id=target, slug="accounting", name="Accounting", settings={}))
            claim = await claim_peer_revision(session, grant=grant, entity_type="item",
                                             source_entity_id=original, source_revision=4,
                                             data={"name": "Product"})
            assert not claim.replay and claim.mapping is None
            assert claim.target_id == new_target_identity(source, "item", original)
            row = Projection(company_id=target, entity_id=claim.target_id, entity_type="item",
                             state={"name": "Product"}, version=4,
                             updated_at=datetime(2026, 10, 13, tzinfo=timezone.utc))
            session.add(row)
            await commit_peer_revision(session, grant=grant, claim=claim,
                                       entity_type="item", source_entity_id=original,
                                       source_revision=4)
            await session.commit()
            again = await claim_peer_revision(session, grant=grant, entity_type="item",
                                              source_entity_id=original, source_revision=4,
                                              data={"name": "Product"})
            assert again.replay
            with pytest.raises(HTTPException) as reused:
                await claim_peer_revision(session, grant=grant, entity_type="item",
                                          source_entity_id=original, source_revision=4,
                                          data={"name": "Changed"})
            assert reused.value.status_code == 409
            await session.delete(row)
            await session.commit()
        async with maker() as session:
            with pytest.raises(HTTPException) as missing:
                await claim_peer_revision(session, grant=grant, entity_type="item",
                                          source_entity_id=original, source_revision=4,
                                          data={"name": "Product"})
            assert missing.value.status_code == 409
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_rejects_inactive_and_staged_company():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Company.__table__.create)
            await conn.run_sync(Projection.__table__.create)
            await conn.run_sync(CompanyPeerIdentity.__table__.create)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        target, source = uuid.uuid4(), uuid.uuid4()
        grant = SimpleNamespace(company_id=target, source_company_id=source)
        async with maker() as session:
            company = Company(id=target, name="Firm", slug="firm", settings={},
                              is_active=False)
            session.add(company)
            await session.flush()
            for active, staged in ((False, False), (True, True)):
                company.is_active = active
                company.is_migration_staged = staged
                await session.flush()
                with pytest.raises(HTTPException) as blocked:
                    await claim_peer_revision(session, grant=grant, entity_type="item",
                                              source_entity_id="item:x", source_revision=1,
                                              data={"name": "X"})
                assert blocked.value.status_code == 403
    finally:
        await engine.dispose()



@pytest.mark.asyncio
async def test_first_financial_sync_rejects_populated_unrelated_company():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            for table in (Company.__table__, Projection.__table__, CompanyPeerIdentity.__table__):
                await connection.run_sync(table.create)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        target, source = uuid.uuid4(), uuid.uuid4()
        original = "doc:existing"
        grant = SimpleNamespace(company_id=target, source_company_id=source)
        async with maker() as session:
            session.add(Company(id=target, slug="original-books", name="Original Books",
                                settings={}))
            session.add(Projection(
                company_id=target, entity_id=original, entity_type="doc",
                state={"doc_type": "invoice", "status": "final", "total": 100},
                version=1, updated_at=datetime.now(timezone.utc)))
            await session.commit()
            with pytest.raises(HTTPException) as unsafe:
                await claim_peer_revision(
                    session, grant=grant, entity_type="doc",
                    source_entity_id="doc:incoming", source_revision=1,
                    data={"doc_type": "invoice", "status": "draft"})
            assert unsafe.value.status_code == 409
            assert "already contains documents" in unsafe.value.detail
            await session.rollback()

            from celerp.services.peer_identity import _digest
            session.add(CompanyPeerIdentity(
                company_id=target, source_company_id=source,
                entity_type="doc", source_entity_id=original,
                source_key_hash=_digest(original), target_entity_id=original,
                last_revision=1))
            await session.commit()
            allowed = await claim_peer_revision(
                session, grant=grant, entity_type="doc",
                source_entity_id="doc:new", source_revision=1,
                data={"doc_type": "invoice", "status": "draft"})
            assert not allowed.replay and allowed.mapping is None
            assert allowed.target_id == new_target_identity(source, "doc", "doc:new")
    finally:
        await engine.dispose()
