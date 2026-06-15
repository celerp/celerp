# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Inventory audit via scanner: location-bound list_type=audit, uniform scan rule (a scan always
audits, adding the line if new), optional re-count, then a reversible stock adjustment that posts a
shrinkage/overage JE. See context/2026-0614-inventory-audit-scanner-plan.md."""
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
    r = await client.post("/audits", headers=_h(t), json={"location_id": loc})
    assert r.status_code == 200, r.text
    return r.json()


# --- creation + pre-population --------------------------------------------

@pytest.mark.asyncio
async def test_create_audit_prepopulates_location(client):
    t = await _register(client)
    loc_a = await _location(client, t, "A")
    loc_b = await _location(client, t, "B")
    here = await _item(client, t, "HERE-1", loc=loc_a, qty=5, barcode="1001")
    await _item(client, t, "THERE-1", loc=loc_b, qty=2)                 # other location
    await _item(client, t, "SVC-1", loc=loc_a, qty=0, inventory_type="service")  # non-stock
    audit = await _audit(client, t, loc_a)
    state = (await client.get(f"/audits/{audit['id']}", headers=_h(t))).json()
    assert state["list_type"] == "audit" and state["location_id"] == loc_a and state["status"] == "unaudited"
    skus = {l["sku"] for l in state["line_items"]}
    assert skus == {"HERE-1"}  # only the in-location, physical, available item
    assert state["line_items"][0]["audited_at"] is None
    assert state["ref_id"].startswith("AUD-")


# --- scan state machine ----------------------------------------------------

@pytest.mark.asyncio
async def test_scan_rule(client):
    t = await _register(client)
    loc = await _location(client, t)
    expected = await _item(client, t, "EXP-1", loc=loc, qty=3, barcode="1002")
    unexpected = await _item(client, t, "UNEXP-1", loc=loc, qty=1, barcode="1003")
    # Make UNEXP not pre-populated by putting it at another location after audit creation is simplest;
    # instead, create the audit first, then it only contains EXP-1 (UNEXP added below by scanning).
    loc2 = await _location(client, t, "B")
    # Move unexpected to loc2 so it isn't pre-populated.
    await client.post(f"/items/{unexpected}/transfer", headers=_h(t), json={"to_location_id": loc2})
    audit = (await _audit(client, t, loc))["id"]
    state = (await client.get(f"/audits/{audit}", headers=_h(t))).json()
    assert {l["sku"] for l in state["line_items"]} == {"EXP-1"}

    # Scan expected (on list, un-audited) -> audited.
    r = await client.post(f"/audits/{audit}/scan", headers=_h(t), json={"barcode": "1002"})
    assert r.status_code == 200 and r.json()["state"] == "audited"
    # Scan expected again -> already scanned.
    r = await client.post(f"/audits/{audit}/scan", headers=_h(t), json={"barcode": "1002"})
    assert r.status_code == 409

    # Scan unexpected (not on list) -> added AND audited, top-inserted.
    r = await client.post(f"/audits/{audit}/scan", headers=_h(t), json={"barcode": "1003"})
    assert r.status_code == 200 and r.json()["state"] == "added"
    state = (await client.get(f"/audits/{audit}", headers=_h(t))).json()
    assert state["line_items"][0]["sku"] == "UNEXP-1" and state["line_items"][0]["audited_at"] is not None

    # Unknown barcode -> rejected.
    assert (await client.post(f"/audits/{audit}/scan", headers=_h(t), json={"barcode": "NOPE"})).status_code == 404


# --- count semantics + adjust + undo + JE ----------------------------------

@pytest.mark.asyncio
async def test_adjust_posts_je_and_is_undoable(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "A1", loc=loc, qty=10, cost_total=100, barcode="1004")  # unit cost 10
    b = await _item(client, t, "B1", loc=loc, qty=4, cost_total=40, barcode="1005")    # unit cost 10
    c = await _item(client, t, "C1", loc=loc, qty=7, cost_total=70, barcode="1006")    # untouched
    audit = (await _audit(client, t, loc))["id"]

    # Count A down to 8 (shrink 2 -> 20), B up to 6 (overage 2 -> 20). Leave C untouched (NULL).
    assert (await client.patch(f"/audits/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    assert (await client.patch(f"/audits/{audit}/line/{b}", headers=_h(t), json={"counted_qty": 6})).status_code == 200

    # Must mark done before adjusting.
    assert (await client.post(f"/audits/{audit}/adjust", headers=_h(t))).status_code == 409
    assert (await client.post(f"/audits/{audit}/done", headers=_h(t))).status_code == 200

    r = await client.post(f"/audits/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["adjusted"] == 2 and body["shrinkage_value"] == 20.0 and body["overage_value"] == 20.0

    # Quantities applied; C untouched.
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 8
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 6
    assert (await client.get(f"/items/{c}", headers=_h(t))).json()["quantity"] == 7
    assert (await client.get(f"/audits/{audit}", headers=_h(t))).json()["status"] == "stock_adjusted"

    # The audit JE is balanced and uses the shrinkage/overage accounts; provenance recorded.
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if audit in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    d = sum(float(x.get("debit", 0) or 0) for x in entries)
    cr = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(d - cr) < 1e-6
    accts = {x["account"] for x in entries}
    assert {"5100", "1130", "4300"} <= accts

    # Undo: quantities restored, status back to audited.
    assert (await client.post(f"/audits/{audit}/undo-adjust", headers=_h(t))).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 10
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 4
    assert (await client.get(f"/audits/{audit}", headers=_h(t))).json()["status"] == "audited"
    # JE voided: a void event exists for the audit's journal entry.
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je_events = [e for e in ledger if audit in (e.get("entity_id") or "")]
    assert any(e.get("event_type") == "acc.journal_entry.voided" for e in je_events)


@pytest.mark.asyncio
async def test_count_zero_sets_quantity_to_zero(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "Z1", loc=loc, qty=5, cost_total=50, barcode="1007")
    audit = (await _audit(client, t, loc))["id"]
    assert (await client.patch(f"/audits/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 0})).status_code == 200
    await client.post(f"/audits/{audit}/done", headers=_h(t))
    assert (await client.post(f"/audits/{audit}/adjust", headers=_h(t))).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 0


@pytest.mark.asyncio
async def test_untouched_lines_are_skipped(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "S1", loc=loc, qty=5, cost_total=50, barcode="1008")
    audit = (await _audit(client, t, loc))["id"]
    # Scan it (audited) but never set a count -> adjust changes nothing.
    await client.post(f"/audits/{audit}/scan", headers=_h(t), json={"barcode": "1008"})
    await client.post(f"/audits/{audit}/done", headers=_h(t))
    r = await client.post(f"/audits/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200 and r.json()["adjusted"] == 0
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 5


@pytest.mark.asyncio
async def test_audit_list_cannot_be_converted(client):
    t = await _register(client)
    loc = await _location(client, t)
    audit = (await _audit(client, t, loc))["id"]
    r = await client.post(f"/lists/{audit}/convert", headers=_h(t), json={"target_type": "invoice"})
    assert r.status_code == 409
