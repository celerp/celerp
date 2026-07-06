# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Inventory audit on the unified list lifecycle (list_type=audit): a draft manifest is built/seeded,
Finalize freezes the on-hand snapshot and opens the counting stage, scanning records presence, counts
are entered, then a reversible stock adjustment overwrites item qty to the count and posts a
shrinkage/overage JE. See context/2026-0617-unified-lists-lifecycle-plan.md."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@audit.test"
    r = await client.post("/auth/register", json={"company_name": "Audit Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t, name="Warehouse A") -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, t, sku, *, loc, qty, barcode=None, cost_total=None, inventory_type="stocked") -> str:
    body = {"sku": sku, "name": sku, "quantity": qty, "sell_by": "piece", "location_id": loc,
            "inventory_type": inventory_type}
    if barcode:
        body["barcode"] = barcode
    if cost_total is not None:
        body["cost_total"] = cost_total
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _audit(client, t, loc) -> dict:
    r = await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})
    assert r.status_code == 200, r.text
    return r.json()


async def _state(client, t, audit_id) -> dict:
    return (await client.get(f"/lists/{audit_id}", headers=_h(t))).json()


async def _finalize(client, t, audit_id):
    r = await client.post(f"/lists/{audit_id}/finalize", headers=_h(t))
    assert r.status_code == 200, r.text
    return r


# --- creation + pre-population (draft manifest) ----------------------------

@pytest.mark.asyncio
async def test_create_audit_prepopulates_location_as_draft(client):
    t = await _register(client)
    loc_a = await _location(client, t, "A")
    loc_b = await _location(client, t, "B")
    await _item(client, t, "HERE-1", loc=loc_a, qty=5, barcode="1001")
    await _item(client, t, "THERE-1", loc=loc_b, qty=2)                 # other location
    await _item(client, t, "SVC-1", loc=loc_a, qty=0, inventory_type="service")  # non-stock
    audit = await _audit(client, t, loc_a)
    state = await _state(client, t, audit["id"])
    assert state["list_type"] == "audit" and state["location_id"] == loc_a
    assert state["status"] == "draft"  # born as a reviewable draft manifest, not mid-count
    assert {l["sku"] for l in state["line_items"]} == {"HERE-1"}  # only in-location, physical, available
    assert state["ref_id"].startswith("AUD-")
    # No on-hand snapshot yet — that is frozen at Finalize.
    assert "on_hand" not in state["line_items"][0]


@pytest.mark.asyncio
async def test_finalize_freezes_onhand_and_opens_counting(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "F1", loc=loc, qty=9, cost_total=90, barcode="2001")
    audit = (await _audit(client, t, loc))["id"]
    # Counting is blocked while still a draft.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 7})).status_code == 409
    await _finalize(client, t, audit)
    state = await _state(client, t, audit)
    assert state["status"] == "finalized"
    assert state["line_items"][0]["on_hand"] == 9.0  # snapshot frozen at finalize
    # Now counts are accepted.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 7})).status_code == 200


# --- scan dispatch on (status) --------------------------------------------

@pytest.mark.asyncio
async def test_scan_adds_in_draft_records_presence_when_finalized(client):
    t = await _register(client)
    loc = await _location(client, t)
    exp = await _item(client, t, "EXP-1", loc=loc, qty=3, barcode="1002")
    unexp = await _item(client, t, "UNEXP-1", loc=loc, qty=1, barcode="1003")
    loc2 = await _location(client, t, "B")
    await client.post(f"/items/{unexp}/transfer", headers=_h(t), json={"to_location_id": loc2})
    audit = (await _audit(client, t, loc))["id"]
    assert {l["sku"] for l in (await _state(client, t, audit))["line_items"]} == {"EXP-1"}

    # DRAFT scan: ADD a not-yet-on-manifest item; no status change (GDR 2d).
    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1003"})
    assert r.status_code == 200 and r.json()["state"] == "added"
    state = await _state(client, t, audit)
    assert state["status"] == "draft"  # scanning never finalizes
    assert state["line_items"][0]["sku"] == "UNEXP-1"
    assert state["line_items"][0].get("audited_at") is None  # presence is a counting-stage concept

    await _finalize(client, t, audit)
    # FINALIZED scan: the manifest is LOCKED — scanning only checks off items already on the list
    # (records presence, top-insert). It never adds.
    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1002"})
    assert r.status_code == 200 and r.json()["state"] == "audited"
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None
    # Re-scanning the same item just re-confirms it (no error) — you can re-check anything.
    assert (await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1002"})).status_code == 200
    # Scanning an item NOT on the locked manifest is rejected (409 with a clear reason), not added.
    await _item(client, t, "EXTRA-1", loc=loc, qty=2, barcode="1099")
    rej = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1099"})
    assert rej.status_code == 409 and "not on this audit" in rej.json()["detail"].lower()
    assert "EXTRA-1" not in {l["sku"] for l in (await _state(client, t, audit))["line_items"]}  # not added
    # Unknown barcode -> 404 in any state.
    assert (await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "NOPE"})).status_code == 404


# --- duplicate-sku, distinct-lot audit invariant (2026-06-17 sku/batch plan §7.1) --

@pytest.mark.asyncio
async def test_audit_duplicate_sku_distinct_lots_survive_and_scan_binds_lot(client):
    """The single biggest audit-side footgun of non-unique SKU: two same-sku,
    distinct-item lots must stay TWO audit lines keyed on item_id (never merged by
    sku). A scanned barcode checks off exactly its lot; the shared sku is ambiguous
    (409), never a silent check-off; and Adjust emits two independent quantity
    changes (no double-count, no loss)."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "SH", loc=loc, qty=5, cost_total=50, barcode="3501")
    b = await _item(client, t, "SH", loc=loc, qty=7, cost_total=70, barcode="3502")
    audit = (await _audit(client, t, loc))["id"]

    # Draft manifest: two distinct lines, same sku, keyed on item_id.
    lines = (await _state(client, t, audit))["line_items"]
    assert len([l for l in lines if l["sku"] == "SH"]) == 2
    assert {l["item_id"] for l in lines} == {a, b}

    await _finalize(client, t, audit)
    fin_lines = (await _state(client, t, audit))["line_items"]
    # STILL two lines after finalize (dedup keys on item_id, not sku).
    assert {l["item_id"] for l in fin_lines} == {a, b}
    on_hand = {l["item_id"]: l["on_hand"] for l in fin_lines}
    assert on_hand[a] == 5.0 and on_hand[b] == 7.0  # each froze its own on-hand

    # Scan lot B's barcode -> only B checked off, A untouched.
    assert (await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "3502"})).status_code == 200
    audited = {l["item_id"]: l.get("audited_at") for l in (await _state(client, t, audit))["line_items"]}
    assert audited[b] is not None and audited[a] is None

    # Scanning/typing the shared SKU is ambiguous -> 409, never a silent check-off.
    rej = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "SH"})
    assert rej.status_code == 409 and "sku" in rej.json()["detail"].lower()

    # Count each lot independently, then Adjust: two independent quantity changes.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 4})).status_code == 200
    assert (await client.patch(f"/lists/{audit}/line/{b}", headers=_h(t), json={"counted_qty": 9})).status_code == 200
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    assert r.json()["adjusted"] == 2  # both lots adjusted, neither dropped nor merged
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 4
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 9


# --- count -> adjust (vs LIVE qty) -> undo + JE ----------------------------

@pytest.mark.asyncio
async def test_adjust_overwrites_to_count_posts_je_and_is_undoable(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "A1", loc=loc, qty=10, cost_total=100, barcode="1004")  # unit cost 10
    b = await _item(client, t, "B1", loc=loc, qty=4, cost_total=40, barcode="1005")    # unit cost 10
    c = await _item(client, t, "C1", loc=loc, qty=7, cost_total=70, barcode="1006")    # untouched
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)

    # Count A down to 8 (shrink 2 -> 20), B up to 6 (overage 2 -> 20). Leave C uncounted.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    assert (await client.patch(f"/lists/{audit}/line/{b}", headers=_h(t), json={"counted_qty": 6})).status_code == 200

    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["adjusted"] == 2 and body["shrinkage_value"] == 20.0 and body["overage_value"] == 20.0
    assert body["skipped"] == 1  # C reported as skipped (uncounted), item untouched

    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 8  # new_qty == count
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 6
    assert (await client.get(f"/items/{c}", headers=_h(t))).json()["quantity"] == 7
    st = await _state(client, t, audit)
    assert st["status"] == "closed" and st["result"] == "stock_adjusted"

    # Balanced JE on the shrinkage/overage/inventory accounts.
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if audit in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    assert abs(sum(float(x.get("debit", 0) or 0) for x in entries) - sum(float(x.get("credit", 0) or 0) for x in entries)) < 1e-6
    assert {"5100", "1130-P", "4300"} <= {x["account"] for x in entries}

    # Undo: quantities restored, audit reopened to finalized, JE voided.
    assert (await client.post(f"/lists/{audit}/undo-adjust", headers=_h(t))).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 10
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 4
    st = await _state(client, t, audit)
    assert st["status"] == "finalized" and "result" not in st
    je_events = [e for e in (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
                 if audit in (e.get("entity_id") or "")]
    assert any(e.get("event_type") == "acc.journal_entry.voided" for e in je_events)


@pytest.mark.asyncio
async def test_adjust_delta_uses_live_qty_not_frozen_snapshot(client):
    """Decision 5.2: new_qty == count; the JE delta is count - LIVE qty (not count - frozen snapshot).
    A sale between Finalize and Adjust must not be double-counted."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "LV1", loc=loc, qty=10, cost_total=100, barcode="3001")  # unit cost 10
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)  # snapshot S = 10
    assert (await _state(client, t, audit))["line_items"][0]["on_hand"] == 10.0
    # A real movement drops live qty to 8 after finalize.
    await client.post(f"/items/{a}/adjust", headers=_h(t), json={"new_qty": 8})
    # Counter physically finds 8.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    # Overwrite-to-count lands on exactly 8 (not 6 as frozen-delta would give); no value change vs live.
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 8
    assert r.json()["adjusted"] == 0 and r.json()["shrinkage_value"] == 0.0  # count == live -> nothing to post


@pytest.mark.asyncio
async def test_count_zero_sets_quantity_to_zero(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "Z1", loc=loc, qty=5, cost_total=50, barcode="1007")
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 0})).status_code == 200
    assert (await client.post(f"/lists/{audit}/adjust", headers=_h(t))).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 0


@pytest.mark.asyncio
async def test_uncounted_lines_skipped(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "S1", loc=loc, qty=5, cost_total=50, barcode="1008")
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)
    # Uncounted -> skipped, item untouched.
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200 and r.json()["adjusted"] == 0 and r.json()["skipped"] == 1
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 5


@pytest.mark.asyncio
async def test_audit_list_cannot_be_converted(client):
    t = await _register(client)
    loc = await _location(client, t)
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)
    r = await client.post(f"/lists/{audit}/convert", headers=_h(t), json={"target_type": "invoice"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_scanned_highlight_persists_and_clears_via_bulk(client):
    """A scanned line stays marked (audited_at) indefinitely — including across a list-type round
    trip — and is reverted by the bulk Clear scanned action."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "CS1", loc=loc, qty=5, barcode="7001")
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)

    await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "7001"})
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None

    # Scanned status survives changing the type away and back.
    await client.post(f"/lists/{audit}/change-type", headers=_h(t), json={"list_type": "quotation"})
    await client.post(f"/lists/{audit}/change-type", headers=_h(t), json={"list_type": "audit"})
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None

    # Bulk Clear scanned reverts the highlight.
    r = await client.post(f"/lists/{audit}/set-scanned", headers=_h(t), json={"scanned": False})
    assert r.status_code == 200 and r.json()["changed"] == 1
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is None

    # Bulk Mark as scanned re-applies the highlight (the inverse action).
    r = await client.post(f"/lists/{audit}/set-scanned", headers=_h(t), json={"scanned": True})
    assert r.status_code == 200 and r.json()["changed"] == 1
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None


@pytest.mark.asyncio
async def test_editable_save_keeps_item_link(client):
    """The editable draft UI sends a line's item id in `entity_id` (no `item_id`). Persisting must
    normalize it to `item_id`, else on-hand freeze (-> 0), scan check-off (-> "not on this audit") and
    dedup all break. Reproduces the regression from making draft audits editable."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "EL1", loc=loc, qty=7, barcode="8001")
    audit = (await _audit(client, t, loc))["id"]

    # Simulate an editable-row autosave: the frontend sends entity_id, not item_id.
    line = (await _state(client, t, audit))["line_items"][0]
    edited = {"entity_id": iid, "sku": "EL1", "name": "EL1", "quantity": 7}  # note: no item_id key
    await client.patch(f"/lists/{audit}", headers=_h(t),
                       json={"fields_changed": {"line_items": {"old": [line], "new": [edited]}}})
    stored = (await _state(client, t, audit))["line_items"][0]
    assert stored.get("item_id") == iid              # normalized on persist (was missing -> the bug)

    await _finalize(client, t, audit)
    assert (await _state(client, t, audit))["line_items"][0]["on_hand"] == 7.0  # freeze found the item

    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "8001"})
    assert r.status_code == 200 and r.json()["state"] == "audited"  # check-off works (was 409)


@pytest.mark.asyncio
async def test_finalize_dedupes_duplicate_item_lines(client):
    """An audit counts each item once. If the draft manifest ends up with two lines for the same
    item_id (hand-edited, or via a list-type round trip), Finalize collapses them to one — otherwise
    check-off can never reach the second row (scan always matches the first) and Adjust double-counts."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "DUP-1", loc=loc, qty=4, barcode="9001")
    audit = (await _audit(client, t, loc))["id"]

    # Seeded with one line for the item; inject a second identical line via the editable-save path.
    line = (await _state(client, t, audit))["line_items"][0]
    await client.patch(f"/lists/{audit}", headers=_h(t),
                       json={"fields_changed": {"line_items": {"old": [line], "new": [line, dict(line)]}}})
    assert len((await _state(client, t, audit))["line_items"]) == 2  # duplicate present pre-finalize

    await _finalize(client, t, audit)
    lines = (await _state(client, t, audit))["line_items"]
    assert len(lines) == 1                 # collapsed to one line per item_id
    assert lines[0]["item_id"] == iid
    assert lines[0]["on_hand"] == 4.0      # surviving line still froze its on-hand snapshot
