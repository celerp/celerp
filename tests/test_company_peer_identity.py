"""Restored source identities must map to fresh target IDs before sync."""
from datetime import datetime, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.services.peer_identity import seed_restored_identities, peer_identity


@pytest.mark.asyncio
async def test_restored_contact_identity_reuses_new_local_record():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Company.__table__.create)
        await conn.run_sync(Projection.__table__.create)
        await conn.run_sync(CompanyPeerIdentity.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    target, source = uuid.uuid4(), uuid.uuid4()
    old, new = "contact:original", "contact:" + str(uuid.uuid4())
    when = datetime(2026, 10, 12, tzinfo=timezone.utc)
    try:
        async with sessions() as session:
            session.add(Company(id=target, name="Accountant", slug="accountant", settings={}))
            session.add(Projection(company_id=target, entity_id=new, entity_type="contact",
                                   state={"name": "Company"}, version=4, updated_at=when))
            await session.commit()
        async with sessions() as session:
            inserted = await seed_restored_identities(session, target_company=target,
                                  source_company=source, id_map={old: new})
            assert inserted == 1
            assert await peer_identity(session, target_company=target, source_company=source,
                                       entity_type="contact", source_entity_id=old) == new
            assert await peer_identity(session, target_company=target, source_company=uuid.uuid4(),
                                       entity_type="contact", source_entity_id=old) is None
            await session.commit()
        async with sessions() as session:
            result = await session.get(CompanyPeerIdentity, (await session.execute(
                select(CompanyPeerIdentity.id))).scalar_one())
            assert result.last_revision == 4
            assert result.source_entity_id == old
            assert result.target_entity_id == new
            await session.delete(await session.get(Projection, (target, new)))
            await session.commit()
        async with sessions() as session:
            with pytest.raises(HTTPException, match="no longer matches"):
                await peer_identity(session, target_company=target, source_company=source,
                                    entity_type="contact", source_entity_id=old)
    finally:
        await engine.dispose()
