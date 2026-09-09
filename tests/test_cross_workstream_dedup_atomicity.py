# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every doc-emitting path inherits the event-boundary uniqueness guard.

The guard lives once in ``emit_event`` (keyed on ``entity_type == "doc"``), so
the paths that build a document from another source - quotation/memo/
consignment conversion, list materialization, subscription generation, and
batch import - are covered with no per-path code. These tests drive each path
through its real entry point (the route handler or its service) and assert two
things: the duplicate is rejected with the documented status, AND no partial
state is persisted (the source is unchanged and no target ledger/projection row
exists). A path that cannot construct a duplicate by its own construction is
documented in its test docstring and asserts the nearest real invariant instead
of faking one.

All requests in a test share one uncommitted transaction (the ``session``/
``client`` fixtures), so querying the projection after a rejected write sees
exactly what a real rollback would leave: nothing.
"""
from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine


# --- shared helpers (match tests/test_document_item_dedup.py style) --------


async def _register(client) -> str:
    addr = f"admin-{_uuid.uuid4().hex[:8]}@xwsdedup.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "XWS Dedup Co", "email": addr, "name": "A", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _company_id(session):
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


def _line(item_id=None, *, sku="", description="", quantity=1, unit_price=10):
    line = {"sku": sku, "description": description, "quantity": quantity, "unit_price": unit_price}
    if item_id is not None:
        line["item_id"] = item_id
    return line


async def _doc_state(session, cid, doc_id):
    """The projected state of a doc, or None if no projection exists."""
    row = await session.get(Projection, {"company_id": cid, "entity_id": doc_id})
    return row.state if row else None


async def _ledger_count(session, cid, entity_id) -> int:
    rows = (await session.execute(
        select(LedgerEntry.id).where(
            LedgerEntry.company_id == cid,
            LedgerEntry.entity_id == entity_id,
        )
    )).all()
    return len(rows)


async def _target_doc_count(session, cid, doc_type) -> int:
    """How many doc projections of a given doc_type exist for the company."""
    rows = (await session.execute(
        select(Projection.entity_id).where(
            Projection.company_id == cid,
            Projection.entity_type == "doc",
            Projection.state["doc_type"].as_string() == doc_type,
        )
    )).all()
    return len(rows)


async def _seed_historical_doc(session, cid, doc_id, data):
    """Insert a doc.created straight into the ledger, bypassing the emit_event
    guard, then rebuild so the projection exists. This is how a source doc that
    carries a duplicate non-splittable item is constructed for paths whose own
    source-creation would otherwise trip the guard (conversions, subscriptions).
    """
    entry = LedgerEntry(
        company_id=cid,
        entity_id=doc_id,
        entity_type="doc",
        event_type="doc.created",
        data=data,
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(_uuid.uuid4()),
        metadata_={},
    )
    session.add(entry)
    await session.flush()
    await ProjectionEngine.rebuild(session)


# --- 1. quotation -> invoice conversion ------------------------------------


@pytest.mark.asyncio
async def test_quotation_to_invoice_duplicate_is_atomic(client, session):
    """convert_doc (POST /docs/{id}/convert) on a quotation carrying the same
    non-splittable item twice is rejected 409 and nothing is committed.

    The quotation is seeded as a historical doc (its own creation would trip the
    guard), then converted. Conversion copies the source line_items verbatim and
    emits the new invoice's doc.created BEFORE session.commit(), so the guard
    fires there: no invoice projection is created and the source quotation is
    still a quotation (its doc.converted never emitted).
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "QI-NS", allow_splitting=False)
    quo_id = f"doc:{_uuid.uuid4().hex[:12]}"
    await _seed_historical_doc(session, cid, quo_id, {
        "doc_type": "quotation", "status": "final",
        "line_items": [_line(item, sku="QI-NS"), _line(item, sku="QI-NS")],
    })
    before_invoices = await _target_doc_count(session, cid, "invoice")

    r = await client.post(f"/docs/{quo_id}/convert", headers=_h(t))

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "duplicate_document_item"
    # Atomicity: no invoice materialized, quotation unchanged and not converted.
    assert await _target_doc_count(session, cid, "invoice") == before_invoices
    src = await _doc_state(session, cid, quo_id)
    assert src is not None and src.get("doc_type") == "quotation"
    assert src.get("status") != "converted" and not src.get("converted_to")


# --- 2. memo -> invoice conversion -----------------------------------------


@pytest.mark.asyncio
async def test_memo_to_invoice_duplicate_is_atomic(client, session):
    """memo->invoice conversion CANNOT construct a duplicate target line by its
    own construction, so this asserts the nearest real invariant.

    The memo path does not copy the memo's line_items: it rebuilds the invoice
    from the memo's allocation set (every backing lot), keyed by each lot's own
    distinct entity_id (routes.py memo branch, billed_by_bound_eid). Two lines of
    one SKU bill two distinct lot entity_ids, so the target can never carry the
    same linked id twice and the duplicate guard is structurally unreachable on
    this path. Rather than fake a duplicate, this test proves the real invariant
    the path does enforce: a memo with no memo_out backing is rejected 422 and no
    invoice is materialized. The duplicate-guard coverage for memos is proven at
    the event boundary by test_document_item_dedup (doc.created on the rebuilt
    invoice would trip the same guard if a duplicate id ever reached it).
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "MI-NS", allow_splitting=False)
    memo_id = f"doc:{_uuid.uuid4().hex[:12]}"
    # A finalized memo with line_items but no memo_out backing lots.
    await _seed_historical_doc(session, cid, memo_id, {
        "doc_type": "memo", "status": "final",
        "line_items": [_line(item, sku="MI-NS")],
    })
    before_invoices = await _target_doc_count(session, cid, "invoice")

    r = await client.post(f"/docs/{memo_id}/convert", headers=_h(t))

    assert r.status_code == 422, r.text
    # Atomicity: no invoice materialized, memo unchanged.
    assert await _target_doc_count(session, cid, "invoice") == before_invoices
    src = await _doc_state(session, cid, memo_id)
    assert src is not None and src.get("doc_type") == "memo"
    assert not src.get("converted_to")


# --- 3. consignment_in -> bill conversion ----------------------------------


@pytest.mark.asyncio
async def test_consignment_in_to_bill_duplicate_is_atomic(client, session):
    """convert_doc on a consignment_in carrying a duplicate non-splittable item
    is rejected 409 and nothing is committed.

    Conversion copies the source line_items verbatim into the bill's doc.created,
    emitted before session.commit(), so the guard fires: no bill projection is
    created and the source consignment is unchanged.
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "CB-NS", allow_splitting=False)
    cons_id = f"doc:{_uuid.uuid4().hex[:12]}"
    await _seed_historical_doc(session, cid, cons_id, {
        "doc_type": "consignment_in", "status": "received",
        "line_items": [_line(item, sku="CB-NS"), _line(item, sku="CB-NS")],
    })
    before_bills = await _target_doc_count(session, cid, "bill")

    r = await client.post(f"/docs/{cons_id}/convert", headers=_h(t))

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "duplicate_document_item"
    # Atomicity: no bill materialized, consignment unchanged and not converted.
    assert await _target_doc_count(session, cid, "bill") == before_bills
    src = await _doc_state(session, cid, cons_id)
    assert src is not None and src.get("doc_type") == "consignment_in"
    assert src.get("status") != "converted" and not src.get("converted_to")


# --- 4. list -> document materialization -----------------------------------


@pytest.mark.asyncio
async def test_list_to_document_duplicate_is_atomic(client, session):
    """convert_list (POST /lists/{id}/convert) on a finalized quotation list
    carrying a duplicate non-splittable item is rejected 409 and nothing is
    committed.

    A list is entity_type == "list", so building it with a duplicate line does
    NOT trip the doc guard. Materialization copies the list's line_items into the
    target doc's doc.created (emitted before session.commit()), and THAT is where
    the guard fires: no invoice is materialized and the list stays finalized
    (its list.closed never emitted).
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "LD-NS", allow_splitting=False)

    r = await client.post("/lists", headers=_h(t), json={
        "list_type": "quotation",
        "line_items": [_line(item, sku="LD-NS"), _line(item, sku="LD-NS")],
    })
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]
    rf = await client.post(f"/lists/{list_id}/finalize", headers=_h(t))
    assert rf.status_code == 200, rf.text
    before_invoices = await _target_doc_count(session, cid, "invoice")

    rc = await client.post(f"/lists/{list_id}/convert", headers=_h(t),
                           json={"target_type": "invoice"})

    assert rc.status_code == 409, rc.text
    assert rc.json()["detail"]["code"] == "duplicate_document_item"
    # Atomicity: no invoice materialized, list still finalized and not closed.
    assert await _target_doc_count(session, cid, "invoice") == before_invoices
    src = await _doc_state(session, cid, list_id)
    assert src is not None and src.get("entity_type", "list")
    assert src.get("status") == "finalized"
    assert not src.get("converted_to") and src.get("result") != "converted"


# --- 5. subscription generation --------------------------------------------


@pytest.mark.asyncio
async def test_subscription_generate_duplicate_is_atomic(client, session):
    """generate_now (POST /subscriptions/{id}/generate) on a subscription
    template carrying a duplicate non-splittable item is rejected 409 and nothing
    is committed.

    The template is itself a doc (doc_type subscription_invoice), so it is seeded
    as a historical doc (creating it via the API would trip the guard). Generation
    copies the template's line_items into the generated invoice's doc.created
    (emitted before session.commit()), so the guard fires: no generated invoice
    exists and the template is unchanged.
    """
    t = await _register(client)
    cid = await _company_id(session)
    item = await _item(client, t, "SG-NS", allow_splitting=False)
    sub_id = f"doc:{_uuid.uuid4().hex[:12]}"
    await _seed_historical_doc(session, cid, sub_id, {
        "doc_type": "subscription_invoice", "status": "active",
        "line_items": [_line(item, sku="SG-NS"), _line(item, sku="SG-NS")],
    })
    before_invoices = await _target_doc_count(session, cid, "invoice")
    before_template = await _doc_state(session, cid, sub_id)

    r = await client.post(f"/subscriptions/{sub_id}/generate", headers=_h(t))

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "duplicate_document_item"
    # Atomicity: no generated invoice, template unchanged (no new generated_doc_ids).
    assert await _target_doc_count(session, cid, "invoice") == before_invoices
    after_template = await _doc_state(session, cid, sub_id)
    assert after_template == before_template


# --- 6. batch import -------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_import_rejects_duplicate_record_survivors_commit(client, session):
    """batch_import_docs (POST /docs/import/batch) rejects the one record that
    carries a duplicate non-splittable item while the valid records survive the
    commit.

    The batch loop catches each record's failure into errors and continues, then
    commits once. The guard raises before emit_event's begin_nested savepoint, so
    the rejected record never corrupts the outer transaction: the valid records
    before and after it are created and persisted, and the bad record produces no
    doc projection. This is the documented batch behavior (good records survive a
    bad one), not all-or-nothing.
    """
    t = await _register(client)
    cid = await _company_id(session)
    good1 = await _item(client, t, "BI-G1", allow_splitting=False)
    bad = await _item(client, t, "BI-BAD", allow_splitting=False)
    good2 = await _item(client, t, "BI-G2", allow_splitting=False)

    ok_id_1 = f"doc:{_uuid.uuid4().hex[:12]}"
    dup_id = f"doc:{_uuid.uuid4().hex[:12]}"
    ok_id_2 = f"doc:{_uuid.uuid4().hex[:12]}"

    def _rec(entity_id, lines):
        return {
            "entity_id": entity_id,
            "event_type": "doc.created",
            "data": {"doc_type": "invoice", "line_items": lines},
            "source": "import",
            "idempotency_key": str(_uuid.uuid4()),
        }

    r = await client.post("/docs/import/batch", headers=_h(t), json={"records": [
        _rec(ok_id_1, [_line(good1, sku="BI-G1")]),
        _rec(dup_id, [_line(bad, sku="BI-BAD"), _line(bad, sku="BI-BAD")]),
        _rec(ok_id_2, [_line(good2, sku="BI-G2")]),
    ]})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2           # the two good records
    assert len(body["errors"]) == 1       # the duplicate record only
    assert dup_id in body["errors"][0]
    # Survivors committed; the duplicate record has no projection.
    assert await _doc_state(session, cid, ok_id_1) is not None
    assert await _doc_state(session, cid, ok_id_2) is not None
    assert await _doc_state(session, cid, dup_id) is None
    assert await _ledger_count(session, cid, dup_id) == 0
