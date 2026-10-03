# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A new document never takes an item that is a draft or that another document reserved.

Reverting an item to draft and reserving it on another document each change the item
while a document is being created. Whichever commits first, the result is one a user
could reach by doing the two one after the other: the document checks the item as the
other action left it, or the other action sees the document. Each case runs on real
PostgreSQL with a fixed interleaving across two connections, in both orders.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events import engine
from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp_docs import routes as docs
from celerp_inventory import routes as inventory

pytestmark = pytest.mark.asyncio

_ITEM = "item:lot"


async def _seed(factory) -> tuple[uuid.UUID, types.SimpleNamespace]:
    """A company with one available item."""
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="State", slug=f"state-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await emit_event(
            s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.created",
            data={"sku": "LOT", "name": "Lot", "quantity": 1, "sell_by": "piece", "status": "available"},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


def _line() -> dict:
    return {"item_id": _ITEM, "sku": "LOT", "name": "Lot", "quantity": 1, "unit_price": 10, "sell_by": "piece"}


def _create(s, company_id, user):
    payload = docs.DocCreatePayload(doc_type="invoice", line_items=[docs.LineItem(**_line())],
                                    subtotal=10, total=10)
    return docs.create_doc(payload, company_id=company_id, _=None, role="admin", settings={},
                           user=user, session=s)


def _revert(s, company_id, user):
    return inventory.bulk_revert_to_draft(
        inventory.RevertToDraftBody(entity_ids=[_ITEM]), company_id=company_id, _=None, user=user,
        role="admin", settings={}, session=s)


def _reserve(s, company_id, user, holder: str):
    return docs.reserve_lines(holder, docs.ReserveLinesRequest(new_status="reserved", line_entity_ids=[_ITEM]),
                              company_id=company_id, _=None, user=user, session=s)


async def _holder(factory, company_id, user) -> str:
    """Another issued invoice already listing the item, which can reserve it."""
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id="doc:HOLD", entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "ref_id": "HOLD", "status": "final", "line_items": [_line()],
                  "subtotal": 10, "total": 10},
            actor_id=user.id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return "doc:HOLD"


async def _until_blocked(engine, task: asyncio.Task) -> None:
    """Wait until ``task`` waits on a row lock (each poll is its own snapshot)."""
    for _ in range(400):
        assert not task.done(), f"the call did not wait: {task.exception()!r}"
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the call never waited")


async def _docs(engine, company_id) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(
            "SELECT entity_id FROM projections WHERE company_id = :c AND entity_type = 'doc' "
            "ORDER BY entity_id"), {"c": company_id})).scalars().all())


async def _item(engine, company_id) -> dict:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT state::jsonb FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one()


def _pause(monkeypatch, module, name: str, session) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold ``session``'s call of ``module.name`` until released; other sessions pass through."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(module, name)

    async def _held(s, *args, **kwargs):
        if s is session:
            reached.set()
            await release.wait()
        return await real(s, *args, **kwargs)

    monkeypatch.setattr(module, name, _held)
    return reached, release


async def test_revert_to_draft_while_a_document_checks_its_lines_waits_and_is_refused(committed_engine, monkeypatch):
    """The document checks the item first: Revert to Draft waits for it, then finds the
    item on the document and refuses."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)

    async with factory() as creator, factory() as reverter:
        reached, release = _pause(monkeypatch, engine, "assert_document_item_uniqueness", creator)
        create = asyncio.create_task(_create(creator, company_id, user))
        await asyncio.wait_for(reached.wait(), timeout=30)
        revert = asyncio.create_task(_revert(reverter, company_id, user))
        await _until_blocked(committed_engine, revert)
        release.set()
        created = await asyncio.wait_for(create, timeout=30)
        outcome = (await asyncio.gather(asyncio.wait_for(revert, timeout=30), return_exceptions=True))[0]

    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 409 and "is on document" in outcome.detail
    assert await _docs(committed_engine, company_id) == [created["id"]]
    assert (await _item(committed_engine, company_id))["status"] == "available"


async def test_a_document_created_while_the_item_is_reverted_to_draft_is_refused(committed_engine, monkeypatch):
    """Revert to Draft holds the item first: the document waits, then sees a draft and
    refuses, writing nothing."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)

    async with factory() as reverter, factory() as creator:
        reached, release = _pause(monkeypatch, inventory, "assert_status_change_allowed", reverter)
        revert = asyncio.create_task(_revert(reverter, company_id, user))
        await asyncio.wait_for(reached.wait(), timeout=30)
        create = asyncio.create_task(_create(creator, company_id, user))
        await _until_blocked(committed_engine, create)
        release.set()
        reverted = await asyncio.wait_for(revert, timeout=30)
        outcome = (await asyncio.gather(asyncio.wait_for(create, timeout=30), return_exceptions=True))[0]
        await creator.rollback()

    assert reverted["updated"] == 1
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 422 and "item is a draft" in outcome.detail["message"]
    assert await _docs(committed_engine, company_id) == []
    assert (await _item(committed_engine, company_id))["status"] == "draft"


async def test_reserving_elsewhere_while_a_document_checks_its_lines_waits_for_it(committed_engine, monkeypatch):
    """The document checks the item first: the other document's Reserve waits for it and
    then reserves, as it would one step later."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    holder = await _holder(factory, company_id, user)

    async with factory() as creator, factory() as reserver:
        reached, release = _pause(monkeypatch, engine, "assert_document_item_uniqueness", creator)
        create = asyncio.create_task(_create(creator, company_id, user))
        await asyncio.wait_for(reached.wait(), timeout=30)
        reserve = asyncio.create_task(_reserve(reserver, company_id, user, holder))
        await _until_blocked(committed_engine, reserve)
        release.set()
        created = await asyncio.wait_for(create, timeout=30)
        await asyncio.wait_for(reserve, timeout=30)

    assert sorted(await _docs(committed_engine, company_id)) == sorted([holder, created["id"]])
    item = await _item(committed_engine, company_id)
    assert (item["status"], item["status_doc_id"]) == ("reserved", holder)


async def test_a_document_created_while_another_reserves_the_item_is_refused(committed_engine, monkeypatch):
    """The other document's Reserve holds the item first: the new document waits, then
    sees the item reserved elsewhere and refuses, naming the holder."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    holder = await _holder(factory, company_id, user)

    async with factory() as reserver, factory() as creator:
        reached, release = _pause(monkeypatch, docs, "_get_unit_map", reserver)
        reserve = asyncio.create_task(_reserve(reserver, company_id, user, holder))
        await asyncio.wait_for(reached.wait(), timeout=30)
        create = asyncio.create_task(_create(creator, company_id, user))
        await _until_blocked(committed_engine, create)
        release.set()
        await asyncio.wait_for(reserve, timeout=30)
        outcome = (await asyncio.gather(asyncio.wait_for(create, timeout=30), return_exceptions=True))[0]
        await creator.rollback()

    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 422
    assert [c["doc_id"] for c in outcome.detail["conflicts"]] == [holder]
    assert await _docs(committed_engine, company_id) == [holder]
    item = await _item(committed_engine, company_id)
    assert (item["status"], item["status_doc_id"]) == ("reserved", holder)


def _credit_note(s, company_id, user, original: str, amount: float):
    payload = docs.DocCreatePayload(doc_type="credit_note", original_doc_id=original,
                                    subtotal=amount, total=amount)
    return docs.create_doc(payload, company_id=company_id, _=None, role="admin", settings={},
                           user=user, session=s)


async def test_two_credit_notes_at_once_both_reduce_what_the_invoice_owes(committed_engine, monkeypatch):
    """A credit note reads its invoice's balance after the other one has reduced it, so
    neither reduction is lost."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id="doc:INV", entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "ref_id": "INV", "status": "final", "line_items": [],
                  "subtotal": 100, "total": 100, "amount_paid": 0, "amount_outstanding": 100},
            actor_id=user.id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()

    async with factory() as first, factory() as second:
        reached, release = _pause(monkeypatch, docs, "_lock_selected_contact", first)
        late = asyncio.create_task(_credit_note(first, company_id, user, "doc:INV", 30))
        await asyncio.wait_for(reached.wait(), timeout=30)
        await _credit_note(second, company_id, user, "doc:INV", 20)
        release.set()
        await asyncio.wait_for(late, timeout=30)

    async with committed_engine.connect() as conn:
        owed = (await conn.execute(text(
            "SELECT (state::jsonb ->> 'amount_outstanding')::numeric FROM projections "
            "WHERE company_id = :c AND entity_id = 'doc:INV'"), {"c": company_id})).scalar_one()
    assert owed == 50
