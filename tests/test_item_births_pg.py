# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An item event can only change an item that exists.

Only item.created and item.snapshot bring an item into being. Every other item event
naming an item that does not exist, because it never did or because it was deleted
while the request waited, is refused with 404 and leaves nothing behind: no ledger
row, no item, no stored file.

The race cases hold a draft's Delete open just before it commits, with every lock it
took still held, send the other request on its own connection, wait until Postgres
reports it blocked, then let the Delete commit.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from migration_support import auth, maker
from test_posting_roles_race_pg_draft import _company, _draft, _race, race  # noqa: F401  (race is a fixture)

pytestmark = pytest.mark.asyncio

_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


async def _left_behind(engine, cid, lot: str) -> tuple[bool, int]:
    """(the item exists, ledger rows naming it)."""
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        exists = await s.get(Projection, {"company_id": cid, "entity_id": lot}) is not None
        rows = await s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.entity_id == lot))
        return exists, rows


def _stored_files(cid) -> list[str]:
    from celerp.services.attachments import company_attachment_dir

    folder = company_attachment_dir(str(cid))
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _delete(client, tok: str, lot: str):
    return lambda: client.post("/items/bulk/delete", headers=auth(tok), json={"entity_ids": [lot]})


def _rename(client, tok: str, lot: str):
    return lambda: client.patch(f"/items/{lot}", headers=auth(tok), json={
        "fields_changed": {"name": {"old": "Lot", "new": "Renamed"}}})


def _upload(client, tok: str, lot: str):
    return lambda: client.post(f"/items/{lot}/files", headers=auth(tok),
                               files={"file": ("photo.png", _PNG, "image/png")})


def _sync(client, tok: str, lot: str):
    return lambda: client.post("/items/bulk/shopify-sync", headers=auth(tok), json={"entity_ids": [lot]})


@pytest.mark.parametrize("second", [_rename, _upload, _sync], ids=["patch", "file-upload", "shop-sync"])
async def test_an_item_event_waiting_on_a_delete_is_refused(committed_engine, race, tmp_path, monkeypatch, second):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 10)

    deleted, late = await _race(committed_engine, client, hold, _delete(client, tok, lot), second(client, tok, lot))

    assert deleted.status_code == 200, deleted.text
    assert late.status_code == 404, late.text
    assert await _left_behind(committed_engine, cid, lot) == (False, 0)
    assert _stored_files(cid) == []


@pytest.mark.parametrize("request_", [_rename, _upload, _sync], ids=["patch", "file-upload", "shop-sync"])
async def test_an_item_event_for_an_item_that_never_existed_is_refused(committed_engine, race, tmp_path, monkeypatch, request_):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    client, _ = race
    cid, tok = await _company(committed_engine)
    ghost = f"item:{uuid.uuid4()}"

    r = await request_(client, tok, ghost)()

    assert r.status_code == 404, r.text
    assert await _left_behind(committed_engine, cid, ghost) == (False, 0)
    assert _stored_files(cid) == []


async def _a_location(engine, cid):
    from celerp.models.company import Location

    async with maker(engine)() as s:
        return (await s.scalars(select(Location.id).where(Location.company_id == cid))).first()


_EVENTS = [
    ("item.updated", {"fields_changed": {"name": {"old": "Lot", "new": "Renamed"}}}),
    ("item.file.attached", {"entity_type": "item", "file_id": "f1", "filename": "a.png", "mime": "image/png",
                            "size": 1}),
    ("item.transferred", {}),
    ("item.status.set", {"new_status": "available"}),
    ("shop.sync.enabled", {}),
]


@pytest.mark.parametrize("gone", ["never-existed", "erased"])
@pytest.mark.parametrize("event_type,data", _EVENTS, ids=[e for e, _ in _EVENTS])
async def test_emitting_a_non_birth_event_for_a_missing_item_is_refused(committed_engine, race, gone, event_type, data):
    from celerp.events.engine import emit_event

    client, _ = race
    cid, tok = await _company(committed_engine)
    if gone == "erased":
        lot = await _draft(client, tok, 10)
        assert (await _delete(client, tok, lot)()).status_code == 200
    else:
        lot = f"item:{uuid.uuid4()}"
    data = data | ({"entity_id": lot} if "entity_type" in data else {})
    if event_type == "item.transferred":
        data = {"to_location_id": str(await _a_location(committed_engine, cid))}

    async with maker(committed_engine)() as s:
        with pytest.raises(HTTPException) as refused:
            await emit_event(s, company_id=cid, entity_id=lot, entity_type="item", event_type=event_type,
                             data=data, actor_id=None, location_id=None, source="api",
                             idempotency_key=str(uuid.uuid4()), metadata_={})
        await s.commit()

    assert refused.value.status_code == 404
    assert await _left_behind(committed_engine, cid, lot) == (False, 0)


async def test_a_refused_event_leaves_the_rest_of_the_request_intact(committed_engine, race):
    """The refusal rolls back only its own event; earlier work in the same session stands."""
    from celerp.events.engine import emit_event

    client, _ = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 10)
    ghost = f"item:{uuid.uuid4()}"

    async with maker(committed_engine)() as s:
        await emit_event(s, company_id=cid, entity_id=lot, entity_type="item", event_type="item.updated",
                         data={"fields_changed": {"name": {"old": "Lot", "new": "Kept"}}}, actor_id=None,
                         location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
        with pytest.raises(HTTPException):
            await emit_event(s, company_id=cid, entity_id=ghost, entity_type="item", event_type="item.updated",
                             data={"fields_changed": {"name": {"old": "Lot", "new": "Ghost"}}}, actor_id=None,
                             location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
        await s.commit()

    from celerp.models.projections import Projection
    async with maker(committed_engine)() as s:
        assert (await s.get(Projection, {"company_id": cid, "entity_id": lot})).state["name"] == "Kept"
    assert await _left_behind(committed_engine, cid, ghost) == (False, 0)
