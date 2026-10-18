"""Company keys last a year, are renewed by the firm in one click, and warn both sides first."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerGrant
from celerp.models.notification import Notification, NotificationRead
from celerp.services import peer_access
from celerp.services.peer_access import authenticate, issue, notify_expiring, renew, revoke

AT = datetime(2026, 10, 12, tzinfo=timezone.utc)
YEAR = timedelta(days=365)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (Company, CompanyPeerGrant, Notification, NotificationRead):
            await conn.run_sync(lambda c, m=model: m.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        company = Company(id=uuid.uuid4(), name="Firm books", slug="firm", settings={})
        session.add(company)
        await session.commit()
        company_id = company.id
    yield factory, company_id
    await engine.dispose()


async def _issued(factory, target, source, *, label="Client books", when=AT):
    async with factory() as s:
        grant, key = await issue(s, company_id=target, source_company_id=source, label=label, firm="Firm Practice",
                                 scopes=frozenset({"contacts", "documents"}), now=when)
        await s.commit()
    return grant, key


async def _accepts(factory, key, source, when) -> bool:
    async with factory() as s:
        try:
            await authenticate(s, key, source_company_id=source, scope="contacts", now=when)
        except HTTPException:
            return False
        return True


async def _notices(factory, target):
    async with factory() as s:
        return (await s.execute(select(Notification).where(
            Notification.company_id == target).order_by(Notification.title))).scalars().all()


@pytest.mark.asyncio
async def test_keys_last_one_year(db):
    factory, target = db
    source = uuid.uuid4()
    grant, key = await _issued(factory, target, source)
    assert grant.expires_at == AT + YEAR
    assert await _accepts(factory, key, source, AT + YEAR - timedelta(minutes=1))
    assert not await _accepts(factory, key, source, AT + YEAR)


@pytest.mark.asyncio
async def test_peer_key_rotation(db):
    factory, target = db
    source = uuid.uuid4()
    grant, key = await _issued(factory, target, source)

    # Before expiry and after it, one click gives the same key another year.
    soon = AT + YEAR - timedelta(days=10)
    async with factory() as s:
        row = await renew(s, target, grant.id, now=soon)
        await s.commit()
    assert row.expires_at == soon + YEAR
    late = soon + YEAR + timedelta(days=3)
    assert not await _accepts(factory, key, source, late)
    async with factory() as s:
        row = await renew(s, target, grant.id, now=late)
        again = await renew(s, target, grant.id, now=late)
        await s.commit()
    assert row.expires_at == again.expires_at == late + YEAR
    assert await _accepts(factory, key, source, late)

    # Another company's key is not found; a revoked key cannot come back.
    async with factory() as s:
        with pytest.raises(HTTPException) as missing:
            await renew(s, uuid.uuid4(), grant.id, now=late)
        assert missing.value.status_code == 404
    async with factory() as s:
        await revoke(s, target, grant.id, now=late)
        await s.commit()
    async with factory() as s:
        with pytest.raises(HTTPException) as revoked:
            await renew(s, target, grant.id, now=late)
        assert revoked.value.status_code == 409


@pytest.mark.asyncio
async def test_firm_is_warned_14_days_before_a_key_expires(db):
    factory, target = db
    source = uuid.uuid4()
    grant, _ = await _issued(factory, target, source, label="Acme Trading")
    await _issued(factory, target, uuid.uuid4(), label="Later Co", when=AT + timedelta(days=60))

    async with factory() as s:
        await notify_expiring(s, target, now=AT + YEAR - timedelta(days=15))
        await s.commit()
    assert await _notices(factory, target) == []

    soon = AT + YEAR - timedelta(days=14)
    async with factory() as s:
        await notify_expiring(s, target, now=soon)
        await notify_expiring(s, target, now=soon + timedelta(hours=1))
        await s.commit()
    [warning] = await _notices(factory, target)
    assert warning.priority == "high"
    assert warning.action_url == "/settings/accountant-access"
    assert warning.body.startswith("The sync key for Acme Trading expires on 2027-10-12.")
    assert warning.i18n["params"] == {"company": "Acme Trading", "date": "2027-10-12"}

    # Still standing once the key has expired, saying so.
    async with factory() as s:
        await notify_expiring(s, target, now=AT + YEAR + timedelta(days=1))
        await s.commit()
    [expired] = await _notices(factory, target)
    assert expired.body.startswith("The sync key for Acme Trading expired on 2027-10-12.")

    # Renewing clears it.
    async with factory() as s:
        await renew(s, target, grant.id, now=AT + YEAR + timedelta(days=1))
        await notify_expiring(s, target, now=AT + YEAR + timedelta(days=1))
        await s.commit()
    assert await _notices(factory, target) == []


@pytest.mark.asyncio
async def test_an_expired_key_is_refused_with_the_firm_name(db):
    factory, target = db
    source = uuid.uuid4()
    _, key = await _issued(factory, target, source)
    async with factory() as s:
        with pytest.raises(HTTPException) as refused:
            await peer_access.held(s, key, source_company_id=source, now=AT + YEAR)
    assert refused.value.status_code == 401
    assert refused.value.detail == {"code": "key_expired", "company_name": "Firm books"}
    # Any other refusal says nothing about the company.
    async with factory() as s:
        with pytest.raises(HTTPException) as other:
            await peer_access.held(s, key, source_company_id=uuid.uuid4(), now=AT + YEAR)
    assert other.value.detail == "Invalid integration credentials"


class _Peer:
    """The firm's installation as the client company's connector sees it."""

    def __init__(self, *, expires_at=None, status=200, detail=None):
        self.expires_at, self.status, self.detail = expires_at, status, detail
        self.calls = []

    def client(self, **kwargs):
        peer = self
        assert kwargs == {"public_only": True}

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, url, **kw):
                from types import SimpleNamespace
                peer.calls.append(("GET", url, kw["headers"]["Authorization"], kw.get("params")))
                body = ({"company_id": "x", "company_name": "Firm books", "scopes": ["contacts"],
                         "expires_at": peer.expires_at.isoformat()} if peer.status == 200
                        else {"detail": peer.detail})
                return SimpleNamespace(status_code=peer.status, json=lambda: body)
            async def post(self, url, **kw):
                raise AssertionError("the client never changes its key")
        return Client()


def _wire(monkeypatch, peer):
    from celerp.connectors import celerp_key
    told = []

    async def tell(company_id, kind, **params):
        told.append((kind, params))

    monkeypatch.setattr(celerp_key, "RateLimitedClient", peer.client)
    monkeypatch.setattr(celerp_key, "_tell", tell)
    return told


def _ctx(company):
    from celerp.connectors.base import ConnectorContext
    return ConnectorContext(company_id=company, access_token="cp1.current",
                            store_handle="https://firm.example")


@pytest.mark.asyncio
@pytest.mark.parametrize("days,kind", [(20, "current"), (14, "expiring"), (3, "expiring")])
async def test_client_is_warned_14_days_before_its_key_expires(monkeypatch, days, kind):
    from celerp.connectors.celerp import CelerpConnector
    company = str(uuid.uuid4())
    peer = _Peer(expires_at=AT + timedelta(days=days))
    told = _wire(monkeypatch, peer)
    ctx = _ctx(company)
    await CelerpConnector().before_outbound(ctx, now=AT)
    assert peer.calls == [("GET", "https://firm.example/company-peers/verify", "Peer cp1.current",
                           {"source_company_id": company})]
    assert ctx.access_token == "cp1.current"
    expected = {} if kind == "current" else {
        "firm": "Firm books", "date": (AT + timedelta(days=days)).date().isoformat()}
    assert told == [(kind, expected)]


@pytest.mark.asyncio
async def test_client_is_told_its_key_expired_and_whom_to_ask(monkeypatch):
    from celerp.connectors.celerp import CelerpConnector
    peer = _Peer(status=401, detail={"code": "key_expired", "company_name": "Firm books"})
    told = _wire(monkeypatch, peer)
    await CelerpConnector().before_outbound(_ctx(str(uuid.uuid4())), now=AT)
    assert told == [("expired", {"firm": "Firm books"})]


@pytest.mark.asyncio
async def test_client_is_told_when_the_firm_no_longer_accepts_its_key(monkeypatch):
    from celerp.connectors.celerp import CelerpConnector
    peer = _Peer(status=401, detail="Invalid integration credentials")
    told = _wire(monkeypatch, peer)
    await CelerpConnector().before_outbound(_ctx(str(uuid.uuid4())), now=AT)
    assert told == [("rejected", {"firm": "firm.example"})]


@pytest.mark.asyncio
async def test_client_warning_text_names_the_firm(monkeypatch):
    """The client's notices use the spec wording and replace one another."""
    from celerp.connectors import celerp_key
    from celerp.notifications import service

    calls = []

    async def standing(session, cid, category, title, body, **kw):
        calls.append(("standing", title, body))

    async def clear(session, cid, category, title):
        calls.append(("clear", title))

    class Session:
        async def commit(self):
            pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session_ctx():
        yield Session()

    from celerp import db as celerp_db
    monkeypatch.setattr(celerp_db, "get_session_ctx", session_ctx)
    monkeypatch.setattr(service, "notify_standing", standing)
    monkeypatch.setattr(service, "clear_standing", clear)
    await celerp_key._tell(str(uuid.uuid4()), "expiring", firm="Firm books", date="2026-10-20")
    shown = [c for c in calls if c[0] == "standing"]
    assert shown == [("standing", "Sync key expiring",
                      "The sync key for Firm books expires on 2026-10-20. Ask Firm books to renew it.")]
    calls.clear()
    await celerp_key._tell(str(uuid.uuid4()), "expired", firm="Firm books")
    assert [c[2] for c in calls if c[0] == "standing"] == [
        "The sync key has expired. Ask Firm books to renew it."]
    calls.clear()
    await celerp_key._tell(str(uuid.uuid4()), "rejected", firm="Firm books")
    assert [c[2] for c in calls if c[0] == "standing"] == [
        "The sync key was rejected. Ask Firm books for a new pairing link."]
    calls.clear()
    await celerp_key._tell(str(uuid.uuid4()), "current")
    assert [c[0] for c in calls] == ["clear", "clear", "clear"]


@pytest.mark.asyncio
async def test_outbound_run_checks_the_key_once_before_sending(monkeypatch):
    from contextlib import asynccontextmanager
    from celerp import db
    from celerp.connectors import ownership, sync_runner
    from celerp.connectors.base import SyncDirection, SyncEntity, SyncResult
    from celerp.connectors.celerp import CelerpConnector

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def session():
        yield Session()

    async def no_config(*args, **kwargs):
        return None

    order = []

    async def fake_run_sync(connector, ctx, entity, **kwargs):
        order.append(entity)
        return SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.OUTBOUND)

    async def before(self, ctx, now=None):
        order.append("key")

    monkeypatch.setattr(db, "get_session_ctx", session)
    monkeypatch.setattr(ownership, "lock_connector_operation", no_config)
    monkeypatch.setattr(sync_runner, "run_sync", fake_run_sync)
    monkeypatch.setattr(CelerpConnector, "before_outbound", before)
    await sync_runner.run_connector_sync(CelerpConnector(), _ctx(str(uuid.uuid4())), SyncDirection.OUTBOUND)
    assert order[0] == "key" and order.count("key") == 1 and len(order) > 2


@pytest.mark.asyncio
async def test_the_client_is_told_the_firm_named_on_the_key(db):
    """The firm's name comes from the key, never from whichever company the firm had open
    when it opened the client's copy, which may be another of its clients."""
    from types import SimpleNamespace
    from celerp.services.peer_journals import lock_feed
    factory, target = db
    async with factory() as s:
        company = await peer_access.locked_company(s, target)
        company.settings = {"restored_backup": {"opened_from": "Northwind Traders"}}
        await s.commit()
    source = uuid.uuid4()
    async with factory() as s:
        _, key = await issue(s, company_id=target, source_company_id=source, label="Client books",
                             firm="Firm Practice", scopes=frozenset({"documents"}), now=AT)
        await s.commit()
    ctx = SimpleNamespace(peer=SimpleNamespace(credential=key, source_company_id=source, scope="documents"))
    async with factory() as s:
        assert (await lock_feed(s, ctx))["firm"] == "Firm Practice"
