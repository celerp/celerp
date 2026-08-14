# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Item Draft status: manually created items start as draft, the circulating-stock
protections (edit_inventory_amounts, cost stripping) attach at commit, and reverting
an available item back to draft is a separately permissioned, guarded action.
"""
from __future__ import annotations

import pytest

from test_helpers import create_item, grant_permission, perm_setup  # noqa: E402


async def _item_state(client, headers: dict, item_id: str) -> dict:
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _set_status(client, headers: dict, item_id: str, status: str):
    return await client.post(f"/items/{item_id}/status",
                             json={"new_status": status}, headers=headers)


def _draft_item_body(location_id: str, sku: str = "DFT-1") -> dict:
    return {"sku": sku, "name": "Draft Widget", "quantity": 5,
            "location_id": location_id, "cost_price": 40.0, "sell_by": "piece"}


# ── Creation defaults ─────────────────────────────────────────────────────────

async def test_manual_create_defaults_to_draft(client, session):
    """POST /items with no status starts the item in draft; committing is a
    deliberate action, not a side effect of creation."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"]),
                          headers=ctx["admin_h"])
    assert r.status_code == 200, r.text
    state = await _item_state(client, ctx["admin_h"], r.json()["id"])
    assert state.get("status") == "draft"


async def test_import_and_split_children_stay_available(client, session):
    """System flows never produce drafts: an imported item and split children
    derive from stock already in circulation, so they start available."""
    ctx = await perm_setup(client, session)
    imp = await client.post(
        "/items/import/batch",
        json={"records": [{"entity_id": "item:imp-dft-1", "event_type": "item.created",
                           "source": "csv", "idempotency_key": "imp-dft-1",
                           "data": {"sku": "IMP-1", "name": "Imported", "quantity": 4,
                                    "sell_by": "piece"}}]},
        headers=ctx["admin_h"],
    )
    assert imp.status_code == 200, imp.text
    listing = (await client.get("/items", params={"search": "IMP-1"},
                                headers=ctx["admin_h"])).json()
    rows = listing if isinstance(listing, list) else listing.get("items", [])
    imported = next(x for x in rows if x.get("sku") == "IMP-1")
    assert imported.get("status") == "available"

    parent = await client.post(
        "/items", json={"sku": "SPL-P", "name": "Split Parent", "quantity": 10,
                        "location_id": ctx["location_id"], "sell_by": "piece",
                        "allow_splitting": True, "status": "available"},
        headers=ctx["admin_h"])
    assert parent.status_code == 200, parent.text
    sp = await client.post(f"/items/{parent.json()['id']}/split",
                           json={"children": [{"sku": "SPL-P.1", "quantity": 3}]},
                           headers=ctx["admin_h"])
    assert sp.status_code == 200, sp.text
    child_rows = (await client.get("/items", params={"search": "SPL-P.1"},
                                   headers=ctx["admin_h"])).json()
    child_rows = child_rows if isinstance(child_rows, list) else child_rows.get("items", [])
    child = next(x for x in child_rows if x.get("sku") == "SPL-P.1")
    assert child.get("status") == "available"


# ── Protections attach at commit, not creation ────────────────────────────────

async def test_draft_amount_edit_allowed_without_amounts_permission(client, session):
    """While an item is draft, a role holding edit_inventory hand-edits amounts
    freely even without edit_inventory_amounts: the lock protects circulating
    stock and attaches at commit."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "DFT-AMT"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _item_state(client, ctx["admin_h"], item_id)).get("status") == "draft"

    patch = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"quantity": {"old": 5, "new": 8}}},
        headers=ctx["operator_h"])
    assert patch.status_code == 200, patch.text


async def test_available_amount_edit_still_gated(client, session):
    """Committing re-arms the lock: the same operator is blocked on the same field
    once the item is available."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "AVL-AMT"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    mk = await _set_status(client, ctx["admin_h"], item_id, "available")
    assert mk.status_code == 200, mk.text

    patch = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"quantity": {"old": 5, "new": 8}}},
        headers=ctx["operator_h"])
    assert patch.status_code == 403, patch.text
    assert "quantity" in patch.json()["detail"]


async def test_draft_cost_visible_without_costs_permission(client, session):
    """A draft item's cost fields stay visible to a role holding edit_inventory even
    without view_inventory_costs: the creator must be able to finish authoring the
    item. Committed items keep today's stripping."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "view_inventory_costs", "manager")
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "DFT-COST"),
                          headers=ctx["admin_h"])
    draft_id = r.json()["id"]

    state = await _item_state(client, ctx["operator_h"], draft_id)
    assert state.get("cost_price") == 40.0, "draft cost hidden from its creator class"

    mk = await _set_status(client, ctx["admin_h"], draft_id, "available")
    assert mk.status_code == 200, mk.text
    committed = await _item_state(client, ctx["operator_h"], draft_id)
    assert "cost_price" not in committed, "committed cost leaked without view_inventory_costs"


async def test_draft_cost_edit_allowed_without_price_permission(client, session):
    """While draft, cost is authorable by edit_inventory alone; once available the
    set_inventory_prices gate applies as today."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "DFT-CEDIT"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]

    patch = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"cost_price": {"old": 40.0, "new": 55.0}}},
        headers=ctx["operator_h"])
    assert patch.status_code == 200, patch.text

    mk = await _set_status(client, ctx["admin_h"], item_id, "available")
    assert mk.status_code == 200, mk.text
    patch2 = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"cost_price": {"old": 55.0, "new": 60.0}}},
        headers=ctx["operator_h"])
    assert patch2.status_code == 403, patch2.text


# ── Status value validation ───────────────────────────────────────────────────

async def test_unknown_status_rejected(client, session):
    """The status endpoints validate against the known status set and answer 422
    with the allowed values, instead of storing an arbitrary string."""
    ctx = await perm_setup(client, session)
    r = await _set_status(client, ctx["admin_h"], ctx["item_id"], "banana")
    assert r.status_code == 422, r.text
    assert "banana" in r.json()["detail"] or "status" in r.json()["detail"].lower()


# ── Revert: permissioned and guarded ──────────────────────────────────────────

async def test_revert_requires_permission(client, session):
    """Available -> draft needs the revert_items_to_draft permission; edit_inventory
    alone is rejected with a 403 naming the permission. This closes the cost back
    door: without it, anyone could revert a lot to read its cost."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVP-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200

    denied = await _set_status(client, ctx["operator_h"], item_id, "draft")
    assert denied.status_code == 403, denied.text
    assert "revert_items_to_draft" in denied.json()["detail"]


async def test_clean_revert_succeeds(client, session):
    """A permitted role reverts an uncirculated available item back to draft, and
    the draft carve-outs apply again."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVC-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200

    rv = await _set_status(client, ctx["admin_h"], item_id, "draft")
    assert rv.status_code == 200, rv.text
    assert (await _item_state(client, ctx["admin_h"], item_id)).get("status") == "draft"


async def test_revert_blocked_by_circulation_event(client, session):
    """An available item with a stock event (a quantity adjustment) no longer
    reverts: the answer names the reason instead of silently allowing amount
    tampering on circulated stock."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVE-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200
    adj = await client.post(f"/items/{item_id}/adjust",
                            json={"new_qty": 7, "reason": "count correction"},
                            headers=ctx["admin_h"])
    assert adj.status_code == 200, adj.text

    rv = await _set_status(client, ctx["admin_h"], item_id, "draft")
    assert rv.status_code == 409, rv.text
    assert "history" in rv.json()["detail"].lower() or "circulat" in rv.json()["detail"].lower()


async def test_revert_blocked_when_on_document(client, session):
    """An available item sitting on a document's lines does not revert to draft."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVD-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200
    doc = await client.post(
        "/docs",
        json={"doc_type": "invoice",
              "line_items": [{"item_id": item_id, "sku": "RVD-1", "name": "Draft Widget",
                              "quantity": 1, "unit_price": 10.0}]},
        headers=ctx["admin_h"])
    assert doc.status_code == 200, doc.text

    rv = await _set_status(client, ctx["admin_h"], item_id, "draft")
    assert rv.status_code == 409, rv.text
    assert "document" in rv.json()["detail"].lower()


async def test_revert_blocked_when_on_document_via_patch_path(client, session):
    """Doc lines written through PATCH /docs keep their entity_id key (POST /docs
    normalizes to item_id), so the membership guard must match both spellings."""
    ctx = await perm_setup(client, session)
    filler = await create_item(client, ctx["admin_h"], ctx["location_id"], sku="RVDP-F")
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVDP-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200

    doc = await client.post(
        "/docs",
        json={"doc_type": "invoice",
              "line_items": [{"item_id": filler, "sku": "RVDP-F", "name": "Perm Item",
                              "quantity": 1, "unit_price": 10.0}]},
        headers=ctx["admin_h"])
    assert doc.status_code == 200, doc.text
    doc_id = doc.json()["id"]
    old_lines = (await client.get(f"/docs/{doc_id}", headers=ctx["admin_h"])).json()["line_items"]
    patch = await client.patch(
        f"/docs/{doc_id}",
        json={"fields_changed": {"line_items": {
            "old": old_lines,
            "new": old_lines + [{"entity_id": item_id, "sku": "RVDP-1",
                                 "name": "Draft Widget", "quantity": 1, "unit_price": 12.0}],
        }}},
        headers=ctx["admin_h"])
    assert patch.status_code == 200, patch.text

    rv = await _set_status(client, ctx["admin_h"], item_id, "draft")
    assert rv.status_code == 409, rv.text
    assert "document" in rv.json()["detail"].lower()


async def test_revert_of_reserved_item_rejected(client, session):
    """Only an available item reverts to draft: a reserved item answers 409 naming
    its current status, never quietly dropping a reservation."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVR-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200
    assert (await _set_status(client, ctx["admin_h"], item_id, "reserved")).status_code == 200

    rv = await _set_status(client, ctx["admin_h"], item_id, "draft")
    assert rv.status_code == 409, rv.text
    assert "available" in rv.json()["detail"].lower()
    assert "reserved" in rv.json()["detail"].lower()


async def test_revert_via_item_patch_requires_permission(client, session):
    """The revert guard also covers PATCH /items status changes: an operator
    patching status back to draft gets the same 403 as on the status endpoint."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "RVPP-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200

    denied = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"status": {"old": "available", "new": "draft"}}},
        headers=ctx["operator_h"])
    assert denied.status_code == 403, denied.text
    assert "revert_items_to_draft" in denied.json()["detail"]


async def test_bulk_revert_validates_all_before_any(client, session):
    """POST /items/bulk/status validates every item before emitting anything: one
    circulated item rejects the whole batch and the clean item keeps its status."""
    ctx = await perm_setup(client, session)
    clean = await client.post("/items", json=_draft_item_body(ctx["location_id"], "BLK-C"),
                              headers=ctx["admin_h"])
    clean_id = clean.json()["id"]
    dirty = await client.post("/items", json=_draft_item_body(ctx["location_id"], "BLK-D"),
                              headers=ctx["admin_h"])
    dirty_id = dirty.json()["id"]
    for iid in (clean_id, dirty_id):
        assert (await _set_status(client, ctx["admin_h"], iid, "available")).status_code == 200
    adj = await client.post(f"/items/{dirty_id}/adjust",
                            json={"new_qty": 9, "reason": "count correction"},
                            headers=ctx["admin_h"])
    assert adj.status_code == 200, adj.text

    bulk = await client.post("/items/bulk/status",
                             json={"entity_ids": [clean_id, dirty_id], "status": "draft"},
                             headers=ctx["admin_h"])
    assert bulk.status_code == 409, bulk.text
    assert (await _item_state(client, ctx["admin_h"], clean_id)).get("status") == "available"


async def test_status_noop_transitions_are_harmless(client, session):
    """Make-available on an already-available item and draft on an already-draft
    item are accepted no-ops, keeping the bulk action idempotent."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "NOP-1"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]

    again = await _set_status(client, ctx["admin_h"], item_id, "draft")
    assert again.status_code == 200, again.text
    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200
    again2 = await _set_status(client, ctx["admin_h"], item_id, "available")
    assert again2.status_code == 200, again2.text


# ── Drafts are excluded wherever stock is counted ─────────────────────────────

async def test_draft_excluded_from_valuation_totals_but_counted(client, session):
    """A draft is not stock: valuation totals and active_item_count skip it, but
    count_by_status still carries it so the Drafts card shows a number."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "VAL-DFT"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]
    assert (await _item_state(client, ctx["admin_h"], item_id)).get("status") == "draft"

    val = (await client.get("/items/valuation", headers=ctx["admin_h"])).json()
    base_count = val["active_item_count"]
    base_cost = val.get("cost_total", 0.0)
    assert val["count_by_status"].get("draft", 0) >= 1
    # 5 pcs at cost 40 = 200 must NOT be in the cost total while draft.

    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200
    val2 = (await client.get("/items/valuation", headers=ctx["admin_h"])).json()
    assert val2["active_item_count"] == base_count + 1
    assert val2.get("cost_total", 0.0) == pytest.approx(base_cost + 200.0)
    assert val2["count_by_status"].get("draft", 0) == val["count_by_status"]["draft"] - 1


async def test_low_stock_filter_excludes_draft(client, session):
    """The low-stock filter never nags about an item that is not stock yet."""
    ctx = await perm_setup(client, session)
    body = _draft_item_body(ctx["location_id"], "LOW-DFT") | {"reorder_point": 10}
    r = await client.post("/items", json=body, headers=ctx["admin_h"])
    assert r.status_code == 200, r.text

    low = (await client.get("/items", params={"filter": "low_stock"},
                            headers=ctx["admin_h"])).json()
    rows = low if isinstance(low, list) else low.get("items", [])
    assert all(x.get("sku") != "LOW-DFT" for x in rows), "draft surfaced as low stock"

    assert (await _set_status(client, ctx["admin_h"], r.json()["id"], "available")).status_code == 200
    low2 = (await client.get("/items", params={"filter": "low_stock"},
                             headers=ctx["admin_h"])).json()
    rows2 = low2 if isinstance(low2, list) else low2.get("items", [])
    assert any(x.get("sku") == "LOW-DFT" for x in rows2)


async def test_dashboard_kpis_exclude_draft(client, session):
    """The dashboard's per-item KPIs skip drafts: a draft below its reorder point
    never counts as low stock until it is committed."""
    ctx = await perm_setup(client, session)
    base = (await client.get("/dashboard/kpis", headers=ctx["admin_h"])).json()
    base_low = base["inventory"]["low_stock_items"]

    body = _draft_item_body(ctx["location_id"], "KPI-DFT") | {"reorder_point": 10}
    r = await client.post("/items", json=body, headers=ctx["admin_h"])
    assert r.status_code == 200, r.text
    with_draft = (await client.get("/dashboard/kpis", headers=ctx["admin_h"])).json()
    assert with_draft["inventory"]["low_stock_items"] == base_low

    assert (await _set_status(client, ctx["admin_h"], r.json()["id"], "available")).status_code == 200
    committed = (await client.get("/dashboard/kpis", headers=ctx["admin_h"])).json()
    assert committed["inventory"]["low_stock_items"] == base_low + 1


async def test_opening_inventory_je_excludes_draft(client, session):
    """The opening-balance JE recalculated on balance-sheet load counts committed
    stock only: committing a draft raises the 1130-OB debit by exactly its cost."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "OB-DFT"),
                          headers=ctx["admin_h"])
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    assert (await client.get("/accounting/balance-sheet", headers=ctx["admin_h"])).status_code == 200
    led1 = await client.get("/accounting/ledger/1130-OB", headers=ctx["admin_h"])
    assert led1.status_code == 200, led1.text
    ob_before = sum(float(ln.get("debit", 0) or 0) for ln in led1.json()["lines"])

    assert (await _set_status(client, ctx["admin_h"], item_id, "available")).status_code == 200
    assert (await client.get("/accounting/balance-sheet", headers=ctx["admin_h"])).status_code == 200
    led2 = await client.get("/accounting/ledger/1130-OB", headers=ctx["admin_h"])
    ob_after = sum(float(ln.get("debit", 0) or 0) for ln in led2.json()["lines"])
    # 5 pcs at cost 40 = 200 enters opening inventory only at commit.
    assert ob_after - ob_before == pytest.approx(200.0, abs=0.02)


def test_manufacturing_lot_qty_excludes_draft_lots():
    """On-hand lot totals never count a draft lot; a lot with no status counts as
    available, matching the projection default for pre-draft data."""
    from celerp_manufacturing.routes import _lot_qty_by_parent

    states = {
        "item:lot-1": {"parent_item_id": "item:prod", "quantity": 4.0, "status": "available"},
        "item:lot-2": {"parent_item_id": "item:prod", "quantity": 3.0, "status": "draft"},
        "item:lot-3": {"parent_item_id": "item:prod", "quantity": 2.0},
    }
    assert _lot_qty_by_parent(states) == {"item:prod": 6.0}


# ── Drafts cannot circulate onto documents or lists ───────────────────────────

async def test_doc_line_add_rejects_draft_item(client, session):
    """Putting a draft item on a document is rejected at the function level with
    the reason, never by hiding the item from the form."""
    ctx = await perm_setup(client, session)
    r = await client.post("/items", json=_draft_item_body(ctx["location_id"], "DOC-DFT"),
                          headers=ctx["admin_h"])
    item_id = r.json()["id"]

    doc = await client.post(
        "/docs",
        json={"doc_type": "invoice",
              "line_items": [{"item_id": item_id, "sku": "DOC-DFT", "name": "Draft Widget",
                              "quantity": 1, "unit_price": 10.0}]},
        headers=ctx["admin_h"])
    assert doc.status_code == 422, doc.text
    detail = doc.json()["detail"]
    msg = detail["message"] if isinstance(detail, dict) else str(detail)
    assert "draft" in msg.lower()


async def test_list_scan_rejects_draft_item(client, session):
    """Scanning a draft item's barcode onto a building list answers with the
    draft reason instead of silently adding a line."""
    ctx = await perm_setup(client, session)
    body = _draft_item_body(ctx["location_id"], "SCN-DFT") | {"barcode": "999888777666"}
    r = await client.post("/items", json=body, headers=ctx["admin_h"])
    assert r.status_code == 200, r.text

    lst = await client.post("/lists", json={"list_type": "quotation"}, headers=ctx["admin_h"])
    assert lst.status_code == 200, lst.text
    scan = await client.post(f"/lists/{lst.json()['id']}/scan",
                             json={"barcode": "999888777666"}, headers=ctx["admin_h"])
    assert scan.status_code == 422, scan.text
    assert "draft" in str(scan.json()["detail"]).lower()


# ── UI surfaces ───────────────────────────────────────────────────────────────

def test_inventory_draft_card_renders():
    """The inventory status cards include a Drafts counter fed by count_by_status."""
    from fasthtml.common import to_xml

    from ui.routes.inventory import _inventory_status_cards

    html = to_xml(_inventory_status_cards({"available": 5, "draft": 2}, ""))
    assert "Draft" in html
    assert "2" in html


def test_field_schema_status_options_include_draft():
    """The status field's option list carries draft so the click-to-edit status
    cell can set and display it."""
    from celerp.services.field_schema import _BASE_FIELDS

    status_field = next(f for f in _BASE_FIELDS if f["key"] == "status")
    assert "draft" in status_field["options"]


# ── Registry row ──────────────────────────────────────────────────────────────

def test_revert_permission_in_catalogue():
    """The registry carries revert_items_to_draft: manager default, grantable,
    viewer floor - role-based like every other inventory gate."""
    from celerp.services.permissions import PERMISSIONS

    by_key = {p.key: p for p in PERMISSIONS}
    assert "revert_items_to_draft" in by_key
    p = by_key["revert_items_to_draft"]
    assert p.default_role == "manager"
    assert p.grantable is True
    assert p.floor_role == "viewer"
