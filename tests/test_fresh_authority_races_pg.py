# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A write that waits for the company lock is judged by the access its user holds
once it has the lock, not by the access it was signed in with.

Each race runs on real PostgreSQL across two connections, in both orders. The write
goes through the full HTTP stack (sign-in token, permission dependencies, handler);
the access change runs through the same handler an owner uses. Whichever transaction
holds the company lock first commits first:

- access change first: the waiting write is refused and leaves nothing behind;
- write first: the write commits, and the access change waits for it, then lands.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.main import app
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.routers import companies
from test_helpers import make_authed_token

pytestmark = [pytest.mark.asyncio, pytest.mark.process]

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


async def _seed(factory, *, grants: dict | None = None) -> dict:
    company_id = uuid.uuid4()
    ids = {role: uuid.uuid4() for role in ("owner", "manager", "operator")}
    async with factory() as s:
        s.add(Company(id=company_id, name="Access", slug=f"acc-{company_id.hex[:8]}",
                      settings={"role_grants": grants or {}}))
        for role, uid in ids.items():
            s.add(User(id=uid, email=f"{role}-{uid.hex[:8]}@example.test", name=role.title(),
                       auth_hash="x", is_active=True))
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


async def _item(factory, company_id, entity_id) -> tuple[dict, int]:
    async with factory() as s:
        state = (await s.get(Projection, {"company_id": company_id, "entity_id": entity_id})).state
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id))).scalar_one()
    return dict(state), events


async def _doc_count(factory, company_id) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "doc"))).scalar_one()


def _revoke(company_id, perm_key, role_key):
    async def _go(s):
        return await companies.patch_role_permissions(
            companies.RolePermissionPatch(perm_key=perm_key, role_key=role_key, granted=False),
            company_id=company_id, _=None, session=s)
    return _go


def _patch_member(company_id, user_id, **change):
    async def _go(s):
        return await companies.patch_user(
            user_id, companies.UserPatch(**change), company_id=company_id, caller_role="owner",
            _=None, session=s)
    return _go


# Each case: the access change, the write (role, method, path, body), the item it touches,
# and the statuses a refused write may answer with (a role change also signs the user out).
_CASES = {
    "permission_revoked": (
        lambda c: _revoke(c["company_id"], "edit_inventory", "operator"),
        ("operator", "post", f"/items/{_ITEM}/status", {"new_status": "archived"}), _ITEM, (403,)),
    "secondary_price_gate_revoked": (
        lambda c: _revoke(c["company_id"], "set_inventory_prices", "operator"),
        ("operator", "post", f"/items/{_ITEM}/price", {"price_type": "retail_price", "new_price": 9}), _ITEM, (403,)),
    "secondary_revert_gate_revoked": (
        lambda c: _revoke(c["company_id"], "revert_items_to_draft", "manager"),
        ("manager", "post", "/items/bulk/revert-to-draft", {"entity_ids": [_ITEM]}), _ITEM, (403,)),
    "demotion": (
        lambda c: _patch_member(c["company_id"], c["ids"]["manager"], role="operator"),
        ("manager", "post", "/items/bulk/status", {"entity_ids": [_ITEM], "status": "archived"}), _ITEM, (401, 403)),
    "member_deactivated": (
        lambda c: _patch_member(c["company_id"], c["ids"]["operator"], is_active=False),
        ("operator", "post", f"/items/{_ITEM}/status", {"new_status": "archived"}), _ITEM, (401, 403)),
}
_GRANTS = {"set_inventory_prices": ["operator", "manager", "admin", "owner"]}


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_access_change_first_refuses_the_waiting_write(committed_engine, case):
    change, (role, method, path, body), entity_id, refusals = _CASES[case]
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory, grants=_GRANTS)
    before = await _item(factory, c["company_id"], entity_id)
    async with _app_client(factory) as (client, pending):
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, change(c)),
            lambda held: _http(pending, held, lambda: getattr(client, method)(path, json=body, headers=c["h"][role])),
        )
    _ok(changed)
    _refused(wrote, *refusals)
    assert await _item(factory, c["company_id"], entity_id) == before


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_write_first_commits_and_the_access_change_waits(committed_engine, case):
    change, (role, method, path, body), entity_id, _ = _CASES[case]
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory, grants=_GRANTS)
    before = await _item(factory, c["company_id"], entity_id)
    async with _app_client(factory) as (client, pending):
        wrote, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, lambda: getattr(client, method)(path, json=body, headers=c["h"][role])),
            lambda held: _direct(factory, held, change(c)),
        )
    _ok(wrote)
    _ok(changed)
    after = await _item(factory, c["company_id"], entity_id)
    assert after[1] == before[1] + 1


async def test_document_price_override_refused_when_its_permission_is_revoked_first(committed_engine):
    """A document write is judged the same way: set_sales_doc_prices revoked while an
    invoice with an overridden line price waits for the lock refuses the invoice."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory, grants={"set_sales_doc_prices": ["operator", "manager", "admin", "owner"]})
    body = {"doc_type": "invoice", "line_items": [
        {"item_id": _ITEM, "sku": _ITEM, "name": _ITEM, "quantity": 1, "unit_price": 777, "sell_by": "piece"}]}
    async with _app_client(factory) as (client, pending):
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "set_sales_doc_prices", "operator")),
            lambda held: _http(pending, held, lambda: client.post("/docs", json=body, headers=c["h"]["operator"])),
        )
    _ok(changed)
    _refused(wrote, 403)
    assert await _doc_count(factory, c["company_id"]) == 0


async def test_document_price_override_commits_when_it_holds_the_lock_first(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory, grants={"set_sales_doc_prices": ["operator", "manager", "admin", "owner"]})
    body = {"doc_type": "invoice", "line_items": [
        {"item_id": _ITEM, "sku": _ITEM, "name": _ITEM, "quantity": 1, "unit_price": 777, "sell_by": "piece"}]}
    async with _app_client(factory) as (client, pending):
        wrote, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, lambda: client.post("/docs", json=body, headers=c["h"]["operator"])),
            lambda held: _direct(factory, held, _revoke(c["company_id"], "set_sales_doc_prices", "operator")),
        )
    _ok(wrote)
    _ok(changed)
    assert await _doc_count(factory, c["company_id"]) == 1


async def test_draft_cost_refused_once_a_concurrent_make_available_lands_first(committed_engine):
    """A draft's author may set its cost without set_inventory_prices; once Make Available
    commits first, the waiting cost edit is judged against the live item and refused."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    before = await _item(factory, c["company_id"], _DRAFT)
    async with _app_client(factory) as (client, pending):
        made, priced = await _race(
            committed_engine,
            lambda held: _http(pending, held, lambda: client.post(
                "/items/bulk/make-available", json={"entity_ids": [_DRAFT]}, headers=c["h"]["owner"])),
            lambda held: _http(pending, held, lambda: client.post(
                f"/items/{_DRAFT}/price", json={"price_type": "cost_price", "new_price": 5}, headers=c["h"]["operator"])),
        )
    _ok(made)
    _refused(priced, 403)
    state, events = await _item(factory, c["company_id"], _DRAFT)
    assert state["status"] == "available" and state.get("cost_total") == before[0].get("cost_total")
    assert events == before[1] + 1


async def test_draft_cost_set_first_then_make_available_waits(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        priced, made = await _race(
            committed_engine,
            lambda held: _http(pending, held, lambda: client.post(
                f"/items/{_DRAFT}/price", json={"price_type": "cost_price", "new_price": 5}, headers=c["h"]["operator"])),
            lambda held: _http(pending, held, lambda: client.post(
                "/items/bulk/make-available", json={"entity_ids": [_DRAFT]}, headers=c["h"]["owner"])),
        )
    _ok(priced)
    _ok(made)
    state, _ = await _item(factory, c["company_id"], _DRAFT)
    assert state["status"] == "available" and float(state["cost_total"]) == 5


async def test_a_caught_refusal_still_cannot_commit(committed_engine):
    """A writer that catches the lock's refusal and carries on (a per-row importer) still
    commits nothing: the refusal stands until the transaction ends."""
    from fastapi import HTTPException

    from celerp.services.company_lock import lock_company
    from celerp.services.permissions import authorize_request, end_request

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    async with factory() as s:
        await _revoke(c["company_id"], "edit_inventory", "operator")(s)
    async with factory() as s:
        authority = authorize_request(s, c["company_id"], c["ids"]["operator"], "operator")
        authority.keys.add("edit_inventory")
        with pytest.raises(HTTPException) as refused:
            await lock_company(s, c["company_id"])
        assert refused.value.status_code == 403
        await emit_event(
            s, company_id=c["company_id"], entity_id=_ITEM, entity_type="item", event_type="item.status.set",
            data={"new_status": "archived"}, actor_id=c["ids"]["operator"], location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()),
        )
        with pytest.raises(HTTPException):
            await s.commit()
        await s.rollback()
        end_request(s, authority)
    state, events = await _item(factory, c["company_id"], _ITEM)
    assert state["status"] == "available" and events == 1


def test_handed_settings_copy_as_plain_dicts():
    import copy

    from celerp.services.permissions import AuthoritySettings, RequestAuthority

    view = AuthoritySettings({"role_grants": {"edit_inventory": ["owner"]}})
    view.authority = RequestAuthority(company_id=uuid.uuid4(), user_id=uuid.uuid4(), role="owner")
    for clone in (copy.copy(view), copy.deepcopy(view), dict(view)):
        assert type(clone) is dict and clone == view


# Production run movements, each from the state that makes it valid: the steps an owner
# takes first, then the operator's movement on the run.
_RUN_MOVES = {
    "return": (("issue",), "return", lambda lot: {}),
    "undo_receipt": (("issue", "receive"), "undo-receipt", lambda lot: {"lot_item_id": lot}),
    "reopen": (("issue", "complete"), "reopen", lambda lot: {}),
    "cancel": ((), "cancel", lambda lot: {"reason": "not needed"}),
    "start": ((), "start", lambda lot: {}),
    "hold": ((), "hold", lambda lot: {"reason": "waiting"}),
    "resume": (("hold",), "resume", lambda lot: {}),
    "schedule": ((), "schedule", lambda lot: {"due_date": "2026-04-01"}),
}


async def _run_events(factory, company_id) -> tuple[dict, int]:
    async with factory() as s:
        runs = {r.entity_id: dict(r.state or {}) for r in (await s.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type.in_(("item", "mfg_order"))))).scalars()}
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id))).scalar_one()
    return runs, events


async def _seed_run(factory, client, c, steps) -> tuple[str, str]:
    """A run making 2 of a product from 10 of a component, taken through ``steps`` by the
    owner; returns the run and the first lot it received, if any."""
    from test_helpers import provision_company_books

    async with factory() as s:
        await provision_company_books(s, c["company_id"])
        await s.commit()
    owner_h = c["h"]["owner"]

    async def post(path, body):
        r = await client.post(path, json=body, headers=owner_h)
        assert r.status_code == 200, (path, r.text)
        return r.json()

    raw = (await post("/items", {"sku": "RAW", "name": "Raw", "quantity": 10, "sell_by": "piece",
                                 "status": "available", "cost_total": 100.0}))["id"]
    product = (await post("/items", {"sku": "FG", "name": "Made", "quantity": 0, "sell_by": "piece",
                                     "status": "available", "cost_total": 0.0}))["id"]
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=owner_h, json={
        "output_qty": 1, "components": [{"item_id": raw, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    order = (await post(f"/manufacturing/items/{product}/build", {"quantity": 2}))["id"]
    for step in steps:
        await post(f"/manufacturing/{order}/{step}", {"quantity": 1} if step == "receive" else {})
    async with factory() as s:
        state = (await s.get(Projection, {"company_id": c["company_id"], "entity_id": order})).state or {}
    return order, next(iter(state.get("received_lots") or []), "")


@pytest.mark.parametrize("move", sorted(_RUN_MOVES))
async def test_run_movement_refused_when_manufacturing_access_is_revoked_first(committed_engine, move):
    steps, path, body = _RUN_MOVES[move]
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        order, lot = await _seed_run(factory, client, c, steps)
        before = await _run_events(factory, c["company_id"])
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "manage_manufacturing", "operator")),
            lambda held: _http(pending, held, lambda: client.post(
                f"/manufacturing/{order}/{path}", json=body(lot), headers=c["h"]["operator"])),
        )
    _ok(changed)
    _refused(wrote, 403)
    assert await _run_events(factory, c["company_id"]) == before


@pytest.mark.parametrize("move", sorted(_RUN_MOVES))
async def test_run_movement_first_commits_and_the_revocation_waits(committed_engine, move):
    steps, path, body = _RUN_MOVES[move]
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        order, lot = await _seed_run(factory, client, c, steps)
        _, before = await _run_events(factory, c["company_id"])
        wrote, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, lambda: client.post(
                f"/manufacturing/{order}/{path}", json=body(lot), headers=c["h"]["operator"])),
            lambda held: _direct(factory, held, _revoke(c["company_id"], "manage_manufacturing", "operator")),
        )
    _ok(wrote)
    _ok(changed)
    _, after = await _run_events(factory, c["company_id"])
    assert after > before
