# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A record of one kind is never changed, created over, or deleted as another kind.

Items, documents, Lists and contacts share one ID space per company. An item action
aimed at a document's ID (or the reverse) must be refused before anything is kept:
no new ledger row, no change to the record, no outbound sync row, no stored file.
Bulk item actions check every selected ID before changing any of them, and the bulk
Delete removes only item records and their own history. Rebuilding projections from
an older ledger that already holds such a crossed row stays deterministic.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.connector_config import OutboundQueue
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from test_helpers import perm_setup

pytestmark = pytest.mark.asyncio


async def _company_id(session):
    from celerp.models.company import Company
    return (await session.execute(select(Company.id).order_by(Company.created_at.desc()).limit(1))).scalar_one()


async def _doc(client, h) -> str:
    r = await client.post("/docs", json={"doc_type": "invoice"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _contact(client, h) -> str:
    r = await client.post("/crm/contacts", json={"name": "Buyer"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _snapshot(session, company_id, entity_id):
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id}, populate_existing=True)
    ledger = (await session.execute(
        select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id)
    )).scalar_one()
    queued = (await session.execute(
        select(func.count()).select_from(OutboundQueue).where(OutboundQueue.entity_id == entity_id)
    )).scalar_one()
    return (proj.entity_type if proj else None, dict(proj.state) if proj else None,
            proj.version if proj else None, ledger, queued)


async def _emit(session, company_id, entity_id, entity_type, event_type, data):
    return await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type=entity_type,
        event_type=event_type, data=data, actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )


@pytest.mark.parametrize("event_type,data", [
    ("item.updated", {"fields_changed": {"name": {"old": None, "new": "Hijacked"}}}),
    ("item.status.set", {"new_status": "sold"}),
    ("shop.sync.enabled", {}),
    ("item.expired", {}),
])
async def test_item_event_on_a_document_is_refused_and_leaves_nothing(client, session, event_type, data):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _doc(client, s["admin_h"])
    before = await _snapshot(session, company_id, doc_id)

    with pytest.raises(HTTPException) as exc:
        await _emit(session, company_id, doc_id, "item", event_type, data)
    assert exc.value.status_code == 404

    assert await _snapshot(session, company_id, doc_id) == before


async def test_document_event_on_an_item_is_refused(client, session):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    before = await _snapshot(session, company_id, s["item_id"])

    with pytest.raises(HTTPException) as exc:
        await _emit(session, company_id, s["item_id"], "doc", "doc.updated",
                    {"fields_changed": {"status": {"old": None, "new": "void"}}})
    assert exc.value.status_code == 404

    assert await _snapshot(session, company_id, s["item_id"]) == before


async def test_item_birth_over_a_contact_id_is_refused(client, session):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    contact_id = await _contact(client, s["admin_h"])
    before = await _snapshot(session, company_id, contact_id)

    with pytest.raises(HTTPException) as exc:
        await _emit(session, company_id, contact_id, "item", "item.created",
                    {"sku": "OVER", "name": "Over", "quantity": 1, "sell_by": "piece"})
    assert exc.value.status_code in (404, 409)

    assert await _snapshot(session, company_id, contact_id) == before


@pytest.mark.parametrize("path,body", [
    ("/items/bulk/delete", {}),
    ("/items/bulk/status", {"status": "sold"}),
    ("/items/bulk/shopify-sync", {"enable": True}),
    ("/items/bulk/expire", {}),
    ("/items/bulk/make-available", {}),
    ("/items/bulk/revert-to-draft", {}),
    ("/items/bulk/transfer", None),
])
async def test_bulk_item_action_with_a_document_id_changes_nothing(client, session, path, body):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _doc(client, s["admin_h"])
    if body is None:
        body = {"to_location_id": s["location_id"]}
    item_before = await _snapshot(session, company_id, s["item_id"])
    doc_before = await _snapshot(session, company_id, doc_id)

    r = await client.post(path, json={"entity_ids": [s["item_id"], doc_id], **body}, headers=s["admin_h"])
    assert r.status_code == 404, r.text

    assert await _snapshot(session, company_id, s["item_id"]) == item_before
    assert await _snapshot(session, company_id, doc_id) == doc_before


async def test_bulk_delete_with_an_unknown_id_deletes_nothing(client, session):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    item_before = await _snapshot(session, company_id, s["item_id"])

    r = await client.post("/items/bulk/delete", json={"entity_ids": [s["item_id"], "item:nope"]},
                          headers=s["admin_h"])
    assert r.status_code == 404, r.text
    assert await _snapshot(session, company_id, s["item_id"]) == item_before


async def test_bulk_delete_of_items_keeps_other_streams(client, session):
    """The item's own rows go; a document's rows stay even if it shares nothing but the company."""
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _doc(client, s["admin_h"])
    doc_before = await _snapshot(session, company_id, doc_id)

    r = await client.post("/items/bulk/delete", json={"entity_ids": [s["item_id"]]}, headers=s["admin_h"])
    assert r.status_code == 200, r.text
    gone = await _snapshot(session, company_id, s["item_id"])
    assert gone[0] is None and gone[3] == 0
    assert await _snapshot(session, company_id, doc_id) == doc_before


@pytest.mark.parametrize("method,suffix,body", [
    ("post", "transfer", "loc"),
    ("post", "reserve", {"quantity": 1}),
    ("post", "unreserve", {"quantity": 1}),
    ("post", "adjust", {"new_qty": 3}),
    ("post", "price", {"price_type": "retail_price", "new_price": 9}),
    ("post", "status", {"new_status": "sold"}),
    ("post", "expire", None),
])
async def test_single_item_action_on_a_document_id_is_refused(client, session, method, suffix, body):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _doc(client, s["admin_h"])
    if body == "loc":
        body = {"to_location_id": s["location_id"]}
    before = await _snapshot(session, company_id, doc_id)

    r = await getattr(client, method)(f"/items/{doc_id}/{suffix}", json=body, headers=s["admin_h"])
    assert r.status_code == 404, r.text
    assert await _snapshot(session, company_id, doc_id) == before


async def test_item_file_upload_on_a_document_id_stores_nothing(client, session, tmp_path, monkeypatch):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _doc(client, s["admin_h"])
    before = await _snapshot(session, company_id, doc_id)

    r = await client.post(f"/items/{doc_id}/files", files={"file": ("a.txt", b"hello", "text/plain")},
                          headers=s["admin_h"])
    assert r.status_code == 404, r.text
    assert await _snapshot(session, company_id, doc_id) == before


async def test_document_bulk_draft_delete_leaves_a_draft_item(client, session):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    r = await client.post("/items", json={"sku": "DRAFT-1", "name": "Draft", "quantity": 1,
                                          "location_id": s["location_id"], "sell_by": "piece",
                                          "status": "draft"}, headers=s["admin_h"])
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    before = await _snapshot(session, company_id, item_id)
    assert before[1].get("status") == "draft"

    r = await client.delete("/docs/bulk-draft", params={"doc_ids": item_id}, headers=s["admin_h"])
    assert r.status_code == 200, r.text
    assert await _snapshot(session, company_id, item_id) == before


async def test_rebuild_with_a_historical_crossed_row_is_deterministic(client, session):
    """An older ledger may already hold an item row on a document's ID; rebuilding twice agrees."""
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _doc(client, s["admin_h"])
    session.add(LedgerEntry(
        company_id=company_id, entity_id=doc_id, entity_type="item", event_type="item.updated",
        data={"fields_changed": {"memo": {"old": None, "new": "legacy"}}}, actor_id=None,
        location_id=None, source="legacy", idempotency_key=str(uuid.uuid4()), metadata_={},
    ))
    await session.flush()

    async def _states():
        rows = (await session.execute(
            select(Projection).where(Projection.company_id == company_id)
            .execution_options(populate_existing=True)
        )).scalars().all()
        return {r.entity_id: (r.entity_type, r.state, r.version) for r in rows}

    await ProjectionEngine.rebuild(session, company_id)
    await session.flush()
    first = await _states()
    await ProjectionEngine.rebuild(session, company_id)
    await session.flush()
    assert await _states() == first
    assert first[doc_id][0] == "doc"
    assert first[s["item_id"]][0] == "item"
