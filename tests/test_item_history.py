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


async def _item(client, headers, entity_id) -> dict:
    r = await client.get(f"/items/{entity_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _pieces(state: dict):
    """pieces, read the way the app does: top-level (as the GET endpoint promotes
    it) then attributes."""
    v = state.get("pieces")
    if v is None:
        v = (state.get("attributes") or {}).get("pieces")
    return v


# --------------------------------------------------------------------------- #
# Issue #223: pieces are partitioned across a split, never duplicated
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_split_weight_parcel_no_child_pieces_clears_instead_of_duplicating(client):
    """The reported bug: a weight-sold parcel with pieces, split without stating
    per-child pieces, duplicated the count onto both mother and child. It must
    now be cleared (unknown) on both, never fabricated."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    # 5 kg / 25 pieces, weight-sold, splitting off 1 kg with pieces left blank.
    parent_id = await _seed(client, h, quantity=5.0, sell_by="carat", attributes={"pieces": 25})
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 1.0, "weight": 1.0}]}, headers=h)
    assert r.status_code == 200, r.text
    child_id = r.json()["children"][0]["id"]

    mother = await _item(client, h, parent_id)
    child = await _item(client, h, child_id)
    # Neither carries a fabricated count (the bug produced 25 on each).
    assert _pieces(mother) is None, f"mother pieces should be cleared, got {_pieces(mother)}"
    assert _pieces(child) is None, f"child must not inherit the mother's pieces, got {_pieces(child)}"


@pytest.mark.asyncio
async def test_split_weight_parcel_with_child_pieces_conserves(client):
    """When per-child pieces ARE given, conservation holds: mother = parent - Σ children."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=5.0, sell_by="carat", attributes={"pieces": 25})
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 2.0, "weight": 2.0, "pieces": 10}]},
                          headers=h)
    assert r.status_code == 200, r.text
    child_id = r.json()["children"][0]["id"]

    assert _pieces(await _item(client, h, child_id)) == 10
    assert _pieces(await _item(client, h, parent_id)) == 15  # 25 - 10, no duplication


@pytest.mark.asyncio
async def test_split_piece_unit_pieces_track_quantity(client):
    """For a piece-unit item, pieces track quantity: children get their own
    quantity, the mother keeps what remains - never the full inherited count."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    # sell_by 'piece' with a pieces attribute equal to quantity.
    parent_id = await _seed(client, h, quantity=10.0, sell_by="piece", attributes={"pieces": 10})
    r = await client.post(f"/items/{parent_id}/split",
                          json={"children": [{"sku": "MUM.1", "quantity": 3.0}]}, headers=h)
    assert r.status_code == 200, r.text
    child_id = r.json()["children"][0]["id"]

    assert _pieces(await _item(client, h, child_id)) == 3
    assert _pieces(await _item(client, h, parent_id)) == 7  # 10 - 3


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


def test_activity_table_full_width_css():
    # Issue 1: the activity/recent-activity feed must fill its container, not shrink to
    # content width. (a) the table carries the class the CSS targets; (b) the rule exists
    # and wins the cascade (declared after .data-table's width:max-content).
    import pathlib, re
    evs = [{"id": 1, "event_type": "item.created", "entity_id": "item:x",
            "ts": "2026-06-21T10:00:00+00:00", "data": {"sku": "S", "name": "n"}}]
    assert "activity-table" in to_xml(activity_table(evs))
    css = pathlib.Path("ui/static/app.css").read_text()
    base = css.index("width: max-content")
    rule = re.search(r"\.activity-table\s*\{[^}]*width:\s*100%", css)
    assert rule and rule.start() > base, "activity-table width:100% must follow data-table max-content"


def test_activity_table_resizable_opt_in():
    # H10: resizable=True wires the shared col-resize script + fixed layout; default keeps
    # the compact auto-layout (so dashboard/contacts/doc widgets are unaffected).
    evs = [{"id": 1, "event_type": "item.created", "entity_id": "item:x",
            "ts": "2026-06-21T10:00:00+00:00", "data": {"sku": "S", "name": "n"}}]
    on = to_xml(activity_table(evs, resizable=True))
    assert "col-resize-handle" in on and "table-layout:fixed" in on and "celerp_activity_wpct_v1" in on
    assert 'class="col-event"' in on
    off = to_xml(activity_table(evs))
    assert "col-resize-handle" not in off and "table-layout:fixed" not in off


def test_activity_table_see_all_footer_link():
    # H7: when more events exist than are shown, the footer links to the full-history page.
    evs = [{"id": i, "event_type": "item.created", "entity_id": "item:x",
            "ts": "2026-06-21T10:00:00+00:00", "data": {"sku": "S", "name": "n"}} for i in range(5)]
    html = to_xml(activity_table(evs, max_display=2, history_url="/inventory/item:x/history"))
    assert "/inventory/item:x/history" in html and "Showing last" in html


def test_doc_tied_item_events_link_doc_number():
    # Issue 2: sold/consigned/fulfilled/reversed events show a linkable doc number.
    def _ev(eid, etype, data):
        return {"id": eid, "event_type": etype, "entity_id": "item:x",
                "ts": "2026-06-21T10:00:00+00:00", "data": data}
    sold = _ev(1, "item.fulfilled", {"source_doc_id": "doc:INV-2606-0001",
               "doc_number": "INV-2606-0001", "quantity_fulfilled": 2, "doc_type": "invoice"})
    html = to_xml(activity_table([sold], subject_entity_id="item:x"))
    assert "Sold" in html and "INV-2606-0001" in html
    assert "/docs/doc:INV-2606-0001" in html and "Qty: 2" in html
    # memo -> consigned verb
    cons = _ev(2, "item.fulfilled", {"source_doc_id": "doc:MEMO-1", "doc_number": "MEMO-1",
               "quantity_fulfilled": 1, "doc_type": "memo"})
    assert "Consigned" in to_xml(activity_table([cons], subject_entity_id="item:x"))
    # reversal
    rev = _ev(3, "item.fulfillment_reversed", {"source_doc_id": "doc:INV-9",
              "doc_number": "INV-9", "quantity_restored": 2, "doc_type": "invoice"})
    assert "Sale reversed" in to_xml(activity_table([rev], subject_entity_id="item:x"))
    # memo->invoice sale confirmation via status.set
    st = _ev(4, "item.status.set", {"new_status": "sold", "source_doc_id": "doc:INV-7", "doc_number": "INV-7"})
    h2 = to_xml(activity_table([st], subject_entity_id="item:x"))
    assert "Sold" in h2 and "/docs/doc:INV-7" in h2


def test_doc_link_falls_back_to_entity_suffix():
    # Existing events without a stored doc_number still show a linkable number (entity suffix).
    ev = {"id": 1, "event_type": "item.fulfilled", "entity_id": "item:x",
          "ts": "2026-06-21T10:00:00+00:00",
          "data": {"source_doc_id": "doc:INV-9", "quantity_fulfilled": 1, "doc_type": "invoice"}}
    html = to_xml(activity_table([ev], subject_entity_id="item:x"))
    assert "INV-9" in html and "/docs/doc:INV-9" in html


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
    assert can_see_costs({}, "manager") and can_see_costs({}, "admin") and can_see_costs({}, "owner")
    assert not can_see_costs({}, "operator") and not can_see_costs({}, "viewer") and not can_see_costs({}, None)
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
    # doc fulfillment COGS stripped, item list kept
    assert redact_event_costs("doc.fulfilled", {"fulfilled_items": [{"sku": "A", "quantity": 2}], "total_cogs": 1500.0, "strategy": "per_line"}) \
        == {"fulfilled_items": [{"sku": "A", "quantity": 2}], "strategy": "per_line"}
    # item.created/snapshot carry full item state — strip cost, keep sell price
    assert redact_event_costs("item.created", {"sku": "A", "quantity": 3, "cost_total": 900.0, "cost_price": 300.0, "retail_price": 500.0}) \
        == {"sku": "A", "quantity": 3, "retail_price": 500.0}


def test_field_change_formatting():
    from ui.components.activity import _fields_changed_summary
    # pieces render as clean integers (10 -> 2, not 10 -> 2.0)
    assert _fields_changed_summary({"pieces": {"old": 10, "new": 2.0}}) == "Pieces: 10 → 2"
    # quantity trims trailing zeros
    assert _fields_changed_summary({"quantity": {"old": 5.0, "new": 3.5}}) == "Quantity: 5 → 3.5"
    # money is currency-formatted
    assert _fields_changed_summary({"amount": {"old": 100, "new": 120}}, "USD") == "Amount: $100.00 → $120.00"
    # dates render as dates, not datetimes
    assert _fields_changed_summary({"due_date": {"old": "2026-01-01T00:00:00+00:00", "new": "2026-02-01T00:00:00+00:00"}}) \
        == "Due date: 2026-01-01 → 2026-02-01"


def test_derived_doc_totals_suppressed_in_summary():
    # Editing an item's price recalculates subtotal/total, but the user's action was the line
    # edit alone - the summary must name only the changed item, not the derived totals.
    from ui.components.activity import _fields_changed_summary
    fields = {
        "line_items": {
            "old": [{"sku": "WIDGET", "name": "Widget", "quantity": 2, "unit_price": 10}],
            "new": [{"sku": "WIDGET", "name": "Widget", "quantity": 2, "unit_price": 15}],
        },
        "subtotal": {"old": 20, "new": 30},
        "total": {"old": 20, "new": 30},
        "tax_amount": {"old": 0, "new": 0},
    }
    summary = _fields_changed_summary(fields, "USD")
    assert "Widget" in summary
    assert "Subtotal" not in summary and "Total" not in summary
    # A change that touches only derived totals (no real user field) renders nothing.
    assert _fields_changed_summary({"subtotal": {"old": 20, "new": 30}, "total": {"old": 20, "new": 30}}) == ""


def test_per_item_tax_change_shows_pct_and_amount():
    """#156: a per-item tax-rate change names the item, the % change, and the amount -
    not a bare, item-less document-level 'Tax: 0.00 → 503.44'."""
    from ui.components.activity import _fields_changed_summary
    fields = {
        "line_items": {
            "old": [{"sku": "DEMO-AUTO-007", "name": "Widget", "quantity": 1,
                     "unit_price": 7192, "line_total": 7192, "tax_rate": 0}],
            "new": [{"sku": "DEMO-AUTO-007", "name": "Widget", "quantity": 1,
                     "unit_price": 7192, "line_total": 7192, "tax_rate": 7}],
        },
        # the document-level scalar that today renders as a bare, item-less amount
        "tax": {"old": 0.0, "new": 503.44},
        "tax_amount": {"old": 0.0, "new": 503.44},
    }
    summary = _fields_changed_summary(fields, "USD")
    assert "DEMO-AUTO-007 Tax 0% → 7%" in summary  # 7192 × 7% = 503.44
    assert "503.44" in summary
    # the bare, item-less document-level tax scalar is suppressed (no "Tax: 0.00 → 503.44")
    assert "Tax: " not in summary


def test_per_item_tax_change_reads_structured_taxes():
    """Effective rate is read from the structured taxes list (summed), not only legacy tax_rate."""
    from ui.components.activity import _fields_changed_summary
    fields = {"line_items": {
        "old": [{"sku": "A", "name": "A", "line_total": 1000, "taxes": []}],
        "new": [{"sku": "A", "name": "A", "line_total": 1000,
                 "taxes": [{"code": "VAT", "rate": 10.0}]}],
    }}
    summary = _fields_changed_summary(fields, "USD")
    assert "A Tax 0% → 10%" in summary
    assert "100.00" in summary  # 1000 × 10%


def test_doc_tax_scalar_kept_when_no_per_item_change():
    """With no per-line tax change, a document-level tax field still renders unchanged."""
    from ui.components.activity import _fields_changed_summary
    assert _fields_changed_summary({"tax": {"old": 0, "new": 50}}, "USD") == "Tax: $0.00 → $50.00"


def test_added_line_item_shows_type_qty_price():
    # Auditors need to see what kind of item was added and at what value. Bills/POs carry an
    # explicit receive_as (stock/expense); invoice lines read as Stock (linked catalog item) or
    # Non-stock (free-typed line).
    from ui.components.activity import _fields_changed_summary
    # True diff: a stock item and an expense added to an existing doc.
    fields = {"line_items": {
        "old": [{"sku": "OLD", "name": "Existing", "quantity": 1, "unit_price": 5}],
        "new": [
            {"sku": "OLD", "name": "Existing", "quantity": 1, "unit_price": 5},
            {"sku": "WIDGET", "name": "Widget", "quantity": 3, "unit_price": 10, "receive_as": "stock"},
            {"name": "Lunch", "quantity": 1, "unit_price": 25, "receive_as": "expense"},
        ],
    }}
    summary = _fields_changed_summary(fields, "USD")
    assert "Added Stock: Widget ×3 @ $10.00" in summary
    assert "Added Expense: Lunch ×1 @ $25.00" in summary
    # No old state (first save on a fresh doc): every line reads as an add with its type.
    fresh = {"line_items": {"new": [
        {"name": "Consulting", "quantity": 2, "unit_price": 150},   # free-typed -> Non-stock
        {"sku": "GADGET", "name": "Gadget", "quantity": 1, "unit_price": 40},  # catalog -> Stock
    ]}}
    fresh_summary = _fields_changed_summary(fresh, "USD")
    assert "Added Non-stock: Consulting ×2 @ $150.00" in fresh_summary
    assert "Added Stock: Gadget ×1 @ $40.00" in fresh_summary


def test_received_goods_names_items_like_fulfillment():
    # Issue 2: receiving goods must name which goods were received, the same way fulfillment does.
    from ui.components.activity import detail_from_entry
    received = detail_from_entry(
        {"received_items": [
            {"sku": "WIDGET", "name": "Widget", "quantity_received": 5},
            {"name": "Ad-hoc part", "quantity_received": 2},
        ]},
        "doc.received",
    )
    assert "WIDGET ×5" in received
    assert "Ad-hoc part ×2" in received


@pytest.mark.asyncio
async def test_patch_captures_old_value():
    # Issue 2: contact/doc/list field edits must record the real prior value as `old`
    # (history showed none -> new because old was hardcoded None).
    from ui.api_client import _wrap_fields_changed

    class _Resp:
        def __init__(self, d):
            self._d = d

        def json(self):
            return self._d

    class _C:
        def __init__(self, d):
            self._d = d

        async def get(self, path):
            return _Resp(self._d)

    c = _C({"name": "Old Name", "credit_limit": 100})
    # bare value -> old captured from the current entity
    assert await _wrap_fields_changed(c, "/x", {"name": "New Name"}) == {"name": {"old": "Old Name", "new": "New Name"}}
    # a caller that already passes {old,new} is respected
    assert await _wrap_fields_changed(c, "/x", {"name": {"old": "A", "new": "B"}}) == {"name": {"old": "A", "new": "B"}}

    class _Fail:
        async def get(self, path):
            raise RuntimeError("boom")

    # a failed lookup degrades gracefully to old None
    assert await _wrap_fields_changed(_Fail(), "/x", {"name": "New"}) == {"name": {"old": None, "new": "New"}}


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


# --------------------------------------------------------------------------- #
# Split / transform weight-reconciliation delta
#   delta = expected_remaining - measured_remaining
# Backend emission tests exercise the real split/transform endpoints; render
# tests feed a hand-built event to activity_table.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_split_records_delta_full_breakup(client):
    """A fully weighed break-up (parent consumed, every child weighed) records
    delta = parent_weight - sum(child_weights). Here 10 - (5 + 3) = 2."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, weight=10.0)
    r = await client.post(f"/items/{parent_id}/split", json={"children": [
        {"sku": "MUM.1", "quantity": 5.0, "weight": 5.0},
        {"sku": "MUM.2", "quantity": 5.0, "weight": 3.0}]}, headers=h)
    assert r.status_code == 200, r.text
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"]
    assert len(split) == 1
    d = split[0]["data"]
    assert "delta" in d
    assert d["delta"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_split_records_delta_on_reconcile(client):
    """A partial draw whose mother re-weigh differs from the derived remainder
    records delta = parent_weight - sum(child_weights) - mother_weight.
    Here 10 - 3 - 6 = 1 (derived remainder would have been 7)."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, weight=10.0)
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "MUM.1", "quantity": 3.0, "weight": 3.0}],
        "mother_weight": 6.0}, headers=h)
    assert r.status_code == 200, r.text
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"]
    assert len(split) == 1
    d = split[0]["data"]
    assert "delta" in d
    assert d["delta"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_split_delta_none_on_routine_draw(client):
    """A routine partial draw with no re-weigh has no measured remainder, so delta
    is present but None (honest degradation, never a fabricated anomaly)."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, weight=10.0)
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "MUM.1", "quantity": 3.0, "weight": 3.0}]}, headers=h)
    assert r.status_code == 200, r.text
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"]
    assert len(split) == 1
    d = split[0]["data"]
    assert "delta" in d
    assert d["delta"] is None


@pytest.mark.asyncio
async def test_split_delta_none_when_child_unweighed(client):
    """A full break-up in which a child carries no weight cannot be reconciled, so
    delta is present but None."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, weight=10.0)
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "MUM.1", "quantity": 10.0}]}, headers=h)
    assert r.status_code == 200, r.text
    split = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.split"]
    assert len(split) == 1
    d = split[0]["data"]
    assert "delta" in d
    assert d["delta"] is None


@pytest.mark.asyncio
async def test_transform_records_delta(client):
    """A transform records delta = parent_weight - child_weight. Here 10 - 8 = 2."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, weight=10.0)
    r = await client.post(f"/items/{parent_id}/transform", json={
        "child_sku": "CUT-1", "child_category": "Cut", "child_sell_by": "piece",
        "child_quantity": 1.0, "child_cost_total": 90.0, "child_weight": 8.0}, headers=h)
    assert r.status_code == 200, r.text
    tr = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.transform"]
    assert len(tr) == 1
    d = tr[0]["data"]
    assert "delta" in d
    assert d["delta"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_transform_delta_none_when_unweighed(client):
    """A transform whose child carries no weight cannot be reconciled, so delta is
    present but None."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    parent_id = await _seed(client, h, quantity=10.0, weight=10.0)
    r = await client.post(f"/items/{parent_id}/transform", json={
        "child_sku": "CUT-2", "child_category": "Cut", "child_sell_by": "piece",
        "child_quantity": 1.0, "child_cost_total": 90.0}, headers=h)
    assert r.status_code == 200, r.text
    tr = [e for e in await _events(client, h, parent_id) if e["event_type"] == "item.transform"]
    assert len(tr) == 1
    d = tr[0]["data"]
    assert "delta" in d
    assert d["delta"] is None


def test_split_delta_rendered_in_history():
    """A nonzero split delta renders one mother-level 'Delta' line in history."""
    ev = _split_event()
    ev["data"]["delta"] = 2.0
    html = to_xml(activity_table([ev], subject_entity_id="item:mum"))
    assert "Delta" in html


def test_split_delta_zero_not_rendered():
    """A zero delta is a clean reconciliation and shows no Delta line."""
    ev = _split_event()
    ev["data"]["delta"] = 0.0
    html = to_xml(activity_table([ev], subject_entity_id="item:mum"))
    assert "Delta" not in html


def test_split_delta_negative_flagged():
    """A negative split delta (measured more than expected) renders a Delta line
    flagged with the negative cell class."""
    ev = _split_event()
    ev["data"]["delta"] = -2.0
    html = to_xml(activity_table([ev], subject_entity_id="item:mum"))
    assert "Delta" in html
    assert "cell--negative" in html


def test_transform_delta_rendered_in_history():
    """A nonzero transform delta renders a 'Delta' line in history."""
    ev = {"id": 200, "event_type": "item.transform", "entity_id": "item:mum",
          "ts": "2026-06-21T10:00:00+00:00",
          "data": {"child_id": "item:c1", "child_sku": "CUT-1", "child_category": "Cut",
                   "parent_cost_total": 100.0, "child_cost_total": 90.0, "parent_sku": "MUM",
                   "child_origin_event_id": 51, "qty_before": 10, "qty_after": 0,
                   "delta": 2.0}}
    html = to_xml(activity_table([ev], subject_entity_id="item:mum"))
    assert "Delta" in html
