# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Deleting a draft document permanently is judged on the document as it stands once
the delete holds it, never on a read taken before.

Each race runs on real PostgreSQL across two connections, in both orders, through the
full HTTP stack. The first writer holds its commit until the second has started and is
waiting on (or past) the lock:

- finalize first: the delete wakes to a finalized document and deletes nothing; the
  document, its history and its journal entry all stay;
- delete first: finalize wakes to a document that is gone and posts nothing;
- permission removed first: the waiting delete is refused and changes nothing.

The bulk delete is raced the same way with a mixed batch: the raced draft, a second
draft and an item ID.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_fresh_authority_races_pg import _ITEM, _app_client, _direct, _http, _ok, _race, _refused, _revoke
from test_fresh_authority_races_pg import _seed as _seed_company
from test_helpers import provision_company_books

pytestmark = pytest.mark.asyncio

_LINE = {"description": "Service", "quantity": 1, "unit_price": 100}


async def _draft(client, c) -> str:
    resp = await client.post("/docs", json={"doc_type": "invoice", "line_items": [_LINE]}, headers=c["h"]["owner"])
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _record(factory, company_id, doc_id) -> tuple[str | None, int, list[str]]:
    """(status or None when gone, ledger events for the document, its automatic journal entries)."""
    async with factory() as s:
        row = await s.get(Projection, {"company_id": company_id, "entity_id": doc_id})
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == doc_id))).scalar_one()
        entries = (await s.execute(select(Projection.entity_id).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.entity_id.like(f"je:auto:{doc_id}:%")))).scalars().all()
    return (row.state.get("status") if row else None), events, sorted(entries)


def _finalize(client, c, doc_id):
    return lambda: client.post(f"/docs/{doc_id}/finalize", headers=c["h"]["owner"])


def _delete_one(client, c, doc_id):
    return lambda: client.delete(f"/docs/{doc_id}", headers=c["h"]["manager"])


def _delete_bulk(client, c, ids):
    return lambda: client.delete("/docs/bulk-draft", params={"doc_ids": ",".join(ids)}, headers=c["h"]["manager"])


async def _seed(factory) -> dict:
    """A company whose books take the entries a finalized document posts."""
    c = await _seed_company(factory)
    async with factory() as s:
        await provision_company_books(s, c["company_id"])
        await s.commit()
    return c


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


# -- DELETE /docs/{entity_id} ---------------------------------------------------------

async def test_finalize_first_leaves_the_waiting_delete_nothing_to_delete(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        doc_id = await _draft(client, c)
        finalized, deleted = await _race(
            committed_engine,
            lambda held: _http(pending, held, _finalize(client, c, doc_id)),
            lambda held: _http(pending, held, _delete_one(client, c, doc_id)),
        )
    _ok(finalized)
    _refused(deleted, 409)
    status, events, entries = await _record(factory, c["company_id"], doc_id)
    assert status == "final" and events == 2 and entries


async def test_delete_first_leaves_the_waiting_finalize_nothing_to_post(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        doc_id = await _draft(client, c)
        deleted, finalized = await _race(
            committed_engine,
            lambda held: _http(pending, held, _delete_one(client, c, doc_id)),
            lambda held: _http(pending, held, _finalize(client, c, doc_id)),
        )
    _ok(deleted)
    _refused(finalized, 404)
    assert await _record(factory, c["company_id"], doc_id) == (None, 0, [])


async def test_permission_removed_first_refuses_the_waiting_delete(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        doc_id = await _draft(client, c)
        before = await _record(factory, c["company_id"], doc_id)
        revoked, deleted = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "delete_documents", "manager")),
            lambda held: _http(pending, held, _delete_one(client, c, doc_id)),
        )
    _ok(revoked)
    _refused(deleted, 403)
    assert await _record(factory, c["company_id"], doc_id) == before == ("draft", 1, [])


# -- DELETE /docs/bulk-draft (mixed batch: raced draft, other draft, item ID) ----------

async def test_finalize_first_keeps_its_document_out_of_a_waiting_bulk_delete(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        raced, other = await _draft(client, c), await _draft(client, c)
        finalized, deleted = await _race(
            committed_engine,
            lambda held: _http(pending, held, _finalize(client, c, raced)),
            lambda held: _http(pending, held, _delete_bulk(client, c, [raced, other, _ITEM])),
        )
    _ok(finalized)
    _ok(deleted)
    assert deleted.json() == {"deleted": [other], "count": 1}
    status, events, entries = await _record(factory, c["company_id"], raced)
    assert status == "final" and events == 2 and entries
    assert await _record(factory, c["company_id"], other) == (None, 0, [])
    assert (await _record(factory, c["company_id"], _ITEM))[0] == "available"


async def test_bulk_delete_first_leaves_the_waiting_finalize_nothing_to_post(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        raced, other = await _draft(client, c), await _draft(client, c)
        deleted, finalized = await _race(
            committed_engine,
            lambda held: _http(pending, held, _delete_bulk(client, c, [raced, other, _ITEM])),
            lambda held: _http(pending, held, _finalize(client, c, raced)),
        )
    _ok(deleted)
    assert sorted(deleted.json()["deleted"]) == sorted([raced, other])
    _refused(finalized, 404)
    assert await _record(factory, c["company_id"], raced) == (None, 0, [])
    assert await _record(factory, c["company_id"], other) == (None, 0, [])
    assert (await _record(factory, c["company_id"], _ITEM))[0] == "available"


async def test_permission_removed_first_refuses_the_waiting_bulk_delete(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        raced, other = await _draft(client, c), await _draft(client, c)
        revoked, deleted = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "delete_documents", "manager")),
            lambda held: _http(pending, held, _delete_bulk(client, c, [raced, other, _ITEM])),
        )
    _ok(revoked)
    _refused(deleted, 403)
    for doc_id in (raced, other):
        assert await _record(factory, c["company_id"], doc_id) == ("draft", 1, [])
