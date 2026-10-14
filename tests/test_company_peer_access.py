"""Company integration grants do not confer general user authority."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerGrant
from celerp.services.peer_access import issue, authenticate, revoke, _parse, SCOPES

AT = datetime(2026, 10, 12, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Company.__table__.create(c))
        await conn.run_sync(lambda c: CompanyPeerGrant.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        company = Company(id=uuid.uuid4(), name="Accounting", slug="accounting", settings={})
        session.add(company)
        await session.commit()
        company_id = company.id
    yield factory, company_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_issue_verify_and_revoke(db):
    factory, target = db
    source = uuid.uuid4()
    async with factory() as s:
        grant, key = await issue(s, company_id=target, source_company_id=source,
                                 label="Client books", scopes=frozenset({"contacts"}), now=AT)
        gid = grant.id
        await s.commit()
        assert key not in repr(grant)
        assert key not in str(grant.__dict__)
        assert _parse(key)[:2] == (target, gid)
    async with factory() as s:
        assert (await authenticate(s, key, source_company_id=source, scope="contacts", now=AT)).id == gid
        for invalid in [
                {"source_company_id": uuid.uuid4(), "scope": "invoices", "now": AT},
                {"source_company_id": source, "scope": "payments", "now": AT},
                {"source_company_id": source, "scope": "invoices", "now": AT},
                {"source_company_id": source, "scope": "invoices", "now": AT + timedelta(days=30)},
                {"source_company_id": source, "scope": "manage_users", "now": AT}]:
            with pytest.raises(HTTPException):
                await authenticate(s, key, **invalid)
        await s.rollback()
    async with factory() as s:
        await revoke(s, target, gid, now=AT)
        await revoke(s, target, gid, now=AT)
        await s.commit()
    async with factory() as s:
        with pytest.raises(HTTPException):
            await authenticate(s, key, source_company_id=source, scope="invoices", now=AT)


@pytest.mark.asyncio
async def test_unrelated_company_and_malformed_keys(db):
    factory, target = db
    source = uuid.uuid4()
    async with factory() as s:
        _, key = await issue(s, company_id=target, source_company_id=source,
                             label="A", scopes=frozenset({"contacts"}), now=AT)
        await s.commit()
    bad = [None, "", "x" + key, key + "z", key.upper(), key[:-1] + "%",
           key.replace(str(target), str(uuid.uuid4())), key[:-1] + "/", key + "?x=1"]
    async with factory() as s:
        for candidate in bad:
            with pytest.raises(HTTPException):
                await authenticate(s, candidate, source_company_id=source, scope="contacts", now=AT)


@pytest.mark.asyncio
async def test_active_grant_capacity_and_scope_validation(db):
    factory, target = db
    async with factory() as s:
        for _ in range(5):
            await issue(s, company_id=target, source_company_id=uuid.uuid4(),
                        label="A", scopes=frozenset({"contacts"}), now=AT)
        with pytest.raises(HTTPException, match="Too many"):
            await issue(s, company_id=target, source_company_id=uuid.uuid4(),
                        label="A", scopes=frozenset({"contacts"}), now=AT)
        with pytest.raises(HTTPException):
            await issue(s, company_id=target, source_company_id=uuid.uuid4(),
                        label="A", scopes=frozenset({"administration"}), now=AT)
        await s.rollback()


def test_unimplemented_peer_operations_cannot_be_pre_authorized():
    from pydantic import ValidationError
    from celerp.routers.company_peers import GrantRequest

    assert SCOPES == frozenset({"contacts", "products", "documents", "inventory"})
    target = uuid.uuid4()
    common = {"source_company_id": target, "label": "Reviewed contact-only access"}
    assert GrantRequest(**common, scopes={"contacts"}).scopes == {"contacts"}
    assert GrantRequest(**common, scopes={"products", "contacts"}).scopes == {"products", "contacts"}
    assert GrantRequest(**common, scopes={"documents"}).scopes == {"documents"}
    assert GrantRequest(**common, scopes={"inventory"}).scopes == {"inventory"}
    for dormant in ("items", "invoices", "payments", "attachments"):
        with pytest.raises(ValidationError):
            GrantRequest(**common, scopes={dormant})



@pytest.mark.asyncio
async def test_product_peer_scope_is_separate_from_contact_authority(db):
    factory, target = db
    source = uuid.uuid4()
    async with factory() as session:
        _, key = await issue(session, company_id=target,
                             source_company_id=source, label="Product catalog",
                             scopes=frozenset({"products"}), now=AT)
        await session.commit()
    async with factory() as session:
        assert (await authenticate(session, key, source_company_id=source,
                                   scope="products", now=AT)).company_id == target
        with pytest.raises(HTTPException) as no_contacts:
            await authenticate(session, key, source_company_id=source,
                               scope="contacts", now=AT)
        assert no_contacts.value.status_code == 401
        with pytest.raises(HTTPException):
            await authenticate(session, key, source_company_id=uuid.uuid4(),
                               scope="products", now=AT)



@pytest.mark.asyncio
async def test_inventory_scope_does_not_grant_financial_document_authority(db):
    factory, target = db
    source = uuid.uuid4()
    async with factory() as session:
        _, key = await issue(session, company_id=target, source_company_id=source,
                             label="Physical counts", scopes=frozenset({"inventory"}), now=AT)
        await session.commit()
    async with factory() as session:
        grant = await authenticate(session, key, source_company_id=source,
                                   scope="inventory", now=AT)
        assert grant.company_id == target
        with pytest.raises(HTTPException):
            await authenticate(session, key, source_company_id=source,
                               scope="documents", now=AT)
        with pytest.raises(HTTPException):
            await authenticate(session, key, source_company_id=uuid.uuid4(),
                               scope="inventory", now=AT)
