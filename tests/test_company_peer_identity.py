"""Restored source identities must map to fresh target IDs before sync."""
from datetime import datetime, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.models.company import Company, Location
from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.services.peer_identity import seed_restored_identities, peer_identity


@pytest.mark.asyncio
async def test_restored_contact_identity_reuses_new_local_record():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Company.__table__.create)
        await conn.run_sync(Location.__table__.create)
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


@pytest.mark.asyncio
async def test_receiver_does_not_mutate_company_owned_contact():
    from types import SimpleNamespace
    from celerp.services.peer_identity import receive_contact

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Company.__table__.create)
        await conn.run_sync(Location.__table__.create)
        await conn.run_sync(Projection.__table__.create)
        await conn.run_sync(CompanyPeerIdentity.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    target, source = uuid.uuid4(), uuid.uuid4()
    original_id = "contact:owned-source"
    restored_id = "contact:" + str(uuid.uuid4())
    try:
        async with sessions() as session:
            session.add(Company(id=target, name="Accountant", slug="accountant", settings={}))
            session.add(Projection(
                company_id=target, entity_id=restored_id, entity_type="contact",
                state={"name": "Owner legal identity", "is_self": True},
                version=4, updated_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
            ))
            await session.commit()
        async with sessions() as session:
            await seed_restored_identities(
                session, target_company=target, source_company=source,
                id_map={original_id: restored_id},
            )
            grant = SimpleNamespace(company_id=target, source_company_id=source)
            with pytest.raises(HTTPException) as error:
                await receive_contact(
                    session, grant=grant, source_entity_id=original_id,
                    source_revision=5, data={"name": "Peer-controlled name"},
                )
            assert error.value.status_code == 403
            owner = await session.get(Projection, (target, restored_id))
            assert owner.state["name"] == "Owner legal identity"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_peer_contact_clears_optional_value_without_changing_regular_imports(monkeypatch):
    from types import SimpleNamespace
    from celerp_contacts import services as contacts

    contact = SimpleNamespace(
        entity_id="contact:existing", state={"name": "Client", "email": "old@example.test"},
    )
    class Rows:
        def scalars(self):
            return self
        def all(self):
            return [contact]
    class Session:
        async def execute(self, query):
            return Rows()
    async def no_prior_event(*args, **kwargs):
        return None

    changes = []
    async def record_change(*args, **kwargs):
        changes.append(kwargs["data"]["fields_changed"])
        return SimpleNamespace()

    monkeypatch.setattr(contacts, "find_event_by_idempotency", no_prior_event)
    monkeypatch.setattr(contacts, "emit_event", record_change)
    record = contacts.CRMImportRecord(
        entity_id=contact.entity_id, event_type="crm.contact.created",
        data={"name": "Client", "email": None},
        source="connector", idempotency_key="test:clear",
    )
    session = Session()
    existing_behavior = await contacts.import_contact_records(
        session, uuid.uuid4(), None, [record], match_identity=False,
    )
    assert existing_behavior.records[0].status == "skipped"
    assert not changes

    peer_behavior = await contacts.import_contact_records(
        session, uuid.uuid4(), None, [record], match_identity=False,
        allow_null_updates=True,
    )
    assert peer_behavior.records[0].status == "updated"
    assert changes == [{"email": {"old": "old@example.test", "new": None}}]



@pytest.mark.asyncio
async def test_product_peer_uses_canonical_inventory_writer_and_stable_restore_identity(monkeypatch):
    from types import SimpleNamespace
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from celerp.services.peer_identity import receive_product, new_target_identity
    from celerp_inventory import services as inventory

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            for table in (Company.__table__, User.__table__, UserCompany.__table__,
                          Projection.__table__, CompanyPeerIdentity.__table__):
                await conn.run_sync(table.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        target, source, owner_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        grant = SimpleNamespace(company_id=target, source_company_id=source)
        source_id = "item:source-widget"
        when = datetime(2026, 10, 12, tzinfo=timezone.utc)
        with_name = {"name": "Widget", "sku": "A1", "sell_by": "piece", "description": "Old"}
        changes = []
        async def canonical_writer(session, company_id, user, role, settings, body):
            assert company_id == target and user.id == owner_id and role == "owner"
            (record,) = body.records
            assert record.source == "connector"
            assert not (set(record.data) & {"quantity", "cost_base", "cost_total"})
            changes.append(record.event_type)
            row = await session.get(Projection, (company_id, record.entity_id))
            if row is None:
                assert record.event_type == "item.created"
                session.add(Projection(
                    company_id=company_id, entity_id=record.entity_id,
                    entity_type="item", state=record.data, version=1, updated_at=when))
                status = "created"
            else:
                assert record.event_type == "item.patched"
                row.state = {**row.state, **record.data}
                row.version += 1
                status = "updated"
            await session.flush()
            return SimpleNamespace(records=[SimpleNamespace(status=status)]), None
        monkeypatch.setattr(inventory, "write_import_batch", canonical_writer)
        async with sessions() as session:
            session.add(User(id=owner_id, email="firm@example.test", name="Accountant"))
            await session.flush()
            session.add(Company(id=target, name="Firm", slug="firm", settings={}))
            session.add(UserCompany(company_id=target, user_id=owner_id, role="owner"))
            await session.commit()
        async with sessions() as session:
            assert await receive_product(session, grant=grant, source_entity_id=source_id,
                                         source_revision=1, data=with_name) == "created"
            await session.commit()
            assert await receive_product(session, grant=grant, source_entity_id=source_id,
                                         source_revision=1, data=with_name) == "skipped"
            with pytest.raises(HTTPException) as reused:
                await receive_product(session, grant=grant, source_entity_id=source_id,
                                      source_revision=1, data={**with_name, "name": "Wrong"})
            assert reused.value.status_code == 409
            assert await receive_product(session, grant=grant, source_entity_id=source_id,
                                         source_revision=2,
                                         data={**with_name, "name": "New Widget"}) == "updated"
            await session.commit()
            row = await session.get(Projection, (target, new_target_identity(source, "item", source_id)))
            assert row is not None and row.state["name"] == "New Widget"
            assert await receive_product(session, grant=grant, source_entity_id=source_id,
                                         source_revision=3,
                                         data={**with_name, "name": "New Widget",
                                               "description": None, "sku": None}) == "updated"
            await session.commit()
            await session.refresh(row)
            assert row.state["description"] is None and row.state["sku"] is None
            mapping = await session.scalar(select(CompanyPeerIdentity).where(
                CompanyPeerIdentity.company_id == target,
                CompanyPeerIdentity.source_company_id == source))
            assert mapping.target_entity_id == row.entity_id and mapping.last_revision == 3
            assert changes == ["item.created", "item.patched", "item.patched"]
    finally:
        await engine.dispose()



@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ("inactive_company", "staged_company", "inactive_owner", "inactive_membership"))
async def test_peer_product_refuses_inactive_canonical_authority(blocked):
    from types import SimpleNamespace
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from celerp.services.peer_identity import receive_product

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    target, source, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            for table in (Company.__table__, User.__table__, UserCompany.__table__,
                          Projection.__table__, CompanyPeerIdentity.__table__):
                await connection.run_sync(table.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(User(id=owner, email="owner@example.test", name="Owner",
                             is_active=blocked != "inactive_owner"))
            await session.flush()
            session.add(Company(id=target, slug="firm", name="Firm", settings={},
                                is_active=blocked != "inactive_company",
                                is_migration_staged=blocked == "staged_company"))
            session.add(UserCompany(user_id=owner, company_id=target, role="owner",
                                    is_active=blocked != "inactive_membership"))
            await session.commit()
            with pytest.raises(HTTPException) as exc:
                await receive_product(
                    session, grant=SimpleNamespace(company_id=target, source_company_id=source),
                    source_entity_id="item:source", source_revision=1,
                    data={"name": "Widget", "sell_by": "piece"})
            assert exc.value.status_code == 403
            assert await session.scalar(select(CompanyPeerIdentity).where(
                CompanyPeerIdentity.company_id == target)) is None
    finally:
        await engine.dispose()



@pytest.mark.asyncio
async def test_peer_product_real_importer_preserves_catalog_without_creating_stock(client, session):
    from types import SimpleNamespace
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from celerp.services.peer_identity import receive_product, new_target_identity

    company_id, source, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session.add(User(id=owner, email=f"peer-{owner.hex[:8]}@example.test", name="Owner"))
    await session.flush()
    session.add(Company(id=company_id, name="Product receiver",
                        slug=f"receiver-{company_id.hex[:8]}", settings={}))
    await session.flush()
    session.add(UserCompany(company_id=company_id, user_id=owner,
                            role="owner", is_active=True))
    await session.commit()

    grant = SimpleNamespace(company_id=company_id, source_company_id=source)
    item = {"name": "Transferred catalog", "sku": f"PEER-{source.hex[:10]}",
            "sell_by": "piece", "description": "First version"}
    assert await receive_product(
        session, grant=grant, source_entity_id="item:remote",
        source_revision=1, data=item) == "created"
    await session.commit()

    target_id = new_target_identity(source, "item", "item:remote")
    row = await session.get(Projection, (company_id, target_id), populate_existing=True)
    assert row is not None and row.entity_type == "item"
    assert row.state["name"] == item["name"]
    assert not (row.state.get("quantity") or 0)
    assert await receive_product(
        session, grant=grant, source_entity_id="item:remote",
        source_revision=1, data=item) == "skipped"
    assert await receive_product(
        session, grant=grant, source_entity_id="item:remote",
        source_revision=2,
        data={**item, "name": "Renamed in source", "description": None}) == "updated"
    await session.commit()
    await session.refresh(row)
    assert row.state["name"] == "Renamed in source"
    assert row.state.get("description") is None
    assert not (row.state.get("quantity") or 0)
