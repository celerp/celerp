# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A file uploaded to an item that is deleted at the same moment is not kept.

Either upload door (the attachments one and the files one) can read the item, store the
file and then find the item gone when it records the upload. The upload is refused, the
item stays deleted with no event left for it, and the stored file and its thumbnail are
deleted again. Runs on real PostgreSQL with local storage, the Delete committing while
the upload is under way. The other way round, an upload that holds the item first is
saved and the Delete waits for it, then deletes the item as usual.
"""

from __future__ import annotations

import asyncio
import io
import types
import uuid

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers

from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp_inventory import routes as inventory
from celerp_inventory import routes_attachments as uploads

pytestmark = pytest.mark.asyncio

_ITEM = "item:x"


def _local_files(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


def _photo() -> UploadFile:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 40, 40)).save(out, "PNG")
    out.seek(0)
    return UploadFile(out, filename="photo.png", headers=Headers({"content-type": "image/png"}))


def _record_stores(monkeypatch) -> list[str]:
    """Ids of the files the upload stores, so the test knows it got that far."""
    from celerp.services import attachments
    stored: list[str] = []
    real = attachments.store_file

    async def store_file(*args, **kwargs):
        meta = await real(*args, **kwargs)
        stored.append(meta["id"])
        return meta

    monkeypatch.setattr(attachments, "store_file", store_file)
    return stored


async def _seed(factory) -> tuple[uuid.UUID, types.SimpleNamespace]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Uploads", slug=f"uploads-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await emit_event(
            s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.created",
            data={"sku": "X", "name": "X", "quantity": 1, "sell_by": "piece", "status": "available"},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


def _attachments_door(s, company_id, user):
    return uploads.upload_attachment(_ITEM, file=_photo(), attachment_type=None, company_id=company_id,
                                     _=None, user=user, session=s)


def _files_door(s, company_id, user):
    return uploads.upload_item_file(_ITEM, file=_photo(), document_tag=None, as_hero=False,
                                    company_id=company_id, _=None, user=user, session=s)


def _delete(s, company_id, user):
    return inventory.bulk_delete(inventory.BulkDeleteBody(entity_ids=[_ITEM]), company_id=company_id,
                                 _=None, user=user, session=s)


async def _deferred() -> None:
    """Stands in for a commit the test makes itself, later."""


async def _until_waiting_or_done(engine, task: asyncio.Task) -> None:
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the upload neither waited nor finished")


async def _rows(engine, company_id, table: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            f"SELECT count(*) FROM {table} WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one()


def _company_files(tmp_path, company_id) -> list[str]:
    return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
                  if p.is_file() and str(company_id) in str(p.relative_to(tmp_path)))


@pytest.mark.parametrize("door", [_attachments_door, _files_door], ids=["attachments", "files"])
async def test_an_upload_racing_a_delete_leaves_nothing_behind(committed_engine, tmp_path, monkeypatch, door):
    _local_files(monkeypatch, tmp_path)
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    stored = _record_stores(monkeypatch)

    async with factory() as a, factory() as b:
        commit_delete = a.commit
        a.commit = _deferred
        await _delete(a, company_id, user)
        upload = asyncio.create_task(door(b, company_id, user))
        await _until_waiting_or_done(committed_engine, upload)
        await commit_delete()
        outcome = (await asyncio.gather(asyncio.wait_for(upload, timeout=30), return_exceptions=True))[0]
        await b.rollback()

    assert isinstance(outcome, BaseException), outcome
    if isinstance(outcome, HTTPException):
        assert outcome.status_code in (404, 409), outcome.detail
    assert await _rows(committed_engine, company_id, "projections") == 0
    assert await _rows(committed_engine, company_id, "ledger") == 0
    assert stored, "the upload never reached storing its file"
    assert _company_files(tmp_path, company_id) == []


@pytest.mark.parametrize("door", [_attachments_door, _files_door], ids=["attachments", "files"])
async def test_an_upload_whose_commit_lands_then_fails_keeps_its_file(committed_engine, tmp_path, monkeypatch, door):
    """The commit reaches the database and the connection drops before it confirms: the
    item records the file, so the stored file must still be there."""
    _local_files(monkeypatch, tmp_path)
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    stored = _record_stores(monkeypatch)

    async with factory() as s:
        real_commit = s.commit

        async def commit_then_drop():
            await real_commit()
            raise ConnectionResetError("connection lost after commit")

        s.commit = commit_then_drop
        with pytest.raises(ConnectionResetError):
            await door(s, company_id, user)

    async with committed_engine.connect() as conn:
        state = (await conn.execute(text(
            "SELECT state::jsonb FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one()
    recorded = {f.get("id") for f in (state.get("attachments") or []) + (state.get("files") or [])}
    assert stored and stored[0] in recorded
    assert any(stored[0] in path for path in _company_files(tmp_path, company_id))


@pytest.mark.parametrize("door", [_attachments_door, _files_door], ids=["attachments", "files"])
async def test_an_upload_holding_the_item_first_is_saved_and_the_delete_waits(committed_engine, tmp_path, monkeypatch, door):
    _local_files(monkeypatch, tmp_path)
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)
    stored = _record_stores(monkeypatch)

    async with factory() as a, factory() as b:
        commit_upload = a.commit
        a.commit = _deferred
        uploaded = await door(a, company_id, user)
        delete = asyncio.create_task(_delete(b, company_id, user))
        await _until_waiting_or_done(committed_engine, delete)
        assert not delete.done(), "the Delete did not wait for the upload holding the item"
        await commit_upload()
        deleted = await asyncio.wait_for(delete, timeout=30)

    assert uploaded and stored
    assert deleted == {"deleted": 1, "kept": 0}
    assert await _rows(committed_engine, company_id, "projections") == 0
    assert await _rows(committed_engine, company_id, "ledger") == 0
