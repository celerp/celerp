# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Erasing sample items never changes a locked period.

When a sample item's history sits on or before the lock date, the erase is refused with
erase.locked_period and the message says to unlock the period, erase, then lock it again;
nothing is removed or posted. Outside a locked period only records proven to be sample
fixtures by their seeding tag are removed; an item with any other record in scope is
refused with demo.not_sample_fixture. The automatic clear-out on a first import or a
business-type change keeps samples whose history is locked instead of failing the user's
action. The same lock rule covers every other erase (a draft deleted by mistake).
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

LOCKED = "erase.locked_period"
HISTORY_TS = "2026-01-15 10:00:00+00"


async def _owner(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "SampleCo", "email": f"s14-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/companies/me/business-type", json={"vertical": "agricultural"}, headers=h)
    assert r.status_code == 200, r.text
    me = await client.get("/companies/me", headers=h)
    return h, uuid.UUID(me.json()["id"])


async def _samples(session, company_id) -> list[str]:
    from celerp.services.demo import demo_item_ids
    session.expire_all()
    return sorted(await demo_item_ids(session, company_id))


async def _backdate(session, company_id, entity_ids) -> None:
    """The samples were seeded months ago, before the period now being locked."""
    await session.execute(sa.update(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(entity_ids),
    ).values(ts=sa.text(f"'{HISTORY_TS}'::timestamptz")))
    await session.commit()


async def _lock(client, h, day: str) -> None:
    r = await client.post("/accounting/period-lock", headers=h, json={"lock_date": day})
    assert r.status_code == 200, r.text


async def _rows(session, company_id) -> int:
    session.expire_all()
    return (await session.execute(sa.select(sa.func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id))).scalar_one()


async def _items(session, company_id, ids) -> int:
    session.expire_all()
    return (await session.execute(sa.select(sa.func.count()).select_from(Projection).where(
        Projection.company_id == company_id, Projection.entity_id.in_(ids)))).scalar_one()


def _keyed(r, status: int, key: str) -> dict:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == key, detail
    return detail


async def test_sample_erase_with_history_in_a_locked_period_is_refused(client, session):
    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    assert len(ids) == 5
    await _backdate(session, cid, ids)
    await _lock(client, h, "2026-06-30")
    rows = await _rows(session, cid)
    r = await client.post("/items/bulk/delete", headers=h, json={"entity_ids": ids, "untouched_samples_only": True})
    detail = _keyed(r, 409, LOCKED)
    assert "2026-06-30" in detail["message"]
    assert "unlock" in detail["message"].lower() and "lock" in detail["message"].lower()
    assert await _rows(session, cid) == rows, "nothing removed and nothing posted"
    assert await _items(session, cid, ids) == 5


async def test_sample_erase_after_the_locked_period_still_removes_the_samples(client, session):
    """Neighbour: a lock that ends before the samples' history does not stop the erase."""
    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    await _backdate(session, cid, ids)
    await _lock(client, h, "2025-12-31")
    r = await client.post("/items/bulk/delete", headers=h, json={"entity_ids": ids, "untouched_samples_only": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 5, "kept": 0}
    assert await _items(session, cid, ids) == 0


async def test_sample_erase_with_no_lock_still_removes_the_samples(client, session):
    """Neighbour: with no lock date the erase works as before."""
    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    r = await client.post("/items/bulk/delete", headers=h, json={"entity_ids": ids, "untouched_samples_only": True})
    assert r.status_code == 200, r.text
    assert await _items(session, cid, ids) == 0


async def test_sample_erase_refuses_an_item_with_a_record_that_is_not_a_sample_fixture(client, session):
    """The canonical sample eraser removes only seeding-tagged records. An item carrying
    anything else (here an edit by the owner) is refused and nothing is removed."""
    from celerp.services.demo import delete_demo_items

    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    r = await client.patch(f"/items/{ids[0]}", headers=h, json={
        "fields_changed": {"quantity": {"old": None, "new": 42}}})
    assert r.status_code == 200, r.text
    rows = await _rows(session, cid)
    with pytest.raises(HTTPException) as exc:
        await delete_demo_items(session, cid, ids)
    assert exc.value.status_code == 409
    assert exc.value.detail["message_key"] == "demo.not_sample_fixture"
    await session.rollback()
    assert await _rows(session, cid) == rows
    assert await _items(session, cid, ids) == 5


async def test_first_import_keeps_samples_whose_history_is_locked(client, session):
    """The automatic clear-out on a first item import keeps locked samples and the import
    still lands."""
    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    await _backdate(session, cid, ids)
    await _lock(client, h, "2026-06-30")
    rows_before = (await _rows(session, cid))
    r = await client.post("/items/import/batch", headers=h, json={"records": [
        {"entity_id": "item:s14-imp", "event_type": "item.created", "source": "csv",
         "idempotency_key": "s14-imp", "data": {"sku": "S14-IMP", "name": "Imported", "quantity": 1,
                                                "sell_by": "piece"}}]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.json()
    assert await _items(session, cid, ids) == 5
    assert await _rows(session, cid) > rows_before


async def test_business_type_change_keeps_samples_whose_history_is_locked(client, session):
    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    await _backdate(session, cid, ids)
    await _lock(client, h, "2026-06-30")
    r = await client.post("/companies/me/business-type", json={"vertical": "electronics"}, headers=h)
    assert r.status_code == 200, r.text
    assert await _items(session, cid, ids) == 5


async def test_reseed_keeps_samples_whose_history_is_locked(client, session):
    h, cid = await _owner(client)
    ids = await _samples(session, cid)
    await _backdate(session, cid, ids)
    await _lock(client, h, "2026-06-30")
    r = await client.post("/companies/me/demo/reseed", headers=h, json={})
    assert r.status_code == 200, r.text
    assert await _items(session, cid, ids) == 5


async def test_draft_erase_with_history_in_a_locked_period_is_refused(client, session):
    """Every erase passes the same rule: a draft deleted by mistake whose history is
    locked is refused too, and stays."""
    h, cid = await _owner(client)
    r = await client.post("/items", headers=h, json={
        "sku": "DRAFT-1", "name": "Draft", "sell_by": "piece", "status": "draft"})
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    await _backdate(session, cid, [item])
    await _lock(client, h, "2026-06-30")
    r = await client.post("/items/bulk/delete", headers=h, json={"entity_ids": [item]})
    _keyed(r, 409, LOCKED)
    assert await _items(session, cid, [item]) == 1
