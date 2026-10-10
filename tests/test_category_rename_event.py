# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A category rename is an event on every item it moves.

Renaming a category moves each item in it through its own item.updated event, so the
ledger holds the rename and a rebuild of the projections keeps it. Items in other
categories are untouched. Every other direct writer of a projection is listed, with
why a rebuild keeps what it writes, in tests/test_set_aside_older_paths.py.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


async def _owner(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "Rename Co", "email": f"ren-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/companies/me", headers=h)).json()
    return h, uuid.UUID(me["id"])


async def _item(client, h, sku: str, category: str) -> str:
    r = await client.post("/items", headers=h, json={
        "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece", "category": category})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _category_of(session, cid, eid) -> str | None:
    session.expire_all()
    row = await session.get(Projection, (cid, eid))
    return (row.state or {}).get("category")


async def _rebuild(session, cid) -> None:
    from celerp.projections.engine import ProjectionEngine

    await ProjectionEngine.rebuild(session, cid)
    await session.commit()


async def _setup(client) -> tuple[dict, uuid.UUID, list[str], str]:
    h, cid = await _owner(client)
    for name in ("Old Gems", "Other"):
        r = await client.post("/companies/me/categories", headers=h, json={"name": name})
        assert r.status_code == 200, r.text
    moved = [await _item(client, h, f"REN-{i}", "old_gems") for i in range(2)]
    other = await _item(client, h, "REN-OTHER", "other")
    return h, cid, moved, other


async def test_a_category_rename_survives_a_rebuild(client, session):
    """Red statement: the rename wrote the item projections directly and left no event, so
    a rebuild put every item back in the old category, which no longer exists."""
    h, cid, moved, other = await _setup(client)
    r = await client.patch("/companies/me/categories/old_gems", headers=h, json={"name": "New Gems"})
    assert r.status_code == 200, r.text
    assert r.json()["items_updated"] == 2
    await _rebuild(session, cid)
    assert [await _category_of(session, cid, eid) for eid in moved] == ["new_gems", "new_gems"]
    assert await _category_of(session, cid, other) == "other"


async def test_a_category_rename_is_one_event_per_item_it_moves(client, session):
    h, cid, moved, other = await _setup(client)
    r = await client.patch("/companies/me/categories/old_gems", headers=h, json={"name": "New Gems"})
    assert r.status_code == 200, r.text
    rows = (await session.execute(sa.select(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.source == "category_rename"))).scalars().all()
    assert sorted(e.entity_id for e in rows) == sorted(moved)
    for e in rows:
        assert e.event_type == "item.updated"
        assert e.data["fields_changed"]["category"] == {"old": "old_gems", "new": "new_gems"}
        assert e.actor_id is not None


async def test_renaming_a_category_back_restores_it_after_a_rebuild(client, session):
    h, cid, moved, _ = await _setup(client)
    for frm, to in (("old_gems", "New Gems"), ("new_gems", "Old Gems")):
        r = await client.patch(f"/companies/me/categories/{frm}", headers=h, json={"name": to})
        assert r.status_code == 200, r.text
    await _rebuild(session, cid)
    assert [await _category_of(session, cid, eid) for eid in moved] == ["old_gems", "old_gems"]


async def test_a_rename_to_the_same_name_writes_no_event(client, session):
    """Neighbour: nothing moves, so nothing is recorded."""
    h, cid, _, _ = await _setup(client)
    r = await client.patch("/companies/me/categories/old_gems", headers=h, json={"name": "Old Gems"})
    assert r.status_code == 200, r.text
    assert r.json()["items_updated"] == 0
    count = await session.scalar(sa.select(sa.func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.source == "category_rename"))
    assert count == 0


async def test_a_refused_rename_moves_no_item(client, session):
    """Neighbour: a rename onto an existing category is refused and nothing moves."""
    h, cid, moved, other = await _setup(client)
    r = await client.patch("/companies/me/categories/old_gems", headers=h, json={"name": "Other"})
    assert r.status_code == 409, r.text
    assert [await _category_of(session, cid, eid) for eid in moved] == ["old_gems", "old_gems"]
    count = await session.scalar(sa.select(sa.func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.source == "category_rename"))
    assert count == 0
