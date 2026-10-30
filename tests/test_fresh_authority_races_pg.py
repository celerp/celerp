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
from sqlalchemy import String, cast, func, select, text
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

pytestmark = pytest.mark.process

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
        s.add(Company(id=company_id, name="Access", slug=f"acc-{company_id.hex[:8]}", settings=settings))
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


_INVOICE = {"doc_type": "invoice", "line_items": [
    {"item_id": _ITEM, "sku": _ITEM, "name": _ITEM, "quantity": 1, "unit_price": 2, "sell_by": "piece"}]}


@pytest.fixture
def module_routes(monkeypatch):
    """Registers a bundled module's routes with the loader as that module's own, as at
    startup, so a request to one is judged by whether the company uses the module."""
    import importlib
    from pathlib import Path

    import default_modules
    from fastapi import FastAPI
    from celerp.modules import loader

    def own(name: str) -> None:
        manifest = loader.read_manifest(Path(default_modules.__file__).parent / name)
        routes = FastAPI()
        importlib.import_module(manifest["api_routes"]).setup_api_routes(routes)
        monkeypatch.setitem(loader._module_routes, name, list(routes.routes))
    return own


@pytest.fixture
def docs_routes(module_routes):
    module_routes("celerp-docs")


def _turn_off(company_id, module):
    async def _go(s):
        return await companies.disable_module(module, company_id=company_id, session=s)
    return _go


async def _company_state(factory, company_id) -> tuple[int, dict]:
    async with factory() as s:
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id))).scalar_one()
        rows = (await s.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == company_id))).all()
    return events, {entity_id: state for entity_id, state in rows}


async def _documents_writes(client, headers) -> dict:
    """Each write a Documents route makes, as (method, path, body), with the document, list
    and notes it acts on already in place."""
    async def made(path, body):
        r = await client.post(path, json=body, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    doc = await made("/docs", _INVOICE)
    lst = await made("/lists", {"list_type": "shipping_doc", "contact_name": "Buyer",
                                "line_items": [{"item_id": _ITEM, "sku": _ITEM, "name": _ITEM, "quantity": 1}]})
    doc_note = await made(f"/docs/{doc}/notes", {"note": "First"})
    list_note = await made(f"/lists/{lst}/notes", {"note": "First"})
    return {
        "create_document": ("post", "/docs", _INVOICE),
        "add_document_note": ("post", f"/docs/{doc}/notes", {"note": "Added"}),
        "edit_document_note": ("patch", f"/docs/{doc}/notes/{doc_note}", {"note": "Edited"}),
        "delete_document_note": ("delete", f"/docs/{doc}/notes/{doc_note}", None),
        "add_list_note": ("post", f"/lists/{lst}/notes", {"note": "Added"}),
        "edit_list_note": ("patch", f"/lists/{lst}/notes/{list_note}", {"note": "Edited"}),
        "delete_list_note": ("delete", f"/lists/{lst}/notes/{list_note}", None),
    }


_DOCUMENTS_WRITES = ("create_document", "add_document_note", "edit_document_note", "delete_document_note",
                     "add_list_note", "edit_list_note", "delete_list_note")


def _send(client, headers, write):
    method, path, body = write
    if body is None:
        return lambda: getattr(client, method)(path, headers=headers)
    return lambda: getattr(client, method)(path, json=body, headers=headers)


@pytest.mark.parametrize("write", _DOCUMENTS_WRITES)
async def test_a_documents_write_waiting_while_documents_is_turned_off_is_refused(committed_engine, docs_routes, write):
    """A Documents write authorized while the company uses Documents, that reaches the company
    lock after Documents is turned off for it, is refused and writes nothing. Another company
    using Documents is unaffected."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c, other = await _seed(factory), await _seed(factory)
    async with _app_client(factory) as (client, pending):
        writes = await _documents_writes(client, c["h"]["owner"])
        other_writes = await _documents_writes(client, other["h"]["owner"])
        before = await _company_state(factory, c["company_id"])
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _turn_off(c["company_id"], "celerp-docs")),
            lambda held: _http(pending, held, _send(client, c["h"]["owner"], writes[write])),
        )
        _ok(changed)
        _refused(wrote, 403)
        assert wrote.json()["detail"]["message_key"] == "modules.off_for_company"
        assert await _company_state(factory, c["company_id"]) == before
        _ok(await _send(client, other["h"]["owner"], other_writes[write])())


@pytest.mark.parametrize("write", _DOCUMENTS_WRITES)
async def test_a_documents_write_holding_the_lock_first_commits_before_documents_is_turned_off(
        committed_engine, docs_routes, write):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        writes = await _documents_writes(client, c["h"]["owner"])
        before = await _company_state(factory, c["company_id"])
        wrote, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, _send(client, c["h"]["owner"], writes[write])),
            lambda held: _direct(factory, held, _turn_off(c["company_id"], "celerp-docs")),
        )
    _ok(wrote)
    _ok(changed)
    assert (await _company_state(factory, c["company_id"]))[0] > before[0]


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


# Writes of the modules whose routes take the company lock themselves, each as (module,
# permission, setup). ``setup`` puts in place what the write acts on, as the owner, and
# returns the write as (method, path, body).
async def _owner_post(client, c, path, body) -> dict:
    r = await client.post(path, json=body, headers=c["h"]["owner"])
    assert r.status_code == 200, (path, r.text)
    return r.json()


def _journal(debit: str, credit: str) -> dict:
    return {"ts": "2026-03-05", "memo": "Race", "idempotency_token": uuid.uuid4().hex, "entries": [
        {"account": debit, "debit": 5, "credit": 0}, {"account": credit, "debit": 0, "credit": 5}]}


async def _books(factory, c) -> None:
    from test_helpers import provision_company_books

    async with factory() as s:
        await provision_company_books(s, c["company_id"])
        await s.commit()


async def _manual_journal(factory, client, c):
    await _books(factory, c)
    return "post", "/accounting/journal-entries", _journal("6200", "4100")


async def _reconciliation_match(factory, client, c):
    await _books(factory, c)
    bank = await _owner_post(client, c, "/accounting/bank-accounts", {
        "bank_name": "Main", "account_number": "1", "bank_type": "checking", "currency": "THB",
        "opening_balance": 100.0})
    je = (await _owner_post(client, c, "/accounting/journal-entries",
                            _journal(bank["chart_account_code"], "4100")))["je_id"]
    recon = await _owner_post(client, c, "/accounting/reconciliation/start", {
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 105.0})
    return "post", f"/accounting/reconciliation/{recon['id']}/match", {"je_ids": [je]}


async def _contact_create(factory, client, c):
    return "post", "/crm/contacts", {"name": "Buyer"}


async def _contact_note(factory, client, c):
    contact = (await _owner_post(client, c, "/crm/contacts", {"name": "Buyer"}))["id"]
    return "post", f"/crm/contacts/{contact}/notes", {"note": "Called"}


async def _work_center_create(factory, client, c):
    return "post", "/manufacturing/work-centers", {"name": "Bench"}


async def _work_center_patch(factory, client, c):
    wc = (await _owner_post(client, c, "/manufacturing/work-centers", {"name": "Bench"}))["id"]
    return "patch", f"/manufacturing/work-centers/{wc}", {"name": "Bench 2", "labor_rate": 12}


_BOUNDARY_WRITES = {
    "manual_journal": ("celerp-accounting", "manage_accounting", _manual_journal),
    "reconciliation_match": ("celerp-accounting", "manage_accounting", _reconciliation_match),
    "contact_create": ("celerp-contacts", "edit_contacts", _contact_create),
    "contact_note": ("celerp-contacts", "edit_contacts", _contact_note),
    "work_center_create": ("celerp-manufacturing", "manage_manufacturing", _work_center_create),
    "work_center_patch": ("celerp-manufacturing", "manage_manufacturing", _work_center_patch),
}
_ALL_ROLES = ["operator", "manager", "admin", "owner"]


async def _company_rows(factory, company_id) -> dict:
    """Every row each company-owned table holds for the company, so a refused write is
    seen to leave nothing behind in any table, not only the ledger."""
    from celerp.models.base import Base

    rows = {}
    async with factory() as s:
        for table in Base.metadata.sorted_tables:
            if "company_id" in table.c:
                found = (await s.execute(select(table).where(
                    cast(table.c.company_id, String) == str(company_id)))).all()
                rows[table.name] = sorted(map(repr, found))
    return rows


async def _boundary_setup(factory, client, module_routes, name, modules=None):
    module, perm, setup = _BOUNDARY_WRITES[name]
    module_routes(module)
    c = await _seed(factory, grants={perm: _ALL_ROLES}, modules=modules)
    return c, perm, await setup(factory, client, c)


@pytest.mark.parametrize("name", sorted(_BOUNDARY_WRITES))
async def test_a_module_write_waiting_while_its_permission_is_revoked_is_refused(
        committed_engine, module_routes, name):
    """The write was authorized when it was sent; the permission is revoked while it waits
    for the company lock. Once it has the lock it is judged again, refused, and every table
    is as it was."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with _app_client(factory) as (client, pending):
        c, perm, write = await _boundary_setup(factory, client, module_routes, name)
        before = await _company_rows(factory, c["company_id"])
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], perm, "operator")),
            lambda held: _http(pending, held, _send(client, c["h"]["operator"], write)),
        )
    _ok(changed)
    _refused(wrote, 403)
    assert await _company_rows(factory, c["company_id"]) == before


@pytest.mark.parametrize("name", sorted(_BOUNDARY_WRITES))
async def test_a_module_write_holding_the_lock_first_commits_and_the_revocation_waits(
        committed_engine, module_routes, name):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with _app_client(factory) as (client, pending):
        c, perm, write = await _boundary_setup(factory, client, module_routes, name)
        before = await _company_rows(factory, c["company_id"])
        wrote, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, _send(client, c["h"]["operator"], write)),
            lambda held: _direct(factory, held, _revoke(c["company_id"], perm, "operator")),
        )
        _ok(wrote)
        _ok(changed)
        assert await _company_rows(factory, c["company_id"]) != before
        _refused(await _send(client, c["h"]["operator"], write)(), 403)


@pytest.mark.parametrize("name", sorted(_BOUNDARY_WRITES))
async def test_a_module_write_waiting_while_its_module_is_turned_off_is_refused(
        committed_engine, module_routes, name):
    """The company uses only the write's module and the ones it needs, so the module can be
    turned off; that lands while the write waits, and the write is refused and leaves every
    table as it was. A company that keeps the module is unaffected."""
    module = _BOUNDARY_WRITES[name][0]
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with _app_client(factory) as (client, pending):
        c, _, write = await _boundary_setup(factory, client, module_routes, name, modules=[module])
        other, _, other_write = await _boundary_setup(factory, client, module_routes, name, modules=[module])
        before = await _company_rows(factory, c["company_id"])
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _turn_off(c["company_id"], module)),
            lambda held: _http(pending, held, _send(client, c["h"]["owner"], write)),
        )
        _ok(changed)
        _refused(wrote, 403)
        assert wrote.json()["detail"]["message_key"] == "modules.off_for_company"
        assert await _company_rows(factory, c["company_id"]) == before
        _ok(await _send(client, other["h"]["owner"], other_write)())


@pytest.mark.parametrize("name", sorted(_BOUNDARY_WRITES))
async def test_a_module_write_holding_the_lock_first_commits_before_its_module_is_turned_off(
        committed_engine, module_routes, name):
    module = _BOUNDARY_WRITES[name][0]
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with _app_client(factory) as (client, pending):
        c, _, write = await _boundary_setup(factory, client, module_routes, name, modules=[module])
        before = await _company_rows(factory, c["company_id"])
        wrote, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, _send(client, c["h"]["owner"], write)),
            lambda held: _direct(factory, held, _turn_off(c["company_id"], module)),
        )
        _ok(wrote)
        _ok(changed)
        assert await _company_rows(factory, c["company_id"]) != before


async def test_concurrent_writes_to_one_company_all_land(committed_engine, module_routes):
    """Writes of three modules sent to one company at once queue on its lock: every one
    commits, none deadlocks, and each leaves its own row."""
    for module in ("celerp-accounting", "celerp-contacts", "celerp-manufacturing"):
        module_routes(module)
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory, grants={perm: _ALL_ROLES for _, perm, _ in _BOUNDARY_WRITES.values()})
    await _books(factory, c)
    h = c["h"]["operator"]
    async with _app_client(factory) as (client, _):
        sends = [client.post("/accounting/journal-entries", json=_journal("6200", "4100"), headers=h)
                 for _ in range(6)]
        sends += [client.post("/crm/contacts", json={"name": f"Buyer {n}"}, headers=h) for n in range(6)]
        sends += [client.post("/manufacturing/work-centers", json={"name": f"Bench {n}"}, headers=h)
                  for n in range(6)]
        outcomes = await asyncio.wait_for(asyncio.gather(*sends, return_exceptions=True), 60)
    for outcome in outcomes:
        _ok(outcome)
    async with factory() as s:
        kinds = dict((await s.execute(
            select(Projection.entity_type, func.count()).where(Projection.company_id == c["company_id"])
            .group_by(Projection.entity_type))).all())
    assert len({o.json()["id"] for o in outcomes[12:]}) == 6
    assert kinds.get("contact") == 6, kinds
    assert len({o.json()["je_id"] for o in outcomes[:6]}) == 6


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
