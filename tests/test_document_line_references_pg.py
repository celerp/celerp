# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A document or List can only gain a line for an item that exists.

Undoing an import removes its items. After that, no save may add a line for one of
them: not a form opened before the Undo, not a document created while the Undo runs,
not an edit, not an import. Each case is on real PostgreSQL; the races use a fixed
interleaving across two connections, in both orders.
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
from celerp.services.company_lock import lock_company
from celerp.services.item_erasure import erase_items
from celerp_docs import routes as docs
from celerp_inventory import routes as inventory
from celerp_inventory.models_import_batch import ImportBatch

pytestmark = pytest.mark.asyncio

_ITEM = "item:undone"
_KEPT = "item:kept"


async def _seed(factory) -> tuple[uuid.UUID, types.SimpleNamespace, uuid.UUID]:
    """A company with one kept item and one imported item whose import can be undone."""
    company_id, user_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"import:{uuid.uuid4().hex}"
    async with factory() as s:
        s.add(Company(id=company_id, name="Refs", slug=f"refs-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        for entity_id, sku, idem in ((_KEPT, "KEPT", str(uuid.uuid4())), (_ITEM, "UNDONE", key)):
            await emit_event(
                s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.created",
                data={"sku": sku, "name": sku.title(), "quantity": 1, "sell_by": "piece"},
                actor_id=user_id, location_id=None, source="csv_import", idempotency_key=idem,
            )
        s.add(ImportBatch(id=batch_id, company_id=company_id, entity_type="item", filename="undone.csv",
                          row_count=1, entity_ids=[_ITEM], idempotency_keys=[key], reversible=True))
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id), batch_id


def _line(entity_id: str) -> dict:
    return {"item_id": entity_id, "sku": entity_id.split(":")[1].upper(), "name": entity_id,
            "quantity": 1, "unit_price": 10, "sell_by": "piece"}


def _undo(s, company_id, user, batch_id):
    return inventory.undo_import_batch(str(batch_id), company_id=company_id, user=user, session=s)


def _create(s, company_id, user, *entity_ids):
    payload = docs.DocCreatePayload(
        doc_type="invoice", line_items=[docs.LineItem(**_line(e)) for e in entity_ids],
        subtotal=10 * len(entity_ids), total=10 * len(entity_ids),
    )
    return docs.create_doc(payload, company_id=company_id, _=None, role="admin", settings={},
                           user=user, session=s)


def _patch(s, company_id, user, entity_id, old_lines, new_lines, **extra):
    changed = {"line_items": {"old": old_lines, "new": new_lines}, **extra}
    return docs.patch_doc(entity_id, docs.DocPatch(fields_changed=changed), company_id=company_id,
                          _=None, role="admin", settings={}, user=user, session=s)


async def _undone(factory, company_id, user, batch_id) -> None:
    async with factory() as s:
        assert await _undo(s, company_id, user, batch_id) == {"ok": True, "removed": 1}


async def _deferred() -> None:
    """Stands in for a commit the test makes itself, later."""


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


async def _records(engine, company_id, entity_type: str) -> list:
    """(entity_id, line item ids) of every stored record of one type."""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT entity_id, state::jsonb -> 'line_items' FROM projections "
            "WHERE company_id = :c AND entity_type = :t ORDER BY entity_id"),
            {"c": company_id, "t": entity_type})).all()
    return [(eid, [li.get("item_id") or li.get("entity_id") for li in lines or []]) for eid, lines in rows]


async def _events(engine, company_id, entity_type: str) -> list:
    async with engine.connect() as conn:
        return list((await conn.execute(text(
            "SELECT event_type FROM ledger WHERE company_id = :c AND entity_type = :t ORDER BY id"),
            {"c": company_id, "t": entity_type})).scalars().all())


def _refused(outcome, entity_id: str = _ITEM, line: int = 1) -> None:
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 422
    assert outcome.detail["code"] == "invalid_reference"
    assert outcome.detail["item_id"] == entity_id
    assert outcome.detail["line"] == line
    assert f"Line {line} " in outcome.detail["message"]


async def test_a_document_form_opened_before_undo_is_refused_and_writes_nothing(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, batch_id = await _seed(factory)
    await _undone(factory, company_id, user, batch_id)

    async with factory() as s:
        outcome = (await asyncio.gather(_create(s, company_id, user, _KEPT, _ITEM), return_exceptions=True))[0]
        await s.commit()

    _refused(outcome, line=2)
    assert await _records(committed_engine, company_id, "doc") == []
    assert await _events(committed_engine, company_id, "doc") == []


async def test_a_document_created_first_makes_undo_refuse(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, batch_id = await _seed(factory)

    async with factory() as creator, factory() as undoer:
        commit = creator.commit
        creator.commit = _deferred
        created = await _create(creator, company_id, user, _ITEM)
        undo = asyncio.create_task(_undo(undoer, company_id, user, batch_id))
        await _until_blocked(committed_engine, undo)
        await commit()
        outcome = (await asyncio.gather(asyncio.wait_for(undo, timeout=30), return_exceptions=True))[0]

    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 409
    assert outcome.detail["code"] == "import_items_modified"
    assert await _records(committed_engine, company_id, "doc") == [(created["id"], [_ITEM])]
    assert [eid for eid, _ in await _records(committed_engine, company_id, "item")] == [_KEPT, _ITEM]


async def test_a_document_created_while_undo_removes_the_item_is_refused(committed_engine, monkeypatch):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, batch_id = await _seed(factory)

    locked, release = asyncio.Event(), asyncio.Event()
    real_check = inventory.mentioned_elsewhere

    async def _paused_check(*args, **kwargs):
        # Undo holds the company and the item and has found no reference yet.
        locked.set()
        await release.wait()
        return await real_check(*args, **kwargs)

    monkeypatch.setattr(inventory, "mentioned_elsewhere", _paused_check)

    async with factory() as undoer, factory() as creator:
        undo = asyncio.create_task(_undo(undoer, company_id, user, batch_id))
        await asyncio.wait_for(locked.wait(), timeout=30)
        # The form's checks run now, while the item is still there.
        create = asyncio.create_task(_create(creator, company_id, user, _ITEM))
        await _until_blocked(committed_engine, create)
        release.set()
        undone = await asyncio.wait_for(undo, timeout=30)
        outcome = (await asyncio.gather(asyncio.wait_for(create, timeout=30), return_exceptions=True))[0]
        await creator.commit()

    assert undone == {"ok": True, "removed": 1}
    _refused(outcome)
    assert await _records(committed_engine, company_id, "doc") == []
    assert await _events(committed_engine, company_id, "doc") == []


async def test_an_edit_adding_an_undone_item_is_refused(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, batch_id = await _seed(factory)
    async with factory() as s:
        created = await _create(s, company_id, user, _KEPT)
    await _undone(factory, company_id, user, batch_id)

    async with factory() as s:
        outcome = (await asyncio.gather(
            _patch(s, company_id, user, created["id"], [_line(_KEPT)], [_line(_KEPT), _line(_ITEM)]),
            return_exceptions=True))[0]

    _refused(outcome, line=2)
    assert await _records(committed_engine, company_id, "doc") == [(created["id"], [_KEPT])]
    assert await _events(committed_engine, company_id, "doc") == ["doc.created"]


async def test_an_edit_keeps_a_line_whose_item_was_already_gone(committed_engine):
    """A line already on the document is carried forward even when its item has since
    gone (data from before this rule): the document stays editable, and only a line the
    save adds must exist."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, _batch = await _seed(factory)
    async with factory() as s:
        created = await _create(s, company_id, user, _ITEM)
    async with factory() as s:
        await erase_items(s, company_id, [_ITEM])
        await s.commit()

    old = [_line(_ITEM)]
    new = [{**_line(_ITEM), "description": "kept as sold"}, _line(_KEPT)]
    async with factory() as s:
        await _patch(s, company_id, user, created["id"], old, new)

    assert await _records(committed_engine, company_id, "doc") == [(created["id"], [_ITEM, _KEPT])]


def _import_record(company_id, *entity_ids, kind: str = "doc") -> dict:
    ref = f"IMP-{uuid.uuid4().hex[:6]}"
    data = {"ref_id": ref, "status": "draft", "line_items": [_line(e) for e in entity_ids],
            "subtotal": 10, "total": 10}
    if kind == "doc":
        data["doc_type"] = "invoice"
    else:
        data["list_type"] = "quotation"
    return {"entity_id": f"{kind}:{ref}", "event_type": f"{kind}.created", "data": data,
            "source": "import", "idempotency_key": f"imp:{uuid.uuid4().hex}"}


@pytest.mark.parametrize("missing", [_ITEM, "item:never-existed"])
async def test_a_document_import_with_a_removed_or_unknown_item_is_refused(committed_engine, missing):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, batch_id = await _seed(factory)
    await _undone(factory, company_id, user, batch_id)

    async with factory() as s:
        one = docs.DocImportRecord(**_import_record(company_id, missing))
        outcome = (await asyncio.gather(
            docs.import_doc(one, company_id=company_id, _=None, __=None, user=user, session=s),
            return_exceptions=True))[0]
        await s.commit()
    _refused(outcome, missing)

    good, bad = _import_record(company_id, _KEPT), _import_record(company_id, _KEPT, missing)
    async with factory() as s:
        result = await docs.batch_import_docs(
            docs.DocBatchImportRequest(records=[good, bad]),
            company_id=company_id, _=None, __=None, user=user, session=s)

    assert result.created == 1
    assert len(result.errors) == 1 and missing in result.errors[0] and "Line 2" in result.errors[0]
    assert await _records(committed_engine, company_id, "doc") == [(good["entity_id"], [_KEPT])]


async def test_a_list_import_with_an_unknown_item_is_refused(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, batch_id = await _seed(factory)
    await _undone(factory, company_id, user, batch_id)

    async with factory() as s:
        record = docs.ListImportRecord(**_import_record(company_id, _ITEM, kind="list"))
        outcome = (await asyncio.gather(
            docs.import_list(record, company_id=company_id, _=None, __=None, user=user, session=s),
            return_exceptions=True))[0]
        await s.commit()

    _refused(outcome)
    assert await _records(committed_engine, company_id, "list") == []


async def test_a_line_writer_that_skipped_the_company_lock_still_waits_for_undo(committed_engine):
    """The check takes the company lock itself, so a writer that did not take it first
    still cannot add a line while a removal holds the lock."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, _batch = await _seed(factory)

    async with factory() as holder, factory() as writer:
        await lock_company(holder, company_id)
        write = asyncio.create_task(emit_event(
            writer, company_id=company_id, entity_id="doc:BARE", entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "ref_id": "BARE", "status": "draft", "line_items": [_line(_ITEM)]},
            actor_id=user.id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        ))
        await _until_blocked(committed_engine, write)
        await erase_items(holder, company_id, [_ITEM])
        await holder.commit()
        outcome = (await asyncio.gather(asyncio.wait_for(write, timeout=30), return_exceptions=True))[0]
        await writer.commit()

    _refused(outcome)
    assert await _records(committed_engine, company_id, "doc") == []


def _create_list(s, company_id, user, *entity_ids):
    payload = docs.ListCreatePayload(list_type="quotation", line_items=[_line(e) for e in entity_ids])
    return docs.create_list(payload, company_id=company_id, _=None, role="admin", settings={},
                            user=user, session=s)


def _patch_list(s, company_id, user, entity_id, version, old_lines, new_lines):
    changed = {"line_items": {"old": old_lines, "new": new_lines}}
    return docs.patch_list(entity_id, docs.ListPatch(fields_changed=changed, expected_version=version),
                           company_id=company_id, _=None, role="admin", settings={}, user=user, session=s)


async def _with_a_line_already_gone(factory, engine, company_id, user, kind: str) -> tuple[str, int]:
    """A stored document or List with one line whose item has since been erased."""
    async with factory() as s:
        created = await (_create(s, company_id, user, _ITEM) if kind == "doc"
                         else _create_list(s, company_id, user, _ITEM))
    async with factory() as s:
        await erase_items(s, company_id, [_ITEM])
        await s.commit()
    async with engine.connect() as conn:
        version = (await conn.execute(text(
            "SELECT version FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": created["id"]})).scalar_one()
    return created["id"], version


def _save(s, company_id, user, kind, entity_id, version, old, new):
    if kind == "doc":
        return _patch(s, company_id, user, entity_id, old, new)
    if kind == "list-page":
        page = docs.ListLinePagePatch(line_items=new, offset=0, original_count=len(old), expected_version=version)
        return docs.patch_list_line_page(entity_id, page, company_id=company_id, _=None, role="admin",
                                         settings={}, user=user, session=s)
    return _patch_list(s, company_id, user, entity_id, version, old, new)


# A List line page save replaces the window it loaded, so it is checked like a full save.
_SAVES = [("doc", "doc"), ("list", "list"), ("list-page", "list")]


@pytest.mark.parametrize("save, kind", _SAVES)
async def test_a_save_cannot_repeat_a_line_whose_item_was_already_gone(committed_engine, save, kind):
    """The stored record holds one line for a gone item; a save may carry that one line
    forward, but a second line for the same gone item is a new reference and is refused."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, _batch = await _seed(factory)
    entity_id, version = await _with_a_line_already_gone(factory, committed_engine, company_id, user, kind)

    async with factory() as s:
        outcome = (await asyncio.gather(
            _save(s, company_id, user, save, entity_id, version, [_line(_ITEM)], [_line(_ITEM), _line(_ITEM)]),
            return_exceptions=True))[0]
        await s.rollback()

    _refused(outcome, line=2)
    assert await _records(committed_engine, company_id, kind) == [(entity_id, [_ITEM])]
    assert await _events(committed_engine, company_id, kind) == [f"{kind}.created"]


@pytest.mark.parametrize("save, kind", _SAVES)
async def test_a_save_may_edit_the_one_line_whose_item_was_already_gone(committed_engine, save, kind):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user, _batch = await _seed(factory)
    entity_id, version = await _with_a_line_already_gone(factory, committed_engine, company_id, user, kind)

    async with factory() as s:
        await _save(s, company_id, user, save, entity_id, version,
                    [_line(_ITEM)], [{**_line(_ITEM), "description": "kept as sold"}])
        await s.commit()

    assert await _records(committed_engine, company_id, kind) == [(entity_id, [_ITEM])]
    assert await _events(committed_engine, company_id, kind) == [f"{kind}.created", f"{kind}.updated"]
