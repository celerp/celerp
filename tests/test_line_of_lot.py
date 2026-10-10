# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which document line a shipped lot belongs to.

A shipment records the line it went out on. That record is trusted first while the line
is still on the document: by its line id, then by its recorded position when that line
still fits the lot. Without a usable record the lot belongs to the only line that binds
it, then to the only line of its product. Anything else is unknown, never a guess."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from line_actions_support import doc, h, line, lot, state  # noqa: F401

L1 = "11111111-1111-4111-8111-111111111111"
L2 = "22222222-2222-4222-8222-222222222222"
L3 = "33333333-3333-4333-8333-333333333333"


def _lines(*specs):
    return [{"item_id": eid, "sku": sku, "quantity": 1, "line_id": lid} for eid, sku, lid in specs]


def _of(lines, lot_id="item:a", sku="S", recorded=None, source_line_id=None):
    from celerp.services.auto_je import line_of_lot
    return line_of_lot(lines, lot_id, {"sku": sku}, recorded, source_line_id)


def test_recorded_position_beats_the_first_line_binding_the_lot():
    lines = _lines(("item:a", "S", L1), ("item:a", "S", L2))
    assert _of(lines, recorded=1) == 1


def test_recorded_line_id_beats_the_recorded_position():
    lines = _lines(("item:a", "S", L1), ("item:a", "S", L2))
    assert _of(lines, recorded=0, source_line_id=L2) == 1


def test_recorded_line_id_no_longer_on_the_document_is_ignored():
    lines = _lines(("item:a", "S", L1), ("item:b", "S", L2))
    assert _of(lines, recorded=1, source_line_id=L3) == 1
    assert _of(lines, source_line_id=L3) == 0


def test_recorded_position_that_no_longer_fits_the_lot_is_ignored():
    lines = _lines(("item:a", "S", L1), ("item:z", "OTHER", L2))
    assert _of(lines, recorded=1) == 0
    assert _of(lines, recorded=7) == 0


def test_two_lines_binding_the_lot_without_a_record_is_unknown():
    lines = _lines(("item:a", "S", L1), ("item:a", "S", L2))
    assert _of(lines) is None


def test_only_line_of_the_product_takes_an_unbound_lot():
    lines = _lines(("item:x", "S", L1), ("item:y", "OTHER", L2))
    assert _of(lines, lot_id="item:sibling") == 0
    two = _lines(("item:x", "S", L1), ("item:y", "S", L2))
    assert _of(two, lot_id="item:sibling") is None


def test_recorded_line_reads_id_and_position():
    from celerp.services.auto_je import recorded_line

    class _E:
        metadata_ = {"line_index": 2, "source_line_id": L1}
    assert recorded_line(_E()) == (L1, 2)
    _E.metadata_ = {"line_index": True}
    assert recorded_line(_E()) == (None, None)


@pytest.mark.asyncio
async def test_shipment_records_the_line_id(client, session, h):
    from celerp.models.ledger import LedgerEntry
    a = await lot(client, h, "LOL-1", 10)
    d = await doc(client, h, [line(None, 1, sku="Fee"), line(a, 10, sku="LOL-1")])
    lid = (await state(client, h, d))["line_items"][1]["line_id"]
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})
    assert r.status_code == 200, r.text
    shipped = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.event_type == "item.fulfilled",
        LedgerEntry.data["source_doc_id"].as_string() == d))).scalars().all()
    assert shipped and all(e.metadata_.get("source_line_id") == lid for e in shipped)
    assert all(e.metadata_.get("line_index") == 1 for e in shipped)
