# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Archiving goods a memo is taking out, and deleting a draft being made available.

Each case holds one request open just before it commits, with every lock it took
still held, sends the other on its own connection, waits until Postgres reports it
blocked, then lets the first commit. Only the serial outcome is acceptable: goods
out on memo are never archived, a draft that became stock is never deleted, and the
books carry exactly the stock recorded on them afterwards.
"""
from __future__ import annotations

import pytest

from migration_support import auth
from test_posting_roles_race_pg_draft import (  # noqa: F401  (race is a fixture)
    _available, _books, _company, _draft, _draft_entries, _move, _ok, _race, _state, race,
)

pytestmark = pytest.mark.asyncio


async def _memo(client, tok: str, lot: str) -> str:
    memo = (await _ok(client, tok, "/docs", {"doc_type": "memo", "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, tok, f"/docs/{memo}/finalize")
    return memo


def _fulfil(client, tok: str, memo: str, lot: str):
    return lambda: client.post(f"/docs/{memo}/fulfill-lines", headers=auth(tok), json={"line_entity_ids": [lot]})


def _archive(client, tok: str, lot: str):
    return lambda: client.post("/items/bulk/status", headers=auth(tok),
                               json={"entity_ids": [lot], "status": "archived"})


def _delete(client, tok: str, lot: str):
    return lambda: client.post("/items/bulk/delete", headers=auth(tok), json={"entity_ids": [lot]})


async def test_archive_waiting_on_a_memo_fulfilment_is_refused(committed_engine, race):
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _available(client, tok, 100.0)
    memo = await _memo(client, tok, lot)

    fulfil, archive = await _race(committed_engine, client, hold, _fulfil(client, tok, memo, lot),
                                  _archive(client, tok, lot))

    assert fulfil.status_code == 200, fulfil.text
    assert archive.status_code == 409 and "resolve the document" in archive.text, archive.text
    state = await _state(committed_engine, cid, lot)
    assert (state["status"], state.get("status_doc_id")) == ("memo_out", memo)
    assert sum((await _books(committed_engine, cid)).values()) == 100


async def test_a_memo_fulfilment_waiting_on_an_archive_is_refused(committed_engine, race):
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _available(client, tok, 100.0)
    memo = await _memo(client, tok, lot)

    archive, fulfil = await _race(committed_engine, client, hold, _archive(client, tok, lot),
                                  _fulfil(client, tok, memo, lot))

    assert archive.status_code == 200, archive.text
    assert fulfil.status_code in (409, 422), fulfil.text
    state = await _state(committed_engine, cid, lot)
    assert state["status"] == "archived" and not state.get("status_doc_id")
    assert sum((await _books(committed_engine, cid)).values()) == 100


async def test_delete_waiting_on_make_available_is_refused(committed_engine, race):
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 100.0)

    made, delete = await _race(committed_engine, client, hold, _move(client, tok, "make-available", lot),
                               _delete(client, tok, lot))

    assert made.status_code == 200, made.text
    assert delete.status_code == 409, delete.text
    assert (await _state(committed_engine, cid, lot))["status"] == "available"
    assert await _draft_entries(committed_engine, cid, lot) == ["made-available"]
    assert sum((await _books(committed_engine, cid)).values()) == 100


async def test_make_available_waiting_on_a_delete_finds_nothing_to_book(committed_engine, race):
    from celerp.models.ledger import LedgerEntry
    from migration_support import maker
    from sqlalchemy import func, select

    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 100.0)

    delete, made = await _race(committed_engine, client, hold, _delete(client, tok, lot),
                               _move(client, tok, "make-available", lot))

    assert delete.status_code == 200, delete.text
    assert made.status_code in (404, 409, 422) or made.json().get("updated") == 0, made.text
    async with maker(committed_engine)() as s:
        assert await s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.entity_id == lot)) == 0
    assert await _draft_entries(committed_engine, cid, lot) == []
    assert sum((await _books(committed_engine, cid)).values()) == 0
