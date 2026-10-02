# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Invoice lifecycle actions that change what an invoice's cost of goods sold is
based on - finalize, fulfill, void, revert to draft, and a cost correction to a lot
the invoice names - finish cleanly when two of them happen at the same moment, and
the books end up as if they had happened one after the other.

These run on real Postgres with independently committed sessions, because the
overlap they describe only exists between two separately committed transactions."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.lot_origin import recognize_opening_lots
from test_memo_lifecycle_concurrency import _barcode, _cleanup, _factory, _race, _seed_company


async def _lot(factory, company_id, user, cost_total: float = 100.0) -> str:
    entity_id = f"item:{uuid.uuid4()}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item",
            event_type="item.created",
            data={"status": "available", "sku": f"CL-{uuid.uuid4().hex[:6]}", "name": "Lot",
                  "quantity": 1, "barcode": _barcode(), "cost_total": cost_total, "sell_by": "piece"},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await recognize_opening_lots(s, company_id, [entity_id], user.id, f"seed:{entity_id}")
        await s.commit()
    return entity_id


async def _finalized_invoice(factory, company_id, user, lot_id: str) -> str:
    from celerp_docs.routes import finalize_doc

    async with factory() as s:
        lot = await s.get(Projection, {"company_id": company_id, "entity_id": lot_id})
        sku = lot.state["sku"]
    doc_id = f"doc:CL-{uuid.uuid4().hex[:8]}"
    line = {"entity_id": lot_id, "sku": sku, "name": "Lot", "quantity": 1, "unit_price": 500.0,
            "sell_by": "piece", "line_total": 500.0}
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "status": "draft", "ref_id": doc_id[4:], "line_items": [line],
                  "subtotal": 500.0, "total": 500.0, "amount_outstanding": 500.0, "currency": "USD"},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    async with factory() as s:
        await finalize_doc(doc_id, company_id=company_id, _=None, user=user, session=s)
    return doc_id


async def _doc_cogs(factory, company_id, doc_id: str) -> float:
    async with factory() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
            Projection.entity_id.like(f"je:auto:{doc_id}:%"),
        ))).scalars().all()
    return round(sum(
        float(e.get("debit") or 0) - float(e.get("credit") or 0)
        for r in rows if (r.state or {}).get("status") == "posted"
        for e in r.state.get("entries", []) if e["account"] == "5100"
    ), 2)


async def _doc_status(factory, company_id, doc_id: str) -> str:
    async with factory() as s:
        return (await s.get(Projection, {"company_id": company_id, "entity_id": doc_id})).state.get("status")


async def _correct_cost(factory, company_id, user, lot_id: str, cost_total: float):
    from celerp_inventory.services import restate_item_cost

    async with factory() as s:
        await restate_item_cost(
            s, company_id, lot_id, event_type="item.cost_adjusted", data={"cost_total": cost_total},
            actor_id=user.id, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()


async def _outcome(coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - the outcome is what the test asserts on
        return exc


@pytest.mark.asyncio
async def test_finalize_sent_again_while_the_invoice_ships_both_finish(_db_engine, monkeypatch):
    from celerp_docs.routes import FulfillLinesRequest, finalize_doc, fulfill_lines
    import celerp_docs.routes as doc_routes

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        lot = await _lot(factory, company_id, user)
        doc = await _finalized_invoice(factory, company_id, user, lot)

        shipping = asyncio.Event()
        original = doc_routes._lock_item_sku_lots

        async def slow_lots(session, cid, item_ids):
            shipping.set()
            await asyncio.sleep(1.5)
            return await original(session, cid, item_ids)

        monkeypatch.setattr(doc_routes, "_lock_item_sku_lots", slow_lots)

        async def ship():
            async with factory() as s:
                return await _outcome(fulfill_lines(
                    doc, FulfillLinesRequest(line_entity_ids=[lot]),
                    company_id=company_id, _=None, user=user, session=s))

        async def finalize_again():
            await asyncio.wait_for(shipping.wait(), timeout=5)
            async with factory() as s:
                return await _outcome(finalize_doc(doc, company_id=company_id, _=None, user=user, session=s))

        shipped, refinalized = await asyncio.wait_for(asyncio.gather(ship(), finalize_again()), timeout=20)
        assert not isinstance(shipped, Exception), shipped
        assert not isinstance(refinalized, Exception) or (
            isinstance(refinalized, HTTPException) and refinalized.status_code in (409, 422)), refinalized
        assert await _doc_cogs(factory, company_id, doc) == 100.0
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("action, step, status", [
    ("void", "void_for_doc_voided", "void"),
    ("revert-to-draft", "void_for_doc_finalized", "draft"),
])
async def test_cost_correction_during_void_or_revert_leaves_no_cogs_behind(_db_engine, monkeypatch, action, step, status):
    from celerp_docs.routes import DocRevertBody, DocVoidBody, revert_doc_to_draft, void_doc

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        lot = await _lot(factory, company_id, user)
        doc = await _finalized_invoice(factory, company_id, user, lot)
        assert await _doc_cogs(factory, company_id, doc) == 100.0

        reversing = asyncio.Event()
        original = getattr(auto_je, step)

        async def slow_reversal(*args, **kwargs):
            result = await original(*args, **kwargs)
            reversing.set()
            await asyncio.sleep(1.5)
            return result

        monkeypatch.setattr(auto_je, step, slow_reversal)

        async def end_invoice():
            async with factory() as s:
                if action == "void":
                    return await _outcome(void_doc(doc, DocVoidBody(), company_id=company_id, _=None,
                                                   user=user, session=s))
                return await _outcome(revert_doc_to_draft(doc, DocRevertBody(), company_id=company_id, _=None,
                                                          user=user, session=s))

        async def correct():
            await asyncio.wait_for(reversing.wait(), timeout=5)
            return await _outcome(_correct_cost(factory, company_id, user, lot, 120.0))

        ended, corrected = await asyncio.wait_for(asyncio.gather(end_invoice(), correct()), timeout=20)
        assert not isinstance(ended, Exception), ended
        assert not isinstance(corrected, Exception), corrected
        assert await _doc_status(factory, company_id, doc) == status
        assert await _doc_cogs(factory, company_id, doc) == 0.0
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_cost_correction_reaches_both_invoices_that_named_the_lot(_db_engine):
    from celerp_docs.routes import FulfillLinesRequest, fulfill_lines

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        lot = await _lot(factory, company_id, user)
        shipped = await _finalized_invoice(factory, company_id, user, lot)
        waiting = await _finalized_invoice(factory, company_id, user, lot)
        async with factory() as s:
            await fulfill_lines(shipped, FulfillLinesRequest(line_entity_ids=[lot]),
                                company_id=company_id, _=None, user=user, session=s)
        await _correct_cost(factory, company_id, user, lot, 120.0)
        assert await _doc_cogs(factory, company_id, shipped) == 120.0
        assert await _doc_cogs(factory, company_id, waiting) == 120.0
    finally:
        await _cleanup(factory, company_id, user_id)
