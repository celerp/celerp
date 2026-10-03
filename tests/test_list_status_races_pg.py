# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A list's status change is judged on the list as it stands once the change holds it,
never on a read taken before.

Each race runs on real PostgreSQL across two connections, through the full HTTP stack.
The first writer holds its commit until the second has started and is waiting on (or
past) the lock:

- finalize first: the waiting delete finds a finalized list and deletes nothing;
- delete first: finalize wakes to a list that is gone;
- permission removed first: the waiting delete is refused and changes nothing;
- convert first: a waiting revert to draft or void finds the list closed and changes
  nothing, so the list cannot be converted a second time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_fresh_authority_races_pg import _app_client, _direct, _http, _ok, _race, _refused, _revoke, _seed

pytestmark = pytest.mark.asyncio

_LINE = {"name": "Service", "quantity": 1, "unit_price": 100, "line_total": 100}


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _list(client, c, *, finalized: bool = False) -> str:
    resp = await client.post("/lists", json={"list_type": "quotation", "line_items": [_LINE]}, headers=c["h"]["owner"])
    assert resp.status_code == 200, resp.text
    list_id = resp.json()["id"]
    if finalized:
        _ok(await client.post(f"/lists/{list_id}/finalize", headers=c["h"]["owner"]))
    return list_id


async def _record(factory, company_id, list_id) -> tuple[str | None, int]:
    """(status or None when gone, ledger events for the list)."""
    async with factory() as s:
        row = await s.get(Projection, {"company_id": company_id, "entity_id": list_id})
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == list_id))).scalar_one()
    return (row.state.get("status") if row else None), events


async def _converted_docs(factory, company_id, list_id) -> int:
    async with factory() as s:
        rows = (await s.execute(select(Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    return sum(1 for state in rows if state.get("source_list_id") == list_id)


def _post(client, c, role, path, json=None):
    return lambda: client.post(path, json=json, headers=c["h"][role])


def _delete(client, c, list_id):
    return lambda: client.delete(f"/lists/{list_id}", headers=c["h"]["manager"])


# -- DELETE /lists/{entity_id} --------------------------------------------------------

async def test_finalize_first_leaves_the_waiting_list_delete_nothing_to_delete(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        list_id = await _list(client, c)
        finalized, deleted = await _race(
            committed_engine,
            lambda held: _http(pending, held, _post(client, c, "owner", f"/lists/{list_id}/finalize")),
            lambda held: _http(pending, held, _delete(client, c, list_id)),
        )
    _ok(finalized)
    _refused(deleted, 409)
    assert await _record(factory, c["company_id"], list_id) == ("finalized", 2)


async def test_list_delete_first_leaves_the_waiting_finalize_nothing_to_finalize(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        list_id = await _list(client, c)
        deleted, finalized = await _race(
            committed_engine,
            lambda held: _http(pending, held, _delete(client, c, list_id)),
            lambda held: _http(pending, held, _post(client, c, "owner", f"/lists/{list_id}/finalize")),
        )
    _ok(deleted)
    _refused(finalized, 404)
    assert await _record(factory, c["company_id"], list_id) == (None, 0)


async def test_permission_removed_first_refuses_the_waiting_list_delete(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        list_id = await _list(client, c)
        revoked, deleted = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "delete_documents", "manager")),
            lambda held: _http(pending, held, _delete(client, c, list_id)),
        )
    _ok(revoked)
    _refused(deleted, 403)
    assert await _record(factory, c["company_id"], list_id) == ("draft", 1)


# -- convert racing revert to draft and void ------------------------------------------

@pytest.mark.parametrize("action", ["revert-to-draft", "void"])
async def test_convert_first_leaves_the_waiting_status_change_a_closed_list(committed_engine, action):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        list_id = await _list(client, c, finalized=True)
        before = await _record(factory, c["company_id"], list_id)
        converted, changed = await _race(
            committed_engine,
            lambda held: _http(pending, held, _post(client, c, "owner", f"/lists/{list_id}/convert",
                                                    {"target_type": "invoice"})),
            lambda held: _http(pending, held, _post(client, c, "owner", f"/lists/{list_id}/{action}", {})),
        )
        _ok(converted)
        _refused(changed, 409)
        status, events = await _record(factory, c["company_id"], list_id)
        assert status == "closed" and events == before[1] + 1
        _refused(await client.post(f"/lists/{list_id}/convert", json={"target_type": "invoice"},
                                   headers=c["h"]["owner"]), 409)
    assert await _converted_docs(factory, c["company_id"], list_id) == 1
