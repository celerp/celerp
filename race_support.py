# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Two-connection race helpers on real PostgreSQL, shared by the core and module race tests:
a company seeded with an owner, a manager and an operator, the real app with a commit that
can be held, and a race that runs one transaction to its commit while a second waits on (or
finishes past) the company lock."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.main import app
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.routers import companies
from test_helpers import make_authed_token

_ITEM = "item:race"
_DRAFT = "item:draft"


async def _until_waiting_or_done(engine, task: asyncio.Task) -> None:
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the second transaction neither waited nor finished")


class _Held:
    """A commit held back until released, with a signal for when it is reached."""

    def __init__(self) -> None:
        self.reached, self.release = asyncio.Event(), asyncio.Event()

    def hold(self, session: AsyncSession) -> None:
        real = session.commit

        async def _held() -> None:
            self.reached.set()
            await self.release.wait()
            await real()

        session.commit = _held


@asynccontextmanager
async def _app_client(factory):
    """The real app on the race database; the next request's commit can be held."""
    pending: list[_Held] = []

    async def _session():
        async with factory() as s:
            if pending:
                pending.pop().hold(s)
            yield s

    @asynccontextmanager
    async def _ctx():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    try:
        with patch("celerp.gateway.client._client", MagicMock()), \
             patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
             patch("celerp.middleware.get_session_ctx", _ctx), \
             patch("celerp.db.LifecycleSessionLocal", factory):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c, pending
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _seed(factory, *, grants: dict | None = None, modules: list[str] | None = None) -> dict:
    """A company with an owner, a manager and an operator; with ``modules``, it uses
    those modules and the ones they need, and no other."""
    from celerp.modules.registry import dependency_closure

    company_id = uuid.uuid4()
    ids = {role: uuid.uuid4() for role in ("owner", "manager", "operator")}
    settings = {"role_grants": grants or {}}
    if modules is not None:
        settings["enabled_modules"] = dependency_closure(modules)
    async with factory() as s:
        for role, uid in ids.items():
            s.add(User(id=uid, email=f"{role}-{uid.hex[:8]}@example.test", name=role.title(),
                       auth_hash="x", is_active=True))
        await s.flush()
        s.add(Company(id=company_id, name="Access", slug=f"acc-{company_id.hex[:8]}", settings=settings))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        for role, uid in ids.items():
            s.add(UserCompany(user_id=uid, company_id=company_id, role=role, is_active=True))
        for entity_id, status in ((_ITEM, "available"), (_DRAFT, "draft")):
            await emit_event(
                s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.created",
                data={"sku": entity_id, "name": entity_id, "quantity": 1, "sell_by": "piece",
                      "status": status, "cost_price": 1, "retail_price": 2},
                actor_id=ids["owner"], location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
            )
        await s.commit()
    tokens = {}
    for role, uid in ids.items():
        async with factory() as s:
            tokens[role] = {"Authorization": f"Bearer {await make_authed_token(s, uid, company_id, role)}"}
            await s.commit()
    return {"company_id": company_id, "ids": ids, "h": tokens}


async def _direct(factory, held: _Held | None, fn):
    async with factory() as s:
        if held is not None:
            held.hold(s)
        try:
            return await fn(s)
        except Exception as exc:  # noqa: BLE001 - the outcome is what the race reports
            await s.rollback()
            return exc


async def _http(pending: list, held: _Held | None, send):
    if held is not None:
        pending.append(held)
    return await send()


async def _race(engine, first, second):
    """``first`` runs to its commit and holds it; ``second`` starts and waits on (or
    finishes past) the lock; then ``first`` commits."""
    held = _Held()
    one = asyncio.create_task(first(held))
    reached = asyncio.create_task(held.reached.wait())
    await asyncio.wait({one, reached}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
    two = asyncio.create_task(second(None))
    await _until_waiting_or_done(engine, two)
    held.release.set()
    reached.cancel()
    return await asyncio.wait_for(one, 30), await asyncio.wait_for(two, 30)


def _ok(outcome) -> None:
    assert not isinstance(outcome, BaseException), outcome
    if hasattr(outcome, "status_code") and not isinstance(outcome, dict):
        assert outcome.status_code == 200, outcome.text


def _refused(outcome, *statuses: int) -> None:
    assert not isinstance(outcome, BaseException), outcome
    assert outcome.status_code in statuses, (outcome.status_code, outcome.text)


def _revoke(company_id, perm_key, role_key):
    async def _go(s):
        return await companies.patch_role_permissions(
            companies.RolePermissionPatch(perm_key=perm_key, role_key=role_key, granted=False),
            company_id=company_id, _=None, session=s)
    return _go
