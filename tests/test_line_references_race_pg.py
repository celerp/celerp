# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Undoing an import while a document or List adds a line for one of its items.

Each case holds one request open just before it commits, with every lock it took
still held, sends the other on its own connection, waits until Postgres reports it
blocked, then lets the first commit. Only the serial outcome is acceptable: either
the Undo removed the item and the save naming it is refused, or the save landed and
the Undo is refused because the item is now in use. A record never keeps a line for
an item that no longer exists.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from migration_support import auth, maker
from test_posting_roles_ingress import main_location
from test_posting_roles_race_pg_draft import _company, _ok, _race, race  # noqa: F401  (race is a fixture)

pytestmark = pytest.mark.asyncio


async def _imported(engine, client, tok: str, cid, sku: str) -> tuple[str, str]:
    """An item brought in by an import that can still be undone: (batch id, item id)."""
    from celerp.models.projections import Projection

    await main_location(client, auth(tok))
    r = await client.post("/items/import/rows", headers=auth(tok), json={"rows": [
        {"sku": sku, "name": "Imported", "sell_by": "piece", "quantity": "1", "cost_price": "10",
         "location_name": "Main"}], "upsert": False, "idempotency_key": f"undo-{sku}"})
    assert r.status_code == 200 and not r.json()["errors"], r.text
    async with maker(engine)() as s:
        [lot] = [row.entity_id for row in (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "item"))).scalars()
            if row.state.get("sku") == sku]
    return r.json()["batch_id"], lot


def _line(lot: str, sku: str) -> dict:
    return {"item_id": lot, "sku": sku, "name": "Imported", "quantity": 1, "unit_price": 20.0, "sell_by": "piece"}


_FREE = {"name": "Free text", "quantity": 1, "unit_price": 5.0}


async def _naming(engine, cid, lot: str) -> list[str]:
    """Every document or List whose stored lines link to ``lot``."""
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        return [row.entity_id for row in (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type.in_(("doc", "list"))))).scalars()
            if any(li.get("item_id") == lot for li in row.state.get("line_items") or [])]


async def _item_exists(engine, cid, lot: str) -> bool:
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        return await s.get(Projection, {"company_id": cid, "entity_id": lot}) is not None


def _create(client, tok: str, kind: str, lot: str, sku: str):
    body = ({"doc_type": "quotation"} if kind == "docs" else {"list_type": "quotation"}) | {
        "line_items": [_line(lot, sku)]}
    return lambda: client.post(f"/{kind}", headers=auth(tok), json=body)


def _patch(client, tok: str, kind: str, created: dict, lot: str, sku: str):
    return lambda: client.patch(f"/{kind}/{created['id']}", headers=auth(tok), json={
        "fields_changed": {"line_items": {"old": [_FREE], "new": [_FREE, _line(lot, sku)]}},
        "expected_version": created["event_id"]})


async def _writer(client, tok: str, kind: str, mode: str, lot: str, sku: str):
    if mode == "create":
        return _create(client, tok, kind, lot, sku)
    body = ({"doc_type": "quotation"} if kind == "docs" else {"list_type": "quotation"}) | {"line_items": [_FREE]}
    return _patch(client, tok, kind, await _ok(client, tok, f"/{kind}", body), lot, sku)


def _undo(client, tok: str, batch: str):
    return lambda: client.post(f"/items/import/batches/{batch}/undo", headers=auth(tok))


@pytest.mark.parametrize("mode", ["create", "patch"])
@pytest.mark.parametrize("kind", ["docs", "lists"])
async def test_a_save_waiting_on_an_import_undo_is_refused(committed_engine, race, kind, mode):
    client, hold = race
    cid, tok = await _company(committed_engine)
    sku = f"UQ-{kind}-{mode}"
    batch, lot = await _imported(committed_engine, client, tok, cid, sku)
    writer = await _writer(client, tok, kind, mode, lot, sku)

    undo, save = await _race(committed_engine, client, hold, _undo(client, tok, batch), writer)

    assert undo.status_code == 200, undo.text
    assert save.status_code == 422 and save.json()["detail"]["code"] == "invalid_reference", save.text
    assert not await _item_exists(committed_engine, cid, lot)
    assert await _naming(committed_engine, cid, lot) == []


@pytest.mark.parametrize("mode", ["create", "patch"])
@pytest.mark.parametrize("kind", ["docs", "lists"])
async def test_an_import_undo_waiting_on_a_save_is_refused(committed_engine, race, kind, mode):
    client, hold = race
    cid, tok = await _company(committed_engine)
    sku = f"UR-{kind}-{mode}"
    batch, lot = await _imported(committed_engine, client, tok, cid, sku)
    writer = await _writer(client, tok, kind, mode, lot, sku)

    save, undo = await _race(committed_engine, client, hold, writer, _undo(client, tok, batch))

    assert save.status_code == 200, save.text
    assert undo.status_code == 409 and undo.json()["detail"]["code"] == "import_items_modified", undo.text
    assert await _item_exists(committed_engine, cid, lot)
    assert len(await _naming(committed_engine, cid, lot)) == 1
