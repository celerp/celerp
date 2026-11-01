# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Two scan batches submitted to one document at the same moment both land: a splittable item
in both is one line of two, and a non-splittable item in both lands once. Each batch runs on its own committed session,
so a lost update or a doubled line would show in the final committed state; the doc
row lock taken before the read is what serializes them."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from celerp.events.engine import emit_event
from test_helpers import remove_company
from test_list_outcome_concurrency import _barcode, _factory, _seed_company, _state


async def _seed_unique_item(factory, company_id, user, *, sku, barcode, splitting, quantity=1,
                            sell_by="piece") -> str:
    from celerp.services.lot_origin import recognize_opening_lots

    entity_id = f"item:{uuid.uuid4()}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.created",
            data={"status": "available", "sku": sku, "name": sku, "quantity": quantity, "sell_by": sell_by,
                  "barcode": barcode, "cost_price": 1, "allow_splitting": splitting},
            actor_id=user.id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await recognize_opening_lots(s, company_id, [entity_id], user.id, f"seed:{entity_id}")
        await s.commit()
    return entity_id


async def _seed_draft_doc(factory, company_id, user, doc_type) -> str:
    entity_id = f"doc:{doc_type}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.created",
            data={"doc_type": doc_type, "status": "draft", "ref_id": entity_id[4:], "line_items": []},
            actor_id=user.id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _race(factory, company_id, user, doc_id, batches: dict[str, str]) -> dict:
    """Run every batch at once on its own session; return each outcome or its exception."""
    from celerp_docs.routes import ScanBody, scan_doc

    outcome: dict = {}

    async def _one(run_key, barcode):
        async with factory() as session:
            try:
                outcome[run_key] = await scan_doc(
                    doc_id, ScanBody(barcode=barcode, run_key=run_key), company_id=company_id, _=None,
                    role="owner", settings={}, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the outcome assertions
                outcome[run_key] = exc
                await session.rollback()

    await asyncio.wait_for(asyncio.gather(*(_one(k, b) for k, b in batches.items())), timeout=20)
    return outcome


async def _both_survive(_db_engine, doc_type):
    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc_x, bc_y = _barcode(), _barcode()
        x = await _seed_unique_item(factory, company_id, user, sku="X", barcode=bc_x, splitting=True)
        y = await _seed_unique_item(factory, company_id, user, sku="Y", barcode=bc_y, splitting=True)
        doc = await _seed_draft_doc(factory, company_id, user, doc_type)

        outcome = await _race(factory, company_id, user, doc, {"kX": bc_x, "kY": bc_y})

        for key in ("kX", "kY"):
            assert not isinstance(outcome[key], Exception), f"{key} raised: {outcome[key]!r}"
            assert outcome[key]["scanned"] == 1, outcome[key]
        state = await _state(factory, company_id, doc)
        assert {line.get("entity_id") or line.get("item_id") for line in state["line_items"]} == {x, y}
        assert len(state["line_items"]) == 2
        assert {r.get("key") for r in state.get("scan_runs") or []} == {"kX", "kY"}
    finally:
        await remove_company(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_concurrent_invoice_scans_both_survive(_db_engine):
    await _both_survive(_db_engine, "invoice")


@pytest.mark.asyncio
async def test_concurrent_memo_scans_both_survive(_db_engine):
    await _both_survive(_db_engine, "memo")


@pytest.mark.asyncio
async def test_concurrent_same_unique_item_lands_once(_db_engine):
    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc = _barcode()
        item = await _seed_unique_item(factory, company_id, user, sku="U", barcode=bc, splitting=False)
        doc = await _seed_draft_doc(factory, company_id, user, "invoice")

        outcome = await _race(factory, company_id, user, doc, {"k1": bc, "k2": bc})

        for key, result in outcome.items():
            assert not isinstance(result, Exception) or isinstance(result, HTTPException), \
                f"{key} raised: {result!r}"
        landed = [r for r in outcome.values() if isinstance(r, dict) and r["scanned"] == 1]
        assert len(landed) == 1, outcome
        state = await _state(factory, company_id, doc)
        assert [line.get("entity_id") or line.get("item_id") for line in state["line_items"]] == [item]
    finally:
        await remove_company(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_concurrent_same_lot_lands_one_full_line(_db_engine):
    """A non-splittable 5-unit lot in two concurrent batches lands as one line of 5; the batch that
    reads it under the row lock second reports it as already on the document."""
    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc = _barcode()
        item = await _seed_unique_item(factory, company_id, user, sku="S5", barcode=bc, splitting=False, quantity=5)
        doc = await _seed_draft_doc(factory, company_id, user, "invoice")

        outcome = await _race(factory, company_id, user, doc, {"k1": bc, "k2": bc})

        assert sorted(r["scanned"] for r in outcome.values()) == [0, 1], outcome
        repeat = next(r for r in outcome.values() if r["scanned"] == 0)
        assert [f["reason"] for f in repeat["failed"]] == ["duplicate_scan"], repeat
        assert "already on this document" in repeat["failed"][0]["label"]
        state = await _state(factory, company_id, doc)
        assert [line.get("item_id") for line in state["line_items"]] == [item]
        assert float(state["line_items"][0]["quantity"]) == 5.0
    finally:
        await remove_company(factory, company_id, user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("doc_type", "on_hand"), [("purchase_order", 0), ("purchase_order", 5), ("invoice", 5)])
async def test_concurrent_scans_of_a_splittable_lot_are_one_line_of_two(_db_engine, doc_type, on_hand):
    """A repeat raises the line: two concurrent batches scanning one splittable lot leave exactly
    one line of two, neither scan lost."""
    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc = _barcode()
        item = await _seed_unique_item(factory, company_id, user, sku="Z0", barcode=bc, splitting=True, quantity=on_hand)
        doc = await _seed_draft_doc(factory, company_id, user, doc_type)

        outcome = await _race(factory, company_id, user, doc, {"k1": bc, "k2": bc})

        assert [r["scanned"] for r in outcome.values()] == [1, 1], outcome
        state = await _state(factory, company_id, doc)
        assert [line.get("item_id") for line in state["line_items"]] == [item]
        assert [float(line["quantity"]) for line in state["line_items"]] == [2.0]
    finally:
        await remove_company(factory, company_id, user_id)
