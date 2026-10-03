# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A draft item, or an item reserved elsewhere, never lands on a new document or List.

Creating an invoice or a List checks each line's item: a draft is not stock yet, and an
invoice cannot take an item another document has reserved. An item can become a draft
(Revert to Draft) or reserved (on another document, on a List, or by a status edit)
while the create is running. Whichever commits first, the other must see it: the create
is refused, or the change is refused, or (for a reservation made after the create) both
stand in that order. Each race runs on real PostgreSQL across two connections, in both
orders, with the first writer holding its transaction open until the second has started.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp_docs import routes as docs
from celerp_inventory import routes as inventory

pytestmark = pytest.mark.asyncio

_ITEM = "item:x"


async def _seed(factory) -> tuple[uuid.UUID, types.SimpleNamespace]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Races", slug=f"races-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await emit_event(
            s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.created",
            data={"sku": "X", "name": "X", "quantity": 1, "sell_by": "piece", "status": "available"},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


def _line() -> dict:
    return {"item_id": _ITEM, "sku": "X", "name": "X", "quantity": 1, "unit_price": 10, "sell_by": "piece"}


async def _holder(factory, company_id, user, kind: str) -> str:
    """A finalized invoice or a draft List already listing the item, able to reserve it."""
    entity_id = f"{kind}:HOLD"
    data = {"ref_id": "HOLD", "status": "final" if kind == "doc" else "draft", "line_items": [_line()]}
    if kind == "doc":
        data.update(doc_type="invoice", total=10, subtotal=10)
    else:
        data["list_type"] = "quotation"
    async with factory() as s:
        await emit_event(s, company_id=company_id, entity_id=entity_id, entity_type=kind,
                         event_type=f"{kind}.created", data=data, actor_id=user.id, location_id=None,
                         source="test", idempotency_key=str(uuid.uuid4()))
        await s.commit()
    return entity_id


# -- the writers that put the item on a new record -------------------------------------

def _create_doc(s, company_id, user):
    payload = docs.DocCreatePayload(doc_type="invoice", line_items=[docs.LineItem(**_line())],
                                    subtotal=10, total=10)
    return docs.create_doc(payload, company_id=company_id, _=None, role="admin", settings={},
                           user=user, session=s)


def _create_list(s, company_id, user):
    payload = docs.ListCreatePayload(list_type="quotation", line_items=[_line()])
    return docs.create_list(payload, company_id=company_id, _=None, role="admin", settings={},
                            user=user, session=s)


# -- the changes that make the item unusable on it -------------------------------------

def _revert(s, company_id, user, _holder_id):
    return inventory.bulk_revert_to_draft(inventory.RevertToDraftBody(entity_ids=[_ITEM]), company_id=company_id,
                                          _=None, user=user, role="admin", settings={}, session=s)


def _reserve_on_doc(s, company_id, user, holder_id):
    body = docs.ReserveLinesRequest(new_status="reserved", line_entity_ids=[_ITEM])
    return docs.reserve_lines(holder_id, body, company_id=company_id, _=None, user=user, session=s)


def _reserve_on_list(s, company_id, user, holder_id):
    body = docs.ReserveLinesRequest(new_status="reserved", line_entity_ids=[_ITEM])
    return docs.reserve_list_lines(holder_id, body, company_id=company_id, _=None, user=user, session=s)


async def _reserved_by_no_document(s, company_id, user) -> None:
    """A reservation no document holds, as an older release's status edit recorded it;
    a status edit can no longer reserve an item."""
    await emit_event(
        s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.status.set",
        data={"new_status": "reserved"}, actor_id=user.id, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()),
    )
    await s.commit()


# (writer, change, holder kind, the status the change leaves the item in)
_RACES = [
    pytest.param(_create_doc, _revert, None, "draft", id="invoice-vs-revert-to-draft"),
    pytest.param(_create_list, _revert, None, "draft", id="list-vs-revert-to-draft"),
    pytest.param(_create_doc, _reserve_on_doc, "doc", "reserved", id="invoice-vs-reserve-on-invoice"),
    pytest.param(_create_doc, _reserve_on_list, "list", "reserved", id="invoice-vs-reserve-on-list"),
]


async def _deferred() -> None:
    """Stands in for a commit the test makes itself, later."""


async def _until_waiting_or_done(engine, task: asyncio.Task) -> None:
    """Let ``task`` run until it waits on a lock or finishes (each poll is its own snapshot)."""
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
    raise AssertionError("the call neither waited nor finished")


async def _race(factory, engine, first, second):
    """Run ``first`` to the end of its work with its commit held back, start ``second``,
    then commit ``first``. Returns both outcomes (a result or the raised exception)."""
    async with factory() as a, factory() as b:
        commit_a, commit_b = a.commit, b.commit
        a.commit = _deferred
        b.commit = _deferred
        one = await asyncio.gather(first(a), return_exceptions=True)
        task = asyncio.create_task(second(b))
        await _until_waiting_or_done(engine, task)
        await commit_a()
        two = await asyncio.gather(asyncio.wait_for(task, timeout=30), return_exceptions=True)
        if isinstance(two[0], BaseException):
            await b.rollback()
        else:
            await commit_b()
    return one[0], two[0]


async def _item_status(engine, company_id) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT state::jsonb ->> 'status' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one()


async def _records_listing_the_item(engine, company_id, holder_id) -> list[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT entity_id, state::jsonb -> 'line_items' FROM projections "
            "WHERE company_id = :c AND entity_type IN ('doc', 'list')"), {"c": company_id})).all()
    return sorted(eid for eid, lines in rows if eid != holder_id
                  and any((li.get("item_id") or li.get("entity_id")) == _ITEM for li in lines or []))


def _refused(outcome, status: int) -> None:
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == status, outcome.detail


@pytest.mark.parametrize("writer, change, holder_kind, changed_status", _RACES)
async def test_a_change_made_first_refuses_the_create(committed_engine, writer, change, holder_kind, changed_status):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    holder_id = await _holder(factory, company_id, user, holder_kind) if holder_kind else None

    changed, created = await _race(
        factory, committed_engine,
        lambda s: change(s, company_id, user, holder_id),
        lambda s: writer(s, company_id, user),
    )

    assert not isinstance(changed, BaseException), changed
    _refused(created, 422)
    assert await _item_status(committed_engine, company_id) == changed_status
    assert await _records_listing_the_item(committed_engine, company_id, holder_id) == []


@pytest.mark.parametrize("writer, change, holder_kind, changed_status", _RACES)
async def test_a_create_made_first_is_seen_by_the_change(committed_engine, writer, change, holder_kind, changed_status):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    holder_id = await _holder(factory, company_id, user, holder_kind) if holder_kind else None

    created, changed = await _race(
        factory, committed_engine,
        lambda s: writer(s, company_id, user),
        lambda s: change(s, company_id, user, holder_id),
    )

    assert not isinstance(created, BaseException), created
    assert await _records_listing_the_item(committed_engine, company_id, holder_id) == [created["id"]]
    if changed_status == "draft":
        # The item is on the new record now, so it can no longer go back to draft.
        _refused(changed, 409)
        assert await _item_status(committed_engine, company_id) == "available"
    else:
        # Reserving an item another record merely lists is allowed after the fact.
        assert not isinstance(changed, BaseException), changed
        assert await _item_status(committed_engine, company_id) == "reserved"


async def test_an_item_reserved_by_a_status_edit_cannot_go_on_a_new_invoice(committed_engine):
    """A reservation held by no document is still not the new invoice's own, as an edit
    adding the same line already treats it."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    async with factory() as s:
        await _reserved_by_no_document(s, company_id, user)

    async with factory() as s:
        outcome = (await asyncio.gather(_create_doc(s, company_id, user), return_exceptions=True))[0]
        await s.rollback()

    _refused(outcome, 422)
    assert await _records_listing_the_item(committed_engine, company_id, None) == []
