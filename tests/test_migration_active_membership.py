"""Source-neutral migration writers reuse normal company membership eligibility."""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.importers.sinks import SinkContext
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.migration_core_sink import acting_member


@pytest.mark.asyncio
async def test_migration_actor_rejects_inactive_membership_and_inactive_user():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Company.__table__.create)
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(UserCompany.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        company_id, user_id = uuid.uuid4(), uuid.uuid4()
        async with factory() as session:
            company = Company(id=company_id, name="Migration target",
                              slug="migration-target", settings={"currency": "USD"},
                              is_migration_staged=True)
            user = User(id=user_id, email="migration@example.test", name="Migrator")
            membership = UserCompany(user_id=user_id, company_id=company_id, role="owner")
            session.add_all([company, user, membership])
            await session.commit()
            ctx = SinkContext(session=session, company_id=company_id, user_id=user_id,
                              run_id=uuid.uuid4(), read_attachment=lambda key: b"")
            acting = await acting_member(ctx)
            assert acting.user.id == user_id and acting.role == "owner"
            assert acting.settings["currency"] == "USD"
            membership.is_active = False
            await session.flush()
            with pytest.raises(PermissionError):
                await acting_member(ctx)
            membership.is_active = True
            user.is_active = False
            await session.flush()
            with pytest.raises(PermissionError):
                await acting_member(ctx)
    finally:
        await engine.dispose()
