# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Renumbering a document and changing its files while another write is in flight,
on real PostgreSQL.

Each change waits for the write ahead of it and is judged against what that write
committed: a renumber that waited for a void is refused, a file that was deleted
meanwhile cannot be deleted or edited again, two documents cannot both take one
number, and a user who loses access while waiting changes nothing.
"""
from __future__ import annotations

import io
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.attachments import attach_file
from test_fresh_authority_races_pg import _app_client, _direct, _http, _ok, _race, _refused, _revoke, _seed

pytestmark = pytest.mark.asyncio

_FILE = "file-1"


async def _record(factory, c, entity_type: str, ref: str) -> str:
    """A final invoice (or a contact) carrying one attached file."""
    entity_id = f"{entity_type}:{uuid.uuid4()}"
    data = ({"doc_type": "invoice", "ref_id": ref, "doc_number": ref, "status": "final", "total": 10, "subtotal": 10}
            if entity_type == "doc" else {"name": ref, "contact_type": "customer"})
    async with factory() as s:
        await emit_event(s, company_id=c["company_id"], entity_id=entity_id, entity_type=entity_type,
                         event_type="doc.created" if entity_type == "doc" else "crm.contact.created", data=data,
                         actor_id=c["ids"]["owner"], location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
        await attach_file(s, c["company_id"], entity_type, entity_id,
                          {"id": _FILE, "filename": "scan.pdf", "mime": "application/pdf", "size": 3, "url": ""},
                          c["ids"]["owner"])
        await s.commit()
    return entity_id


async def _snapshot(factory, c, entity_id: str) -> tuple[dict, int]:
    async with factory() as s:
        row = await s.get(Projection, {"company_id": c["company_id"], "entity_id": entity_id})
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == c["company_id"], LedgerEntry.entity_id == entity_id))).scalar_one()
    return dict(row.state), events


def _send(client, pending, c, role: str, method: str, path: str, **kw):
    return lambda held: _http(pending, held, lambda: client.request(method, path, headers=c["h"][role], **kw))


async def _setup(committed_engine, entity_type: str = "doc"):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    return factory, c, await _record(factory, c, entity_type, f"INV-{uuid.uuid4().hex[:6]}")


async def test_a_renumber_that_waited_for_a_void_is_refused(committed_engine):
    factory, c, doc = await _setup(committed_engine)
    before, _ = await _snapshot(factory, c, doc)
    async with _app_client(factory) as (client, pending):
        voided, renumbered = await _race(
            committed_engine,
            _send(client, pending, c, "owner", "POST", f"/docs/{doc}/void", json={"reason": "wrong"}),
            _send(client, pending, c, "manager", "POST", f"/docs/{doc}/renumber", json={"ref_id": "NEW-1"}),
        )
    _ok(voided)
    _refused(renumbered, 409)
    after, _ = await _snapshot(factory, c, doc)
    assert (after["status"], after["ref_id"]) == ("void", before["ref_id"])


async def test_a_void_that_waited_for_a_renumber_voids_the_renumbered_document(committed_engine):
    factory, c, doc = await _setup(committed_engine)
    async with _app_client(factory) as (client, pending):
        renumbered, voided = await _race(
            committed_engine,
            _send(client, pending, c, "manager", "POST", f"/docs/{doc}/renumber", json={"ref_id": "NEW-2"}),
            _send(client, pending, c, "owner", "POST", f"/docs/{doc}/void", json={"reason": "wrong"}),
        )
    _ok(renumbered)
    _ok(voided)
    after, _ = await _snapshot(factory, c, doc)
    assert (after["status"], after["ref_id"]) == ("void", "NEW-2")


async def test_two_documents_cannot_take_one_number_at_once(committed_engine):
    factory, c, doc = await _setup(committed_engine)
    other = await _record(factory, c, "doc", f"INV-{uuid.uuid4().hex[:6]}")
    async with _app_client(factory) as (client, pending):
        one, two = await _race(
            committed_engine,
            _send(client, pending, c, "manager", "POST", f"/docs/{doc}/renumber", json={"ref_id": "SAME-1"}),
            _send(client, pending, c, "manager", "POST", f"/docs/{other}/renumber", json={"ref_id": "SAME-1"}),
        )
    _ok(one)
    _refused(two, 409)
    assert (await _snapshot(factory, c, other))[0]["ref_id"] != "SAME-1"


@pytest.mark.parametrize("entity_type, prefix", [("doc", "/docs"), ("contact", "/crm/contacts")])
async def test_a_file_deleted_twice_at_once_is_deleted_once(committed_engine, entity_type, prefix):
    factory, c, entity_id = await _setup(committed_engine, entity_type)
    _, events = await _snapshot(factory, c, entity_id)
    path = f"{prefix}/{entity_id}/files/{_FILE}"
    async with _app_client(factory) as (client, pending):
        one, two = await _race(committed_engine, _send(client, pending, c, "manager", "DELETE", path),
                               _send(client, pending, c, "manager", "DELETE", path))
    _ok(one)
    _refused(two, 404)
    state, after = await _snapshot(factory, c, entity_id)
    assert after == events + 1 and not [f for f in state.get("files", []) if not f.get("deleted")]


@pytest.mark.parametrize("entity_type, prefix, tag_method", [("doc", "/docs", "PATCH"), ("contact", "/crm/contacts", "POST")])
async def test_a_file_tagged_after_it_was_deleted_is_refused(committed_engine, entity_type, prefix, tag_method):
    factory, c, entity_id = await _setup(committed_engine, entity_type)
    path = f"{prefix}/{entity_id}/files/{_FILE}"
    async with _app_client(factory) as (client, pending):
        deleted, tagged = await _race(
            committed_engine,
            _send(client, pending, c, "manager", "DELETE", path),
            _send(client, pending, c, "manager", tag_method, f"{path}/tag", data={"document_tag": "receipt"}),
        )
    _ok(deleted)
    _refused(tagged, 404)


_DOC_WRITES = {
    "renumber": ("POST", "/renumber", {"json": {"ref_id": "LATE-1"}}),
    "tag": ("PATCH", f"/files/{_FILE}/tag", {"data": {"document_tag": "receipt"}}),
    "description": ("PATCH", f"/files/{_FILE}/description", {"data": {"description": "late"}}),
    "delete": ("DELETE", f"/files/{_FILE}", {}),
    "upload": ("POST", "/files", {"files": {"file": ("late.txt", io.BytesIO(b"late"), "text/plain")}}),
}


@pytest.mark.parametrize("write", sorted(_DOC_WRITES))
async def test_a_document_writer_whose_access_is_revoked_while_waiting_changes_nothing(committed_engine, write):
    method, suffix, kw = _DOC_WRITES[write]
    factory, c, doc = await _setup(committed_engine)
    before = await _snapshot(factory, c, doc)
    async with _app_client(factory) as (client, pending):
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "edit_documents", "manager")),
            _send(client, pending, c, "manager", method, f"/docs/{doc}{suffix}", **kw),
        )
    _ok(changed)
    _refused(wrote, 403)
    assert await _snapshot(factory, c, doc) == before
