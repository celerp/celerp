# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Event-boundary uniqueness guard for physical document items.

A non-splittable physical inventory item must appear at most once on any
document. The guard lives in ``emit_event`` and keys on ``entity_type == "doc"``
(never a doc-type list), extracting the post-change line set by DATA SHAPE so
every current and future doc writer is covered by one rule. Splittable and
unlinked/free-text lines may repeat. Historical events replay unchanged because
rebuild applies events via ``apply_event``, never ``emit_event``.
"""
from __future__ import annotations

import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine


async def _register(client) -> str:
    addr = f"admin-{_uuid.uuid4().hex[:8]}@dedup.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Dedup Co", "email": addr, "name": "A", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _company_id(session) -> object:
    return (await session.execute(select(Company))).scalars().first().id


async def _item(client, t, sku, *, allow_splitting: bool) -> str:
    r = await client.post(
        "/items",
        headers=_h(t),
        json={
            "status": "available",
            "sku": sku,
            "name": sku,
            "quantity": 5,
            "sell_by": "piece",
            "allow_splitting": allow_splitting,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line(item_id=None, *, sku="", description="", quantity=1, entity_id=None):
    line = {"sku": sku, "description": description, "quantity": quantity}
    if item_id is not None:
        line["item_id"] = item_id
    if entity_id is not None:
        line["entity_id"] = entity_id
    return line


async def _emit_doc(session, cid, *, event_type, data, entity_id=None):
    return await emit_event(
        session,
        company_id=cid,
        entity_id=entity_id or f"doc:{_uuid.uuid4().hex[:12]}",
        entity_type="doc",
        event_type=event_type,
        data=data,
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(_uuid.uuid4()),
        metadata_={},
    )


async def _ledger_count(session, cid, entity_id) -> int:
    rows = (await session.execute(
        select(LedgerEntry.id).where(
            LedgerEntry.company_id == cid,
            LedgerEntry.entity_id == entity_id,
        )
    )).all()
    return len(rows)


# --- doc.created ---------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_created_rejects_duplicate_non_splittable(client, session):
    """doc.created with two identical non-splittable linked ids -> 409, no ledger row."""
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "NS-1", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    with pytest.raises(HTTPException) as exc:
        await _emit_doc(
            session, cid, event_type="doc.created", entity_id=doc_id,
            data={"doc_type": "invoice", "line_items": [_line(item), _line(item)]},
        )
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail.get("code") == "duplicate_document_item"
    assert await _ledger_count(session, cid, doc_id) == 0


@pytest.mark.asyncio
async def test_doc_updated_rejects_duplicate_in_new_lines(client, session):
    """doc.updated fields_changed.line_items.new with a duplicate non-splittable -> 409."""
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "NS-2", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"
    # Seed a clean draft first.
    await _emit_doc(
        session, cid, event_type="doc.created", entity_id=doc_id,
        data={"doc_type": "invoice", "line_items": [_line(item)]},
    )
    before = await _ledger_count(session, cid, doc_id)

    with pytest.raises(HTTPException) as exc:
        await _emit_doc(
            session, cid, event_type="doc.updated", entity_id=doc_id,
            data={"fields_changed": {"line_items": {"new": [_line(item), _line(item)]}}},
        )
    assert exc.value.status_code == 409
    assert await _ledger_count(session, cid, doc_id) == before


@pytest.mark.asyncio
async def test_doc_patched_rejects_duplicate_non_splittable(client, session):
    """doc.patched whose data['line_items'] carries a duplicate non-splittable item -> rejected."""
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "NS-3", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    with pytest.raises(HTTPException) as exc:
        await _emit_doc(
            session, cid, event_type="doc.patched", entity_id=doc_id,
            data={"doc_type": "invoice", "line_items": [_line(item), _line(item)]},
        )
    assert exc.value.status_code == 409
    assert await _ledger_count(session, cid, doc_id) == 0


@pytest.mark.asyncio
async def test_shared_import_rejects_duplicate_non_splittable(client, session):
    """doc.shared_import bundle with a duplicate non-splittable item -> rejected."""
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "NS-4", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    with pytest.raises(HTTPException) as exc:
        await _emit_doc(
            session, cid, event_type="doc.shared_import", entity_id=doc_id,
            data={"doc_type": "invoice", "line_items": [_line(item), _line(item)]},
        )
    assert exc.value.status_code == 409
    assert await _ledger_count(session, cid, doc_id) == 0


# --- negative guards (over-rejection); green at merge-base and after -----

@pytest.mark.asyncio
async def test_splittable_items_may_repeat(client, session):
    """Two lines of a splittable item -> accepted (guard must not over-reject)."""
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "SP-1", allow_splitting=True)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    await _emit_doc(
        session, cid, event_type="doc.created", entity_id=doc_id,
        data={"doc_type": "invoice", "line_items": [_line(item), _line(item)]},
    )
    assert await _ledger_count(session, cid, doc_id) == 1


@pytest.mark.asyncio
async def test_unlinked_freetext_lines_may_repeat(client, session):
    """Two unlinked/free-text lines (no item_id/entity_id) -> accepted."""
    t = await _register(client)
    cid = await _company_id(session)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    await _emit_doc(
        session, cid, event_type="doc.created", entity_id=doc_id,
        data={
            "doc_type": "invoice",
            "line_items": [
                _line(description="Delivery fee"),
                _line(description="Delivery fee"),
            ],
        },
    )
    assert await _ledger_count(session, cid, doc_id) == 1


# --- unresolvable reference ----------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_linked_ref_unresolvable_is_422(client, session):
    """A repeated id that resolves to no item projection -> 422 invalid reference."""
    t = await _register(client)
    cid = await _company_id(session)
    ghost = f"item:{_uuid.uuid4().hex}"  # never created
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    with pytest.raises(HTTPException) as exc:
        await _emit_doc(
            session, cid, event_type="doc.created", entity_id=doc_id,
            data={"doc_type": "invoice", "line_items": [_line(ghost), _line(ghost)]},
        )
    assert exc.value.status_code == 422
    assert await _ledger_count(session, cid, doc_id) == 0


# --- future / non-governed doc type --------------------------------------

@pytest.mark.asyncio
async def test_future_doc_type_not_governed(client, session):
    """A doc of a novel doc_type with duplicate non-splittable items is NOT governed by the
    outbound-only uniqueness invariant: the guard returns early and the event persists.

    The invariant governs only outbound customer-stock docs (invoice, memo). A novel type - like
    every inbound/internal type - legitimately allows the same physical item to appear more than
    once, so the guard must not reject it. Red at the PR head, where emit_event enforces on every
    entity_type=='doc' and raises 409; after the fix the guard is scoped and the write succeeds.
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "NS-FUT", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    await _emit_doc(
        session, cid, event_type="doc.created", entity_id=doc_id,
        data={"doc_type": "totally_new_doc_type_2099", "line_items": [_line(item), _line(item)]},
    )
    assert await _ledger_count(session, cid, doc_id) == 1


# --- rebuild exemption (regression lock); green before and after ---------

@pytest.mark.asyncio
async def test_rebuild_replays_historical_duplicate_unchanged(client, session):
    """A pre-existing duplicate event rebuilds byte-for-byte via apply_event.

    Inserts a duplicate doc.created directly into the ledger (bypassing the
    emit_event guard), then runs ProjectionEngine.rebuild and asserts it
    replays without raising. Proves the guard is new-write-only.
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "HIST-1", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    # Insert the historical duplicate event directly, bypassing emit_event.
    entry = LedgerEntry(
        company_id=cid,
        entity_id=doc_id,
        entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "invoice", "line_items": [_line(item), _line(item)]},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(_uuid.uuid4()),
        metadata_={},
    )
    session.add(entry)
    await session.flush()

    # Rebuild must replay the historical event without raising.
    await ProjectionEngine.rebuild(session)

    proj = (await session.execute(
        select(LedgerEntry.id).where(
            LedgerEntry.company_id == cid,
            LedgerEntry.entity_id == doc_id,
        )
    )).all()
    assert len(proj) == 1  # the historical event is intact
