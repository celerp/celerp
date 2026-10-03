# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A draft item never newly lands on a document or List, and an item reserved elsewhere
never newly lands on an invoice or memo, whichever way the line arrives.

The rule is checked where every line write is recorded, so it holds for an import
(single and batch, document and List), an edit, a conversion and a duplicate as it does
for the create form. "Newly" counts lines, not ids: a record already holding an item
once may keep that line, but a second line for the same item is a new reference and is
checked like any other. Each case runs on real PostgreSQL and checks that a refused
write leaves the record and its history unchanged.
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

pytestmark = pytest.mark.asyncio

_ITEM = "item:x"
_HOLDER = "doc:HOLD"


async def _emit(s, company_id, user_id, entity_id, entity_type, event_type, data):
    await emit_event(s, company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                     event_type=event_type, data=data, actor_id=user_id, location_id=None,
                     source="test", idempotency_key=str(uuid.uuid4()))


def _line() -> dict:
    return {"item_id": _ITEM, "sku": "X", "name": "X", "quantity": 1, "unit_price": 10, "sell_by": "piece"}


async def _seed(engine, status: str):
    """A company with splittable item X, then X put into ``status``: a draft, or reserved by
    a finalized invoice that holds it."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Refs", slug=f"refs-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await _emit(s, company_id, user_id, _ITEM, "item", "item.created",
                    {"sku": "X", "name": "X", "quantity": 5, "sell_by": "piece", "status": "available"})
        await s.commit()
    return factory, company_id, types.SimpleNamespace(id=user_id)


async def _make(factory, company_id, user, status: str) -> None:
    async with factory() as s:
        if status == "draft":
            await _emit(s, company_id, user.id, _ITEM, "item", "item.status.set", {"new_status": "draft"})
        else:
            await _emit(s, company_id, user.id, _HOLDER, "doc", "doc.created",
                        {"doc_type": "invoice", "ref_id": "HOLD", "status": "final", "total": 10,
                         "subtotal": 10, "line_items": [_line()]})
            await _emit(s, company_id, user.id, _ITEM, "item", "item.status.set",
                        {"new_status": "reserved", "source_doc_id": _HOLDER, "doc_number": "HOLD"})
        await s.commit()


async def _records(engine, company_id) -> dict[str, int]:
    """Every document and List other than the holder, with how many lines link to X."""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT entity_id, state::jsonb -> 'line_items' FROM projections "
            "WHERE company_id = :c AND entity_type IN ('doc', 'list')"), {"c": company_id})).all()
    return {eid: sum((li.get("item_id") or li.get("entity_id")) == _ITEM for li in lines or [])
            for eid, lines in rows if eid != _HOLDER}


async def _events(engine, company_id, entity_id) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(
            "SELECT event_type FROM ledger WHERE company_id = :c AND entity_id = :e ORDER BY id"),
            {"c": company_id, "e": entity_id})).scalars())


async def _version(engine, company_id, entity_id) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT version FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": entity_id})).scalar_one()


async def _call(factory, fn):
    async with factory() as s:
        outcome = (await asyncio.gather(fn(s), return_exceptions=True))[0]
        if isinstance(outcome, BaseException):
            await s.rollback()
        else:
            await s.commit()
    return outcome


def _refused(outcome, needle: str) -> None:
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 422, outcome.detail
    assert needle in str(outcome.detail), outcome.detail


_REASON = {"draft": "is a draft", "reserved": "reserved on HOLD"}


def _record(kind: str, ref: str) -> dict:
    data = {"ref_id": ref, "status": "draft", "line_items": [_line()]}
    if kind == "doc":
        data.update(doc_type="invoice", total=10, subtotal=10)
    else:
        data["list_type"] = "quotation"
    return {"entity_id": f"{kind}:{ref}", "event_type": f"{kind}.created", "data": data,
            "source": "csv_import", "idempotency_key": f"imp-{ref}"}


# -- imports -------------------------------------------------------------------------

_IMPORTS = [
    pytest.param("doc", "draft", id="doc-draft"),
    pytest.param("doc", "reserved", id="doc-reserved"),
    pytest.param("list", "draft", id="list-draft"),
]


@pytest.mark.parametrize("kind, status", _IMPORTS)
async def test_a_single_import_cannot_add_an_ineligible_item(committed_engine, kind, status):
    factory, company_id, user = await _seed(committed_engine, status)
    await _make(factory, company_id, user, status)
    record = docs.DocImportRecord(**_record(kind, "IMP1"))
    route = docs.import_doc if kind == "doc" else docs.import_list

    outcome = await _call(factory, lambda s: route(record, company_id=company_id, _=None, __=None,
                                                   user=user, session=s))

    _refused(outcome, _REASON[status])
    assert await _records(committed_engine, company_id) == {}
    assert await _events(committed_engine, company_id, record.entity_id) == []


@pytest.mark.parametrize("kind, status", _IMPORTS)
async def test_a_batch_import_cannot_add_an_ineligible_item(committed_engine, kind, status):
    factory, company_id, user = await _seed(committed_engine, status)
    await _make(factory, company_id, user, status)
    clean = _record(kind, "IMP0")
    clean["data"]["line_items"] = [{"name": "Free text", "quantity": 1, "unit_price": 10}]
    body = docs.DocBatchImportRequest(records=[docs.DocImportRecord(**clean),
                                               docs.DocImportRecord(**_record(kind, "IMP1"))])
    route = docs.batch_import_docs if kind == "doc" else docs.batch_import_lists

    outcome = await _call(factory, lambda s: route(body, company_id=company_id, _=None, __=None,
                                                   user=user, session=s))

    assert not isinstance(outcome, BaseException), outcome
    assert outcome.created == 1, outcome
    assert any(_REASON[status] in e for e in outcome.errors), outcome.errors
    assert await _records(committed_engine, company_id) == {f"{kind}:IMP0": 0}
    assert await _events(committed_engine, company_id, f"{kind}:IMP1") == []


# -- a second line for an item the record already holds ------------------------------

async def test_a_second_line_for_an_item_reserved_elsewhere_is_refused(committed_engine):
    """Invoice A holds splittable X once; B then reserves X; A adding a second X line is a
    new reference to an item reserved on B."""
    factory, company_id, user = await _seed(committed_engine, "reserved")
    async with factory() as s:
        await _emit(s, company_id, user.id, "doc:A", "doc", "doc.created",
                    {"doc_type": "invoice", "ref_id": "A", "status": "draft", "total": 10,
                     "subtotal": 10, "line_items": [_line()]})
        await s.commit()
    await _make(factory, company_id, user, "reserved")
    before = await _events(committed_engine, company_id, "doc:A")
    payload = docs.DocPatch(fields_changed={"line_items": {"old": [_line()], "new": [_line(), _line()]},
                                            "subtotal": {"old": 10, "new": 20}, "total": {"old": 10, "new": 20}})

    outcome = await _call(factory, lambda s: docs.patch_doc(
        "doc:A", payload, company_id=company_id, _=None, role="admin", settings={}, user=user, session=s))

    _refused(outcome, _REASON["reserved"])
    assert await _records(committed_engine, company_id) == {"doc:A": 1}
    assert await _events(committed_engine, company_id, "doc:A") == before


async def test_a_second_line_for_an_item_now_a_draft_is_refused(committed_engine):
    """A List holds X once; X becomes a draft; a second X line is a new reference to a draft."""
    factory, company_id, user = await _seed(committed_engine, "draft")
    async with factory() as s:
        await _emit(s, company_id, user.id, "list:L", "list", "list.created",
                    {"list_type": "quotation", "ref_id": "L", "status": "draft", "line_items": [_line()]})
        await s.commit()
    await _make(factory, company_id, user, "draft")
    before = await _events(committed_engine, company_id, "list:L")
    payload = docs.ListPatch(fields_changed={"line_items": {"old": [_line()], "new": [_line(), _line()]}},
                             expected_version=await _version(committed_engine, company_id, "list:L"))

    outcome = await _call(factory, lambda s: docs.patch_list(
        "list:L", payload, company_id=company_id, _=None, role="admin", settings={}, user=user, session=s))

    _refused(outcome, _REASON["draft"])
    assert await _records(committed_engine, company_id) == {"list:L": 1}
    assert await _events(committed_engine, company_id, "list:L") == before


async def test_the_line_a_record_already_holds_stays_editable(committed_engine):
    """The draft rule judges new lines only: a List holding X before X became a draft can
    still be saved with that one line."""
    factory, company_id, user = await _seed(committed_engine, "draft")
    async with factory() as s:
        await _emit(s, company_id, user.id, "list:L", "list", "list.created",
                    {"list_type": "quotation", "ref_id": "L", "status": "draft", "line_items": [_line()]})
        await s.commit()
    await _make(factory, company_id, user, "draft")
    edited = {**_line(), "name": "X renamed"}
    payload = docs.ListPatch(fields_changed={"line_items": {"old": [_line()], "new": [edited]}},
                             expected_version=await _version(committed_engine, company_id, "list:L"))

    outcome = await _call(factory, lambda s: docs.patch_list(
        "list:L", payload, company_id=company_id, _=None, role="admin", settings={}, user=user, session=s))

    assert not isinstance(outcome, BaseException), outcome
    assert await _records(committed_engine, company_id) == {"list:L": 1}


# -- conversions and copies make new records -----------------------------------------

async def _finalized_list(factory, company_id, user) -> None:
    async with factory() as s:
        await _emit(s, company_id, user.id, "list:L", "list", "list.created",
                    {"list_type": "quotation", "ref_id": "L", "status": "finalized", "line_items": [_line()]})
        await s.commit()


async def test_a_list_holding_an_item_now_a_draft_cannot_be_converted(committed_engine):
    factory, company_id, user = await _seed(committed_engine, "draft")
    await _finalized_list(factory, company_id, user)
    await _make(factory, company_id, user, "draft")

    outcome = await _call(factory, lambda s: docs.convert_list(
        "list:L", docs.ListConvertBody(target_type="invoice"), company_id=company_id, _=None,
        user=user, session=s))

    _refused(outcome, _REASON["draft"])
    assert await _records(committed_engine, company_id) == {"list:L": 1}


async def test_a_list_holding_an_item_now_a_draft_cannot_be_duplicated(committed_engine):
    factory, company_id, user = await _seed(committed_engine, "draft")
    await _finalized_list(factory, company_id, user)
    await _make(factory, company_id, user, "draft")

    outcome = await _call(factory, lambda s: docs.duplicate_list(
        "list:L", company_id=company_id, _=None, user=user, session=s))

    _refused(outcome, _REASON["draft"])
    assert await _records(committed_engine, company_id) == {"list:L": 1}


async def test_a_list_converts_with_the_reservation_it_holds(committed_engine):
    """The List's own reservation moves to the new invoice; it is not a foreign one."""
    factory, company_id, user = await _seed(committed_engine, "reserved")
    await _finalized_list(factory, company_id, user)
    async with factory() as s:
        await _emit(s, company_id, user.id, _ITEM, "item", "item.status.set",
                    {"new_status": "reserved", "source_doc_id": "list:L", "doc_number": "L"})
        await s.commit()

    outcome = await _call(factory, lambda s: docs.convert_list(
        "list:L", docs.ListConvertBody(target_type="invoice"), company_id=company_id, _=None,
        user=user, session=s))

    assert not isinstance(outcome, BaseException), outcome
    new_id = outcome["target_doc_id"]
    assert (await _records(committed_engine, company_id))[new_id] == 1
    async with committed_engine.connect() as conn:
        owner = (await conn.execute(text(
            "SELECT state::jsonb ->> 'status_doc_id' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one()
    assert owner == new_id
