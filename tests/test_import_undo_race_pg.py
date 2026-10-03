# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Undoing an import never removes an item someone changed, whichever comes first.

Two orders, each on real PostgreSQL connections with a fixed interleaving:

1. An edit holds the item and has written its change but not committed; Undo starts,
   waits for the item, and must then see the edit and refuse, leaving the item.
2. Undo holds the item and is about to remove it; an edit starts and waits for the
   item. Undo removes it; the edit must then fail and must not bring the item back
   or leave a stray event behind.
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
from celerp_inventory import routes as inventory
from celerp_inventory.models_import_batch import ImportBatch

pytestmark = pytest.mark.asyncio

_ITEM = "item:undo-raced"


async def _seed(factory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A company with one imported item and the reversible Import History entry for it."""
    company_id, user_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"import:{uuid.uuid4().hex}"
    async with factory() as s:
        s.add(Company(id=company_id, name="UndoRace", slug=f"undo-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await emit_event(
            s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.created",
            data={"sku": "RACED", "name": "Raced", "quantity": 1, "sell_by": "piece"},
            actor_id=user_id, location_id=None, source="csv_import", idempotency_key=key,
        )
        s.add(ImportBatch(id=batch_id, company_id=company_id, entity_type="item", filename="raced.csv",
                          row_count=1, entity_ids=[_ITEM], idempotency_keys=[key], reversible=True))
        await s.commit()
    return company_id, user_id, batch_id


def _undo(s, company_id, user_id, batch_id):
    return inventory.undo_import_batch(str(batch_id), company_id=company_id,
                                       user=types.SimpleNamespace(id=user_id), session=s)


def _edit(s, company_id, user_id):
    return inventory.patch_item(
        _ITEM, inventory.ItemPatch(fields_changed={"name": {"old": "Raced", "new": "Renamed"}}),
        company_id=company_id, user=types.SimpleNamespace(id=user_id), role="admin", settings={}, session=s)


async def _deferred() -> None:
    """Stands in for a commit the test makes itself, later."""


async def _until_blocked(engine, task: asyncio.Task) -> None:
    """Wait until ``task`` waits on a row lock (each poll is its own snapshot)."""
    for _ in range(400):
        assert not task.done(), f"the call did not wait for the item: {task.exception()!r}"
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the call never waited for the item")


async def _stored(engine, company_id) -> tuple:
    async with engine.connect() as conn:
        name = (await conn.execute(text(
            "SELECT state::jsonb ->> 'name' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one_or_none()
        events = (await conn.execute(text(
            "SELECT event_type FROM ledger WHERE company_id = :c AND entity_id = :e ORDER BY id"),
            {"c": company_id, "e": _ITEM})).scalars().all()
        status = (await conn.execute(text(
            "SELECT status FROM import_batches WHERE company_id = :c"), {"c": company_id})).scalar_one()
    return name, list(events), status


async def test_an_edit_that_holds_the_item_first_makes_undo_refuse(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, batch_id = await _seed(factory)

    async with factory() as editor, factory() as s:
        # The edit route commits as its last step; hold that commit back so the edit
        # is written and holds the item while Undo starts.
        commit = editor.commit
        editor.commit = _deferred
        await _edit(editor, company_id, user_id)
        task = asyncio.create_task(_undo(s, company_id, user_id, batch_id))
        await _until_blocked(committed_engine, task)
        await commit()
        outcome = (await asyncio.gather(asyncio.wait_for(task, timeout=30), return_exceptions=True))[0]

    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 409
    assert outcome.detail["code"] == "import_items_modified"
    assert outcome.detail["entity_ids"] == [_ITEM]
    assert await _stored(committed_engine, company_id) == ("Renamed", ["item.created", "item.updated"], "active")


async def test_an_edit_waiting_on_an_undo_that_removes_the_item_does_not_bring_it_back(committed_engine, monkeypatch):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, batch_id = await _seed(factory)

    locked, release = asyncio.Event(), asyncio.Event()
    real_check = inventory.mentioned_elsewhere

    async def _paused_check(*args, **kwargs):
        # Undo has the company and the item locked and has read the item's history.
        locked.set()
        await release.wait()
        return await real_check(*args, **kwargs)

    monkeypatch.setattr(inventory, "mentioned_elsewhere", _paused_check)

    async with factory() as undoer, factory() as editor:
        undo = asyncio.create_task(_undo(undoer, company_id, user_id, batch_id))
        await asyncio.wait_for(locked.wait(), timeout=30)
        edit = asyncio.create_task(_edit(editor, company_id, user_id))
        await _until_blocked(committed_engine, edit)
        release.set()
        undone = await asyncio.wait_for(undo, timeout=30)
        outcome = (await asyncio.gather(asyncio.wait_for(edit, timeout=30), return_exceptions=True))[0]
        if not isinstance(outcome, BaseException):
            await editor.commit()

    assert undone == {"ok": True, "removed": 1}
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 404
    assert await _stored(committed_engine, company_id) == (None, [], "undone")
