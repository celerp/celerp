"""Company keys can be rotated by the sender, extended by the owner, and warn before expiry."""
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
from celerp.services.peer_access import authenticate, extend, issue, notify_expiring, revoke, rotate

AT = datetime(2026, 10, 12, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (Company, CompanyPeerGrant, Notification, NotificationRead):
            await conn.run_sync(lambda c, m=model: m.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        company = Company(id=uuid.uuid4(), name="Accounting", slug="accounting", settings={})
        session.add(company)
        await session.commit()
        company_id = company.id
    yield factory, company_id
    await engine.dispose()


async def _issued(factory, target, source, *, days=30, scopes=frozenset({"contacts", "documents"})):
    async with factory() as s:
        grant, key = await issue(s, company_id=target, source_company_id=source,
                                 label="Client books", scopes=scopes, lifetime_days=days, now=AT)
        await s.commit()
    return grant.id, key


async def _accepts(factory, key, source, when, scope="contacts") -> bool:
    async with factory() as s:
        try:
            await authenticate(s, key, source_company_id=source, scope=scope, now=when)
        except HTTPException:
            return False
        return True


@pytest.mark.asyncio
async def test_peer_key_rotation(db):
    factory, target = db
    source = uuid.uuid4()
    old_id, old = await _issued(factory, target, source, days=14)
    when = AT + timedelta(days=10)
    async with factory() as s:
        first, first_key = await rotate(s, old, source_company_id=source, now=when)
        await s.commit()
    assert first_key != old
    assert first.label == "Client books" and sorted(first.scopes) == ["contacts", "documents"]
    assert first.lifetime_days == 14 and first.expires_at == when + timedelta(days=14)
    assert await _accepts(factory, first_key, source, when, scope="documents")
    # The old key keeps working for a day, so a reply lost on the way back never stops sync.
    assert await _accepts(factory, old, source, when + timedelta(hours=23))
    assert not await _accepts(factory, old, source, when + timedelta(days=1, minutes=1))

    # A retry with the old key replaces the reply that was lost: only the newest key works.
    async with factory() as s:
        second, second_key = await rotate(s, old, source_company_id=source, now=when + timedelta(hours=1))
        await s.commit()
    assert not await _accepts(factory, first_key, source, when + timedelta(hours=2))
    assert await _accepts(factory, second_key, source, when + timedelta(hours=2))
    async with factory() as s:
        row = await s.get(CompanyPeerGrant, old_id)
        assert row.replaced_by_id == second.id

    # Another company, a revoked key and an expired key cannot rotate.
    async with factory() as s:
        with pytest.raises(HTTPException) as other:
            await rotate(s, second_key, source_company_id=uuid.uuid4(), now=when)
        assert other.value.status_code == 401
    async with factory() as s:
        await revoke(s, target, second.id, now=when + timedelta(hours=3))
        await s.commit()
    async with factory() as s:
        with pytest.raises(HTTPException):
            await rotate(s, second_key, source_company_id=source, now=when + timedelta(hours=4))
    _, short = await _issued(factory, target, source, days=1)
    async with factory() as s:
        with pytest.raises(HTTPException):
            await rotate(s, short, source_company_id=source, now=AT + timedelta(days=2))


@pytest.mark.asyncio
async def test_rotation_does_not_count_the_retiring_key_toward_the_limit(db):
    factory, target = db
    keys = [await _issued(factory, target, uuid.uuid4()) for _ in range(peer_access.MAX_ACTIVE - 1)]
    source = uuid.uuid4()
    _, last = await _issued(factory, target, source)
    async with factory() as s:
        _, renewed = await rotate(s, last, source_company_id=source, now=AT + timedelta(days=25))
        await s.commit()
    assert renewed.startswith("cp1.") and len(keys) == peer_access.MAX_ACTIVE - 1


@pytest.mark.asyncio
async def test_key_renewal_and_expiry_warning(db):
    factory, target = db
    source = uuid.uuid4()
    grant_id, key = await _issued(factory, target, source, days=30)

    async def notices():
        async with factory() as s:
            return (await s.execute(select(Notification).where(
                Notification.company_id == target))).scalars().all()

    async with factory() as s:
        await notify_expiring(s, target, now=AT + timedelta(days=20))
        await s.commit()
    assert await notices() == []

    soon = AT + timedelta(days=24)
    async with factory() as s:
        await notify_expiring(s, target, now=soon)
        await notify_expiring(s, target, now=soon + timedelta(hours=1))
        await s.commit()
    [warning] = await notices()
    assert warning.priority == "high"
    assert warning.action_url == "/settings/accountant-access"
    assert "Client books" in warning.body and "2026-11-11" in warning.body
    assert warning.i18n["title"] == "notice.peer_keys_expiring.title"

    # After expiry, the owner extends the key in one click and the same key works again.
    late = AT + timedelta(days=31)
    assert not await _accepts(factory, key, source, late)
    async with factory() as s:
        row = await extend(s, target, grant_id, now=late)
        await notify_expiring(s, target, now=late)
        await s.commit()
    assert row.expires_at == late + timedelta(days=30)
    assert await _accepts(factory, key, source, late)
    assert await notices() == []

    # A revoked or replaced key cannot be extended; another company's key is not found.
    async with factory() as s:
        with pytest.raises(HTTPException) as missing:
            await extend(s, uuid.uuid4(), grant_id, now=late)
        assert missing.value.status_code == 404
    async with factory() as s:
        await rotate(s, key, source_company_id=source, now=late)
        await s.commit()
    async with factory() as s:
        with pytest.raises(HTTPException) as replaced:
            await extend(s, target, grant_id, now=late)
        assert replaced.value.status_code == 409


class _Peer:
    """The receiving installation as the sending company's connector sees it."""

    def __init__(self, *, expires_at, verify_status=200, renew_status=200):
        self.expires_at, self.verify_status, self.renew_status = expires_at, verify_status, renew_status
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
                return SimpleNamespace(status_code=peer.verify_status,
                                       json=lambda: {"company_id": "x", "scopes": ["contacts"],
                                                     "expires_at": peer.expires_at.isoformat()})
            async def post(self, url, **kw):
                from types import SimpleNamespace
                peer.calls.append(("POST", url, kw["headers"]["Authorization"], kw.get("json")))
                return SimpleNamespace(status_code=peer.renew_status, json=lambda: {
                    "credential": "cp1.renewed", "expires_at": (AT + timedelta(days=60)).isoformat()})
        return Client()


def _wire(monkeypatch, peer, *, store_ok=True):
    from celerp.connectors import celerp_key
    stored, told = [], []

    async def store(connector, key, secret, store_url):
        stored.append((connector, key, secret, store_url))
        if not store_ok:
            raise RuntimeError("relay unavailable")

    async def tell(company_id, kind, **params):
        told.append((kind, params))

    async def latest(ctx):
        return ctx

    monkeypatch.setattr(celerp_key, "RateLimitedClient", peer.client)
    monkeypatch.setattr(celerp_key, "store_api_key", store)
    monkeypatch.setattr(celerp_key, "_tell", tell)
    monkeypatch.setattr(celerp_key, "_current_context", latest)
    return stored, told


def _ctx(company):
    from celerp.connectors.base import ConnectorContext
    return ConnectorContext(company_id=company, access_token="cp1.current",
                            store_handle="https://firm.example")


@pytest.mark.asyncio
async def test_sender_renews_its_key_before_expiry_and_says_so(monkeypatch):
    from celerp.connectors.celerp import CelerpConnector
    company = str(uuid.uuid4())
    peer = _Peer(expires_at=AT + timedelta(days=5))
    stored, told = _wire(monkeypatch, peer)
    ctx = _ctx(company)
    await CelerpConnector().before_outbound(ctx, now=AT)
    assert [c[:3] for c in peer.calls] == [
        ("GET", "https://firm.example/company-peers/verify", "Peer cp1.current"),
        ("POST", "https://firm.example/company-peers/renew", "Peer cp1.current")]
    assert peer.calls[1][3] == {"source_company_id": company}
    assert stored == [("celerp", "cp1.renewed", "", "https://firm.example")]
    assert ctx.access_token == "cp1.renewed"
    assert told == [("renewed", {"date": "2026-12-11"})]


@pytest.mark.asyncio
async def test_sender_leaves_a_current_key_alone(monkeypatch):
    from celerp.connectors.celerp import CelerpConnector
    peer = _Peer(expires_at=AT + timedelta(days=20))
    stored, told = _wire(monkeypatch, peer)
    await CelerpConnector().before_outbound(_ctx(str(uuid.uuid4())), now=AT)
    assert [c[0] for c in peer.calls] == ["GET"] and stored == []
    assert told == [("current", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["refused", "relay"])
async def test_sender_is_warned_when_its_key_cannot_be_renewed(monkeypatch, case):
    from celerp.connectors.celerp import CelerpConnector
    peer = _Peer(expires_at=AT + timedelta(days=3), renew_status=409 if case == "refused" else 200)
    stored, told = _wire(monkeypatch, peer, store_ok=case != "relay")
    ctx = _ctx(str(uuid.uuid4()))
    await CelerpConnector().before_outbound(ctx, now=AT)
    assert ctx.access_token == "cp1.current"
    assert told == [("renew_failed", {"date": "2026-10-15"})]


@pytest.mark.asyncio
async def test_sender_is_told_when_the_receiving_company_no_longer_accepts_its_key(monkeypatch):
    from celerp.connectors.celerp import CelerpConnector
    peer = _Peer(expires_at=AT, verify_status=401)
    stored, told = _wire(monkeypatch, peer)
    await CelerpConnector().before_outbound(_ctx(str(uuid.uuid4())), now=AT)
    assert [c[0] for c in peer.calls] == ["GET"] and stored == []
    assert told == [("rejected", {})]


@pytest.mark.asyncio
async def test_outbound_run_keeps_the_key_current_once_before_sending(monkeypatch):
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
