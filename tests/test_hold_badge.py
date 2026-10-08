# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line that holds less (or more) than its quantity says so.

Reading a document or a List reports how much each line holds, from the holds stamped
with their line. The status badge of a line its record reserves reads "Reserved h of q"
when the two differ, as after a quantity edit, and never a plain "Reserved"."""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from line_actions_support import doc, h, line, line_ids, lot, quotation, state  # noqa: F401

_L0 = "11111111-1111-4111-8111-111111111111"


async def _reserve(client, h, entity_id, ids):
    path = "lists" if entity_id.startswith("list:") else "docs"
    r = await client.post(f"/{path}/{entity_id}/reserve-lines", headers=h,
                          json={"line_ids": ids, "new_status": "reserved"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_document_reports_what_each_line_holds_after_a_quantity_edit(client, h):
    a = await lot(client, h, "HB-1", 10)
    d = await doc(client, h, [line(a, 4, sku="HB-1"), line(None, 1, sku="HB-FREE")])
    l0, l1 = await line_ids(client, h, d)
    await _reserve(client, h, d, [l0])
    assert (await client.post(f"/docs/{d}/revert-to-draft", headers=h, json={})).status_code == 200
    lines = [dict(li) for li in (await state(client, h, d))["line_items"]]
    lines[0]["quantity"] = 6
    r = await client.patch(f"/docs/{d}", headers=h, json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text
    got = await state(client, h, d)
    assert got["line_holds"] == {l0: 4.0}
    assert l1 not in got["line_holds"]
    # The holds are reported beside the lines, never stored on them.
    assert all("line_holds" not in li and "held_quantity" not in li for li in got["line_items"])


@pytest.mark.asyncio
async def test_a_list_page_reports_what_each_line_holds(client, h):
    a = await lot(client, h, "HB-2", 3)
    q = await quotation(client, h, [line(a, 3, sku="HB-2")])
    (l0,) = await line_ids(client, h, q)
    await _reserve(client, h, q, [l0])
    r = await client.get(f"/lists/{q}/page", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["line_holds"] == {l0: 3.0}


def _render(status: str, qty: float, holds: dict | None, held_by: str = "doc:inv-1"):
    from ui.routes.documents import _doc_detail
    d = {"entity_id": "doc:inv-1", "doc_type": "invoice", "status": status, "ref_id": "I-1",
         "line_items": [{"line_id": _L0, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1",
                         "quantity": qty, "unit_price": 1}]}
    if holds is not None:
        d["line_holds"] = holds
    return to_xml(_doc_detail(d, item_status_map={"item:1": "reserved"},
                              item_status_doc_map={"item:1": (held_by, "X-1")}))


@pytest.mark.parametrize("status", ["draft", "sent"])
def test_a_line_holding_less_than_its_quantity_shows_how_much(status):
    html = _render(status, 6, {_L0: 4})
    assert "Reserved 4 of 6" in html


@pytest.mark.parametrize("status", ["draft", "sent"])
def test_a_line_holding_its_quantity_shows_reserved(status):
    html = _render(status, 4, {_L0: 4})
    assert "Reserved 4 of" not in html and ">Reserved<" in html


def test_a_line_whose_item_another_record_reserves_shows_no_count():
    assert " of 6" not in _render("sent", 6, {}, held_by="doc:other")


def test_the_badge_reports_a_mismatch_either_way():
    from ui.routes.documents import _item_status_badge_cell
    assert "Reserved 6 of 2" in to_xml(_item_status_badge_cell("reserved", "item:1", held=(6, 2)))
    assert "Reserved 0 of 2" in to_xml(_item_status_badge_cell("reserved", "item:1", held=(0, 2)))
