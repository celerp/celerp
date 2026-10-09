# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An invoice finalizing on a lot while the lot is adjusted to nothing: whichever goes
first, the other sees it, so the lot's cost leaves inventory once, as cost of sales or
as shrinkage, never as both."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services import auto_je

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "default_modules" / "celerp-docs" / "tests"))
from test_cogs_lifecycle_concurrency import _lot, _outcome, emit_event
from test_memo_lifecycle_concurrency import _cleanup, _factory, _seed_company


async def _net(factory, company_id, account):
    async with factory() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).scalars().all()
    return round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
                     for r in rows if (r.state or {}).get("status") == "posted"
                     for e in r.state.get("entries", []) if e["account"] == account), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", ["adjuster", "finalizer"])
async def test_adjust_racing_finalize_gives_up_cost_once(_db_engine, monkeypatch, slow):
    from celerp_docs.routes import finalize_doc
    from celerp_inventory.routes import AdjustBody, adjust_item

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        lot = await _lot(factory, company_id, user)
        async with factory() as s:
            sku = (await s.get(Projection, {"company_id": company_id, "entity_id": lot})).state["sku"]
        a = f"doc:CL-{uuid.uuid4().hex[:8]}"
        line = {"entity_id": lot, "sku": sku, "name": "Lot", "quantity": 1, "unit_price": 500.0,
                "sell_by": "piece", "line_total": 500.0}
        async with factory() as s:
            await emit_event(s, company_id=company_id, entity_id=a, entity_type="doc", event_type="doc.created",
                             data={"doc_type": "invoice", "status": "draft", "ref_id": a[4:], "line_items": [line],
                                   "subtotal": 500.0, "total": 500.0, "amount_outstanding": 500.0, "currency": "USD"},
                             actor_id=user.id, location_id=None, source="test",
                             idempotency_key=str(uuid.uuid4()), metadata_={})
            await s.commit()
        gate = asyncio.Event()
        if slow == "adjuster":
            original = auto_je.refuse_taking_set_aside

            async def slow_guard(*args, **kwargs):
                result = await original(*args, **kwargs)
                gate.set()
                await asyncio.sleep(1.5)
                return result
            monkeypatch.setattr(auto_je, "refuse_taking_set_aside", slow_guard)
        else:
            original = auto_je.compute_doc_cogs

            async def slow_cogs(*args, **kwargs):
                result = await original(*args, **kwargs)
                gate.set()
                await asyncio.sleep(1.5)
                return result
            monkeypatch.setattr(auto_je, "compute_doc_cogs", slow_cogs)

        async def adjust():
            if slow == "finalizer":
                await asyncio.wait_for(gate.wait(), timeout=5)
            async with factory() as s:
                return await _outcome(adjust_item(lot, AdjustBody(new_qty=0), company_id=company_id, _=None,
                                                  user=user, session=s))

        async def finalize():
            if slow == "adjuster":
                await asyncio.wait_for(gate.wait(), timeout=5)
            async with factory() as s:
                return await _outcome(finalize_doc(a, company_id=company_id, _=None, user=user, session=s))

        adjusted, finalized = await asyncio.wait_for(asyncio.gather(adjust(), finalize()), timeout=30)
        cogs, writeoff = await _net(factory, company_id, "5100"), await _net(factory, company_id, "6970")
        async with factory() as s:
            qty = (await s.get(Projection, {"company_id": company_id, "entity_id": lot})).state.get("quantity")
        assert round(cogs + writeoff, 2) == 100.0, (adjusted, finalized, cogs, writeoff, qty)
    finally:
        await _cleanup(factory, company_id, user_id)
