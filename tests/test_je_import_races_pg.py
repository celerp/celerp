# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A journal entry imported twice at once is created once.

Two journal imports naming the same entry under different keys run on real PostgreSQL
across two connections, in both orders, through the full HTTP stack. The first holds
its commit until the second has started and is waiting on (or past) the lock. The
second must see the first's entry once it may write: it is counted skipped, the ledger
holds one creation, and the entry keeps the first import's details even when the second
carries different ones. The same file sent twice at once (one key) is created once too,
and an import whose access is removed while it waits writes nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_fresh_authority_races_pg import _app_client, _direct, _http, _ok, _race, _refused, _revoke, _seed

pytestmark = pytest.mark.asyncio

_JE = "je:RACE-1"
_FIRST = {"memo": "First import", "entries": []}
_SECOND = {"memo": "Second import", "entries": []}


def _import(client, c, role, key, data):
    body = {"records": [{"entity_id": _JE, "event_type": "acc.journal_entry.created", "data": data,
                         "source": "import", "idempotency_key": key}]}
    return lambda: client.post("/accounting/import/batch", json=body, headers=c["h"][role])


async def _entry(factory, company_id) -> tuple[dict | None, list[str]]:
    """(the entry's state or None, the idempotency keys of every creation of it)."""
    async with factory() as s:
        row = await s.get(Projection, {"company_id": company_id, "entity_id": _JE})
        births = (await s.execute(select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == _JE,
            LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().all()
    return (dict(row.state) if row else None), list(births)


def _counts(resp) -> tuple[int, int, list]:
    _ok(resp)
    body = resp.json()
    return body["created"], body["skipped"], body["errors"]


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.parametrize("first_wins", [True, False], ids=["first-payload-wins", "second-payload-wins"])
async def test_one_entry_imported_twice_at_once_under_different_keys_is_created_once(committed_engine, first_wins):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    (win_key, win_data), (lose_key, lose_data) = (("key-a", _FIRST), ("key-b", _SECOND))[::1 if first_wins else -1]
    async with _app_client(factory) as (client, pending):
        won, lost = await _race(
            committed_engine,
            lambda held: _http(pending, held, _import(client, c, "owner", win_key, win_data)),
            lambda held: _http(pending, held, _import(client, c, "manager", lose_key, lose_data)),
        )
    assert _counts(won) == (1, 0, [])
    assert _counts(lost) == (0, 1, [])
    state, births = await _entry(factory, c["company_id"])
    assert births == [win_key]
    assert state["memo"] == win_data["memo"]


async def test_the_same_entry_import_sent_twice_at_once_is_created_once(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        one, two = await _race(
            committed_engine,
            lambda held: _http(pending, held, _import(client, c, "owner", "key-same", _FIRST)),
            lambda held: _http(pending, held, _import(client, c, "owner", "key-same", _FIRST)),
        )
    assert _counts(one) == (1, 0, [])
    assert _counts(two) == (0, 1, [])
    state, births = await _entry(factory, c["company_id"])
    assert births == ["key-same"] and state["memo"] == _FIRST["memo"]


async def test_an_entry_import_whose_access_is_removed_while_it_waits_writes_nothing(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with _app_client(factory) as (client, pending):
        revoked, imported = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "import_export_data", "manager")),
            lambda held: _http(pending, held, _import(client, c, "manager", "key-a", _FIRST)),
        )
    _ok(revoked)
    _refused(imported, 403)
    assert await _entry(factory, c["company_id"]) == (None, [])
