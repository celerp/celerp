# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Clearing unused sample items never removes one someone is editing, whichever comes first.

A company's first import clears the sample items nobody edited or used. Two orders,
each on real PostgreSQL connections with a fixed interleaving:

1. An edit has written its change to a sample item but not committed; the clean-up
   starts, waits for the item, and must then see the edit and keep the item.
2. The clean-up has checked the items and is about to remove them; an edit starts and
   waits for the item. The clean-up removes it; the edit must then fail and must not
   bring the item back or leave a stray event behind.
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
from celerp.services import demo
from celerp.services.company_lock import locked_company
from celerp_inventory import routes as inventory

pytestmark = pytest.mark.asyncio

_EDITED = "item:demo-edited"
_SPARE = "item:demo-spare"


async def _seed(factory) -> tuple[uuid.UUID, uuid.UUID]:
    """A company with two sample items, as the demo seeder writes them."""
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="DemoRace", slug=f"demo-race-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        for entity_id in (_EDITED, _SPARE):
            await emit_event(
                s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.created",
                data={"sku": entity_id.split(":")[1].upper(), "name": "Sample", "quantity": 1, "sell_by": "piece"},
                actor_id=user_id, location_id=None, source="demo", idempotency_key=str(uuid.uuid4()),
            )
        await s.commit()
    return company_id, user_id


async def _clear(s, company_id) -> tuple[int, int]:
    """The import's clean-up, under the company lock its callers hold."""
    await locked_company(s, company_id)
    return await demo.delete_untouched_demo_items(s, company_id)


def _edit(s, company_id, user_id):
    return inventory.patch_item(
        _EDITED, inventory.ItemPatch(fields_changed={"name": {"old": "Sample", "new": "Mine"}}),
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


async def _stored(engine, company_id, entity_id) -> tuple:
    async with engine.connect() as conn:
        name = (await conn.execute(text(
            "SELECT state::jsonb ->> 'name' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": entity_id})).scalar_one_or_none()
        events = (await conn.execute(text(
            "SELECT event_type FROM ledger WHERE company_id = :c AND entity_id = :e ORDER BY id"),
            {"c": company_id, "e": entity_id})).scalars().all()
    return name, list(events)


async def test_an_edit_that_holds_the_item_first_keeps_it_through_the_clean_up(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed(factory)

    async with factory() as editor, factory() as cleaner:
        commit = editor.commit
        editor.commit = _deferred
        await _edit(editor, company_id, user_id)
        clear = asyncio.create_task(_clear(cleaner, company_id))
        await _until_blocked(committed_engine, clear)
        await commit()
        cleared = await asyncio.wait_for(clear, timeout=30)
        await cleaner.commit()

    assert cleared == (1, 1)
    assert await _stored(committed_engine, company_id, _EDITED) == ("Mine", ["item.created", "item.updated"])
    assert await _stored(committed_engine, company_id, _SPARE) == (None, [])


async def test_an_edit_waiting_on_the_clean_up_does_not_bring_the_item_back(committed_engine, monkeypatch):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed(factory)

    checked, release = asyncio.Event(), asyncio.Event()
    real_check = demo._untouched_demo_items

    async def _paused_check(*args, **kwargs):
        # The clean-up has found the items untouched and is about to remove them.
        removable = await real_check(*args, **kwargs)
        checked.set()
        await release.wait()
        return removable

    monkeypatch.setattr(demo, "_untouched_demo_items", _paused_check)

    async with factory() as cleaner, factory() as editor:
        clear = asyncio.create_task(_clear(cleaner, company_id))
        await asyncio.wait_for(checked.wait(), timeout=30)
        edit = asyncio.create_task(_edit(editor, company_id, user_id))
        await _until_blocked(committed_engine, edit)
        release.set()
        cleared = await asyncio.wait_for(clear, timeout=30)
        await cleaner.commit()
        outcome = (await asyncio.gather(asyncio.wait_for(edit, timeout=30), return_exceptions=True))[0]
        await editor.commit()

    assert cleared == (2, 0)
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 404
    assert await _stored(committed_engine, company_id, _EDITED) == (None, [])
    assert await _stored(committed_engine, company_id, _SPARE) == (None, [])
