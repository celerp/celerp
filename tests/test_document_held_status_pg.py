# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reserved and out on memo belong to the document that set them.

A document reserves an item or sends it out on memo, and only that document
releases it. None of the three generic status doors (the item's status action, the
bulk status action, and an item edit) can put an item into either status, or take
it out of one, and a refused edit leaves the document's hold on the item intact.
A sold item can still be archived.
"""
from __future__ import annotations

import pytest

from migration_support import auth
from test_posting_roles_race_pg_custody import _memo
from test_posting_roles_race_pg_draft import _available, _company, _ok, _state, race  # noqa: F401  (race is a fixture)

pytestmark = pytest.mark.asyncio


def _single(client, tok, lot, status):
    return client.post(f"/items/{lot}/status", headers=auth(tok), json={"new_status": status})


def _bulk(client, tok, lot, status):
    return client.post("/items/bulk/status", headers=auth(tok), json={"entity_ids": [lot], "status": status})


async def _patch(client, tok, lot, status):
    return await client.patch(f"/items/{lot}", headers=auth(tok), json={
        "fields_changed": {"status": {"old": None, "new": status}}})


_DOORS = [_single, _bulk, _patch]
_DOOR_IDS = ["status", "bulk-status", "patch"]


async def _reserved(client, tok) -> tuple[str, str]:
    lot = await _available(client, tok, 100)
    memo = await _memo(client, tok, lot)
    await _ok(client, tok, f"/docs/{memo}/reserve-lines", {"new_status": "reserved", "line_entity_ids": [lot]})
    return lot, memo


async def _out_on_memo(client, tok) -> tuple[str, str]:
    lot = await _available(client, tok, 100)
    memo = await _memo(client, tok, lot)
    await _ok(client, tok, f"/docs/{memo}/fulfill-lines", {"line_entity_ids": [lot]})
    return lot, memo


@pytest.mark.parametrize("door", _DOORS, ids=_DOOR_IDS)
@pytest.mark.parametrize("status", ["reserved", "memo_out"])
async def test_a_status_edit_cannot_put_an_item_in_a_document_status(committed_engine, race, door, status):
    client, _ = race
    cid, tok = await _company(committed_engine)
    lot = await _available(client, tok, 100)

    r = await door(client, tok, lot, status)

    assert r.status_code == 422, r.text
    assert "not a direct status edit" in r.json()["detail"]
    state = await _state(committed_engine, cid, lot)
    assert (state["status"], state.get("status_doc_id")) == ("available", None)


@pytest.mark.parametrize("door", _DOORS, ids=_DOOR_IDS)
@pytest.mark.parametrize("held", [_reserved, _out_on_memo], ids=["reserved", "memo_out"])
@pytest.mark.parametrize("target", ["available", "active", "returned", "archived"])
async def test_a_status_edit_cannot_take_an_item_from_its_document(committed_engine, race, door, held, target):
    client, _ = race
    cid, tok = await _company(committed_engine)
    lot, memo = await held(client, tok)
    before = await _state(committed_engine, cid, lot)

    r = await door(client, tok, lot, target)

    assert r.status_code == 409, r.text
    assert "held by document" in r.json()["detail"]
    after = await _state(committed_engine, cid, lot)
    assert (after["status"], after.get("status_doc_id")) == (before["status"], memo)
    assert after.get("status_doc_number") == before.get("status_doc_number")


async def test_the_document_still_releases_its_reservation(committed_engine, race):
    client, _ = race
    cid, tok = await _company(committed_engine)
    lot, memo = await _reserved(client, tok)

    await _ok(client, tok, f"/docs/{memo}/reserve-lines", {"new_status": "available", "line_entity_ids": [lot]})

    state = await _state(committed_engine, cid, lot)
    assert (state["status"], state.get("status_doc_id")) == ("available", None)


@pytest.mark.parametrize("door", _DOORS, ids=_DOOR_IDS)
async def test_a_sold_item_can_still_be_archived(committed_engine, race, door):
    client, _ = race
    cid, tok = await _company(committed_engine)
    lot = await _available(client, tok, 100)
    invoice = (await _ok(client, tok, "/docs", {"doc_type": "invoice", "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, tok, f"/docs/{invoice}/finalize")
    await _ok(client, tok, f"/docs/{invoice}/fulfill-lines", {"line_entity_ids": [lot]})
    assert (await _state(committed_engine, cid, lot))["status"] == "sold"

    r = await door(client, tok, lot, "archived")

    assert r.status_code == 200, r.text
    assert (await _state(committed_engine, cid, lot))["status"] == "archived"
