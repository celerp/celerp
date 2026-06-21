# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Airtight item-history coverage for split / transform / merge.

Backend half: the right ledger events are emitted on BOTH the mother and the child
(origin markers), with the enriched payloads the history UI needs.
Render half: activity_table renders the requested linked-SKU style, qty/pcs/wt deltas,
proper money/quantity formatting, anchors, and context-aware child-origin suppression.
"""

from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from ui.components.activity import (
    activity_table,
    detail_from_entry,
    fmt_price,
    fmt_qty,
    _origin_detail,
    _qpw_delta,
)


async def _token(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


async def _seed(client, headers, **over) -> str:
    body = {"sku": "MUM", "name": "Mother", "quantity": 10.0, "sell_by": "piece",
            "cost_price": 100.0, "allow_splitting": True}
    body.update(over)
    r = await client.post("/items", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _events(client, headers, entity_id, event_type=None):
    params = {"entity_id": entity_id, "limit": 200}
    if event_type:
        params["event_type"] = event_type
    r = await client.get("/ledger", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --------------------------------------------------------------------------- #
# Backend: emission
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_split_emits_child_origin_event(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0)
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 3.0}]}, headers=h)
    assert r.status_code == 200, r.text
    child_id = r.json()["children"][0]["id"]

    origin = [e for e in await _events(client, h, child_id) if e["event_type"] == "item.split_from"]
    assert len(origin) == 1
    d = origin[0]["data"]
    assert d["parent_id"] == parent_id
    assert d["parent_sku"] == "MUM"
    assert d["qty"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_split_summary_carries_mother_deltas(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0)
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 3.0},
                                             {"sku": "MUM.2", "quantity": 4.0}]}, headers=h)
    assert r.status_code == 200, r.text
    child_ids = [c["id"] for c in r.json()["children"]]

    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"]
    assert len(split) == 1
    d = split[0]["data"]
    assert d["parent_sku"] == "MUM"
    cd = d["children_detail"]
    assert len(cd) == 2
    # Sequential mother deltas: 10 -> 7 -> 3.
    assert (cd[0]["qty_before"], cd[0]["qty_after"]) == (pytest.approx(10.0), pytest.approx(7.0))
    assert (cd[1]["qty_before"], cd[1]["qty_after"]) == (pytest.approx(7.0), pytest.approx(3.0))
    # Each child descriptor deep-links to that child's origin event.
    assert {c["child_id"] for c in cd} == set(child_ids)
    for c in cd:
        assert isinstance(c["origin_event_id"], int)


@pytest.mark.asyncio
async def test_split_summary_carries_cost_before_after(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, cost_price=100.0)  # cost_total = 1000
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 4.0}]}, headers=h)
    assert r.status_code == 200, r.text
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"][0]
    c0 = split["data"]["children_detail"][0]
    # 4 of 10 units carved off: parent cost 1000 -> 600.
    assert c0["cost_before"] == pytest.approx(1000.0)
    assert c0["cost_after"] == pytest.approx(600.0)


@pytest.mark.asyncio
async def test_split_parent_mechanical_rows_collapse(client):
    # The split summary is the single coherent parent entry; mechanical per-field rows
    # (quantity.adjusted / cost pricing.set / weight-pieces updated) are suppressed in the feed.
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, cost_price=100.0)
    await client.post(f"/items/{parent_id}/split",
                      json={"children": [{"sku": "MUM.1", "quantity": 4.0}]}, headers=h)
    ledger = await _events(client, h, parent_id)
    html = to_xml(activity_table(ledger, subject_entity_id=parent_id, currency="USD"))
    assert "Item Split - MUM" in html
    assert "Qty: 10 → 6" in html and "Cost: $1,000.00 → $600.00" in html
    # The bare "Qty → 6" mechanical row must not appear (collapsed into the summary).
    assert "Qty → 6" not in html


@pytest.mark.asyncio
async def test_split_pieces_omitted_when_unused(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    # Non-pieces item: pieces fields must be absent from the descriptor.
    parent_id = await _seed(client, h, quantity=10.0)
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 3.0}]}, headers=h)
    assert r.status_code == 200
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"][0]
    c0 = split["data"]["children_detail"][0]
    assert "pieces_before" not in c0 and "pieces_after" not in c0


@pytest.mark.asyncio
async def test_split_pieces_tracked_when_used(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, sell_by="carat", attributes={"pieces": 8})
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 3.0, "weight": 3.0, "pieces": 5}]},
                          headers=h)
    assert r.status_code == 200, r.text
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"][0]
    c0 = split["data"]["children_detail"][0]
    assert (c0["pieces_before"], c0["pieces_after"]) == (8, 3)


@pytest.mark.asyncio
async def test_transform_emits_child_origin_event(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0)
    r = await client.post(f"/items/{parent_id}/transform",
                          json={"child_sku": "CUT-1", "child_category": "Cut", "child_sell_by": "carat",
                                "child_quantity": 8.0, "child_cost_total": 900.0}, headers=h)
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]

    origin = [e for e in await _events(client, h, child_id) if e["event_type"] == "item.transformed_from"]
    assert len(origin) == 1
    d = origin[0]["data"]
    assert d["parent_id"] == parent_id
    assert d["parent_sku"] == "MUM"
    assert d["qty"] == pytest.approx(8.0)
    assert d["category"] == "Cut"


@pytest.mark.asyncio
async def test_transform_summary_carries_mother_deltas(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0)
    r = await client.post(f"/items/{parent_id}/transform",
                          json={"child_sku": "CUT-1", "child_category": "Cut", "child_sell_by": "carat",
                                "child_quantity": 8.0, "child_cost_total": 900.0}, headers=h)
    assert r.status_code == 200
    tr = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.transform"][0]
    d = tr["data"]
    assert d["parent_sku"] == "MUM"
    assert d["qty_before"] == pytest.approx(10.0)
    assert d["qty_after"] == 0
    assert isinstance(d["child_origin_event_id"], int)


@pytest.mark.asyncio
async def test_transform_archives_and_logs_mother(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0)
    await client.post(f"/items/{parent_id}/transform",
                      json={"child_sku": "CUT-1", "child_category": "Cut", "child_sell_by": "carat",
                            "child_quantity": 8.0, "child_cost_total": 900.0}, headers=h)
    types = {e["event_type"] for e in await _events(client, h, parent_id)}
    assert "item.status.set" in types and "item.transform" in types
    item = (await client.get(f"/items/{parent_id}", headers=h)).json()
    assert item["status"] == "archived"


@pytest.mark.asyncio
async def test_merge_denormalizes_skus(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a = await _seed(client, h, sku="A", quantity=5.0, category="Raw")
    b = await _seed(client, h, sku="B", quantity=3.0, category="Raw")
    r = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a},
                          headers=h)
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    merged = [e for e in await _events(client, h, new_id) if e["event_type"] == "item.merged"][0]
    assert merged["data"]["source_skus"]
    assert merged["data"]["resulting_qty"] == pytest.approx(8.0)
    src = [e for e in await _events(client, h, a) if e["event_type"] == "item.source_deactivated"][0]
    assert src["data"]["merged_into_sku"]
    assert src["data"]["original_qty"] is not None


@pytest.mark.asyncio
async def test_transfer_records_from_to_with_names(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    loc1 = (await client.post("/companies/me/locations", json={"name": "Vault", "type": "warehouse"}, headers=h)).json()
    loc2 = (await client.post("/companies/me/locations", json={"name": "Showroom", "type": "warehouse"}, headers=h)).json()
    item_id = (await client.post("/items", json={"sku": "TR-1", "name": "x", "quantity": 1.0,
                                                 "sell_by": "piece", "location_id": loc1["id"]}, headers=h)).json()["id"]
    await client.post(f"/items/{item_id}/transfer", json={"to_location_id": loc2["id"]}, headers=h)

    ev = [e for e in await _events(client, h, item_id) if e["event_type"] == "item.transferred"][0]
    d = ev["data"]
    assert d["from_location_id"] == loc1["id"] and d["from_location_name"] == "Vault"
    assert d["to_location_id"] == loc2["id"] and d["to_location_name"] == "Showroom"
    # Renders as "from -> to".
    assert detail_from_entry(d, "item.transferred") == "Vault → Showroom"


@pytest.mark.asyncio
async def test_bulk_transfer_records_from_to(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    loc1 = (await client.post("/companies/me/locations", json={"name": "A", "type": "warehouse"}, headers=h)).json()
    loc2 = (await client.post("/companies/me/locations", json={"name": "B", "type": "warehouse"}, headers=h)).json()
    i1 = (await client.post("/items", json={"sku": "BT1", "name": "x", "quantity": 1.0, "sell_by": "piece", "location_id": loc1["id"]}, headers=h)).json()["id"]
    await client.post("/items/bulk/transfer", json={"entity_ids": [i1], "to_location_id": loc2["id"]}, headers=h)
    ev = [e for e in await _events(client, h, i1) if e["event_type"] == "item.transferred"][0]
    assert ev["data"]["from_location_name"] == "A" and ev["data"]["to_location_name"] == "B"


def test_transfer_detail_degrades_gracefully():
    # No source (e.g. item had no location) -> "-> to".
    assert detail_from_entry({"to_location_name": "B"}, "item.transferred") == "→ B"
    # Legacy event (only raw to_location_id) still renders something.
    assert detail_from_entry({"to_location_id": "abc"}, "item.transferred") == "→ abc"


# --------------------------------------------------------------------------- #
# Render: helpers (pure, no server)
# --------------------------------------------------------------------------- #

def test_cost_redaction_helper():
    from celerp.services.activity_redaction import redact_event_costs, can_see_costs
    assert can_see_costs("manager") and can_see_costs("admin") and can_see_costs("owner")
    assert not can_see_costs("operator") and not can_see_costs("viewer") and not can_see_costs(None)
    # cost pricing → amount stripped, marker set
    assert redact_event_costs("item.pricing.set", {"price_type": "cost_total", "new_price": 5.0}) \
        == {"price_type": "cost_total", "cost_redacted": True}
    # sell price → untouched
    assert redact_event_costs("item.pricing.set", {"price_type": "retail_price", "new_price": 9.0}) \
        == {"price_type": "retail_price", "new_price": 9.0}
    # fields_changed cost stripped, other fields kept
    assert redact_event_costs("item.updated", {"fields_changed": {"cost_total": {"old": 1, "new": 2}, "name": {"old": "a", "new": "b"}}}) \
        == {"fields_changed": {"name": {"old": "a", "new": "b"}}}
    # transform cost totals removed, non-cost kept
    assert redact_event_costs("item.transform", {"child_sku": "C", "parent_cost_total": 10, "child_cost_total": 9, "qty_before": 5}) \
        == {"child_sku": "C", "qty_before": 5}
    # split children_detail cost deltas removed
    out = redact_event_costs("item.split", {"parent_sku": "M", "children_detail": [{"child_sku": "C", "qty_before": 10, "qty_after": 7, "cost_before": 100, "cost_after": 70}]})
    assert out["children_detail"][0] == {"child_sku": "C", "qty_before": 10, "qty_after": 7}


def test_field_change_formatting():
    from ui.components.activity import _fields_changed_summary
    # pieces render as clean integers (10 -> 2, not 10 -> 2.0)
    assert _fields_changed_summary({"pieces": {"old": 10, "new": 2.0}}) == "Pieces: 10 → 2"
    # quantity trims trailing zeros
    assert _fields_changed_summary({"quantity": {"old": 5.0, "new": 3.5}}) == "Quantity: 5 → 3.5"
    # money is currency-formatted
    assert _fields_changed_summary({"total": {"old": 100, "new": 120}}, "USD") == "Total: $100.00 → $120.00"
    # dates render as dates, not datetimes
    assert _fields_changed_summary({"due_date": {"old": "2026-01-01T00:00:00+00:00", "new": "2026-02-01T00:00:00+00:00"}}) \
        == "Due date: 2026-01-01 → 2026-02-01"


def test_fmt_qty_trims_decimals():
    assert fmt_qty(8.0) == "8"
    assert fmt_qty(7.50) == "7.5"
    assert fmt_qty(10) == "10"
    assert fmt_qty(0) == "0"
    assert fmt_qty(None) == ""


def test_fmt_price_currency_vs_rate():
    assert fmt_price(1234.5, "cost_total", "USD") == "$1,234.50"   # total at currency precision
    assert fmt_price(15.285, "retail_price", "USD") == "$15.285"   # rate trims to significant


def test_pricing_detail_formats_money():
    assert detail_from_entry({"price_type": "cost_total", "new_price": 1200.0},
                             "item.pricing.set", "USD") == "Cost Total → $1,200.00"


def test_qpw_delta_only_changed_fields():
    assert _qpw_delta({"qty_before": 10, "qty_after": 4, "pieces_before": 8, "pieces_after": 5}) \
        == "Qty: 10 → 4, Pcs: 8 → 5"
    # Weight present, pieces absent -> no Pcs segment.
    assert _qpw_delta({"qty_before": 10.0, "qty_after": 7.5, "weight_before": 20.0, "weight_after": 12.5}) \
        == "Qty: 10 → 7.5, Wt: 20 → 12.5"


def test_origin_detail_zero_to_received():
    assert _origin_detail({"qty": 8.0, "pieces": 5}) == "Qty: 0 → 8, Pcs: 0 → 5"
    assert _origin_detail({"qty": 8.0, "category": "Cut"}, with_category=True) == "Qty: 0 → 8, Cat: Cut"


def _split_event(rid=100):
    return {"id": rid, "event_type": "item.split", "entity_id": "item:mum",
            "ts": "2026-06-21T10:00:00+00:00",
            "data": {"child_ids": ["item:c1", "item:c2"], "child_skus": ["MUM.1", "MUM.2"],
                     "quantities": [3, 3], "parent_sku": "MUM", "children_detail": [
                         {"child_id": "item:c1", "child_sku": "MUM.1", "origin_event_id": 50,
                          "qty_before": 10, "qty_after": 7},
                         {"child_id": "item:c2", "child_sku": "MUM.2", "origin_event_id": 60,
                          "qty_before": 7, "qty_after": 4}]}}


def test_split_renders_one_row_per_child_with_links_and_anchors():
    html = to_xml(activity_table([_split_event()], subject_entity_id="item:mum"))
    assert "Item Split - MUM" in html
    assert '/inventory/item:c1#evt-50' in html
    assert '/inventory/item:c2#evt-60' in html
    assert 'id="evt-100-0"' in html and 'id="evt-100-1"' in html
    assert "Qty: 10 → 7" in html and "Qty: 7 → 4" in html


def test_split_from_links_parent_on_child_page():
    origin = {"id": 50, "event_type": "item.split_from", "entity_id": "item:c1",
              "ts": "2026-06-21T10:00:00+00:00",
              "data": {"parent_id": "item:mum", "parent_sku": "MUM", "qty": 3, "pieces": 3}}
    created = {"id": 49, "event_type": "item.created", "entity_id": "item:c1",
               "ts": "2026-06-21T10:00:00+00:00", "data": {"sku": "MUM.1", "name": "x"},
               "metadata": {"parent_id": "item:mum"}}
    html = to_xml(activity_table([origin, created], subject_entity_id="item:c1"))
    assert "Split from" in html
    assert "/inventory/item:mum" in html
    assert "Item added" not in html            # child item.created stays suppressed


def test_child_origin_deduped_on_dashboard():
    origin = {"id": 50, "event_type": "item.split_from", "entity_id": "item:c1",
              "ts": "2026-06-21T10:00:00+00:00",
              "data": {"parent_id": "item:mum", "parent_sku": "MUM", "qty": 3}}
    html = to_xml(activity_table([origin], subject_entity_id=None))
    assert "Split from" not in html


def test_transform_row_links_child():
    ev = {"id": 200, "event_type": "item.transform", "entity_id": "item:mum",
          "ts": "2026-06-21T10:00:00+00:00",
          "data": {"child_id": "item:c1", "child_sku": "CUT-1", "child_category": "Cut",
                   "parent_cost_total": 100.0, "child_cost_total": 90.0, "parent_sku": "MUM",
                   "child_origin_event_id": 51, "qty_before": 10, "qty_after": 0}}
    html = to_xml(activity_table([ev], subject_entity_id="item:mum"))
    assert "Item Transform - MUM" in html
    assert "/inventory/item:c1#evt-51" in html
    assert "Qty: 10 → 0" in html
