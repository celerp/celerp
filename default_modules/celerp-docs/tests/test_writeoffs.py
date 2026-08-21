# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Inventory write-off / disposal on the unified list lifecycle (list_type=writeoff): a draft list is
seeded from an inventory selection, each line carries a quantity to remove, a destination expense/cogs/
equity account and a free-text reason, then the Write off stock terminal removes the stock (whole row or
a carved child lot) and posts one balanced journal entry (Dr chosen account / Cr Inventory). Every
written-off portion ends as a hidden `disposed` item row - the permanent disposal record. Undo voids the
JE and restores each disposed lot to `available`. Mirrors the audit list flow (test_audits.py)."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@writeoff.test"
    r = await client.post("/auth/register", json={"company_name": "WO Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@writeoff.test"
    r = await client.post(
        "/companies/me/users",
        json={"name": role.title(), "email": addr, "password": "testpass123", "role": role},
        headers=_h(admin_token),
    )
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    r2 = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def _location(client, t, name="Warehouse A") -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, t, sku, *, loc=None, qty, cost_total=None, sell_by="piece", inventory_type="stocked") -> str:
    body = {"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by,
            "inventory_type": inventory_type}
    if loc:
        body["location_id"] = loc
    if cost_total is not None:
        body["cost_total"] = cost_total
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _writeoff(client, t, entity_ids) -> dict:
    r = await client.post("/lists/writeoff", headers=_h(t), json={"entity_ids": entity_ids})
    assert r.status_code == 200, r.text
    return r.json()


async def _state(client, t, wo_id) -> dict:
    return (await client.get(f"/lists/{wo_id}", headers=_h(t))).json()


async def _line_id(client, t, wo_id, item_id) -> str:
    """The generated line_id of the seeded line for item_id."""
    lines = (await _state(client, t, wo_id))["line_items"]
    return next(l["line_id"] for l in lines if l["item_id"] == item_id)


async def _set_line(client, t, wo_id, *, line_id=None, item_id=None, qty_out=None, account=None, comment=None):
    body: dict = {}
    if line_id is not None:
        body["line_id"] = line_id
    if item_id is not None:
        body["item_id"] = item_id
    if qty_out is not None:
        body["qty_out"] = qty_out
    if account is not None:
        body["account"] = account
    if comment is not None:
        body["comment"] = comment
    return await client.post(f"/lists/{wo_id}/writeoff-line", headers=_h(t), json=body)


async def _finalize(client, t, wo_id):
    r = await client.post(f"/lists/{wo_id}/finalize", headers=_h(t))
    assert r.status_code == 200, r.text
    return r


async def _terminal(client, t, wo_id):
    return await client.post(f"/lists/{wo_id}/write-off", headers=_h(t))


async def _je_for(client, t, wo_id) -> dict | None:
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    return next((e for e in ledger if wo_id in (e["data"].get("memo") or "")), None)


# Seeded-chart destination accounts (default_modules/celerp-accounting chart): 6950 Misc Expenses
# (expense), 6600 Marketing (expense) are valid destinations; 1130-P (asset) and 4100 (revenue) are
# wrong-class; 9999 does not exist.
EXP_A = "6950"
EXP_B = "6600"


# --- full-row disposal + JE ------------------------------------------------

@pytest.mark.asyncio
async def test_writeoff_full_row_disposes_and_posts_je(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-FULL", loc=loc, qty=4, cost_total=40)  # unit cost 10
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A, comment="spoiled")
    await _finalize(client, t, wo)

    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written_off"] == 1 and body["skipped"] == 0 and body["value"] == 40.0

    # The whole row is now a hidden `disposed` item (no split).
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "disposed"
    assert a not in {i["entity_id"] for i in (await client.get("/items", headers=_h(t))).json()["items"]}

    st = await _state(client, t, wo)
    assert st["status"] == "closed" and st["result"] == "written_off"

    # One balanced JE: Dr chosen expense account / Cr Inventory at cost_total.
    je = await _je_for(client, t, wo)
    assert je is not None
    entries = je["data"]["entries"]
    assert abs(sum(float(x.get("debit", 0) or 0) for x in entries)
               - sum(float(x.get("credit", 0) or 0) for x in entries)) < 1e-6
    debit = {x["account"]: float(x.get("debit", 0) or 0) for x in entries if float(x.get("debit", 0) or 0)}
    credit = {x["account"]: float(x.get("credit", 0) or 0) for x in entries if float(x.get("credit", 0) or 0)}
    assert debit == {EXP_A: 40.0}
    assert credit == {"1130-P": 40.0}


@pytest.mark.asyncio
async def test_writeoff_partial_splits_child(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-PART", loc=loc, qty=10, cost_total=100)  # unit cost 10
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=3, account=EXP_A, comment="samples")
    await _finalize(client, t, wo)
    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    assert r.json()["value"] == 30.0

    # Parent lot stays available with the reduced quantity; a carved child lot is disposed at qty_out.
    parent = (await client.get(f"/items/{a}", headers=_h(t))).json()
    assert parent["status"] == "available" and float(parent["quantity"]) == 7.0
    disposed = [i for i in (await client.get("/items?status=disposed", headers=_h(t))).json()["items"]]
    assert len(disposed) == 1 and float(disposed[0]["quantity"]) == 3.0

    je = await _je_for(client, t, wo)
    entries = je["data"]["entries"]
    assert {x["account"] for x in entries if float(x.get("debit", 0) or 0)} == {EXP_A}
    assert {x["account"] for x in entries if float(x.get("credit", 0) or 0)} == {"1130-P"}


@pytest.mark.asyncio
async def test_writeoff_two_lines_same_item_one_balanced_je(client):
    """The same item on two lines with different accounts posts ONE JE: two expense debits against a
    single summed Inventory credit."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-DUP", loc=loc, qty=10, cost_total=100)  # unit cost 10
    wo = (await _writeoff(client, t, [a]))["id"]
    # Seeded line -> account A, qty 2; a second appended line (line_id omitted) -> account B, qty 3.
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=2, account=EXP_A, comment="spoiled")
    assert (await _set_line(client, t, wo, item_id=a, qty_out=3, account=EXP_B, comment="samples")).status_code == 200
    await _finalize(client, t, wo)
    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    assert r.json()["value"] == 50.0

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    jes = [e for e in ledger if wo in (e["data"].get("memo") or "")]
    assert len(jes) == 1  # exactly one JE for both lines
    entries = jes[0]["data"]["entries"]
    debit = {x["account"]: float(x.get("debit", 0) or 0) for x in entries if float(x.get("debit", 0) or 0)}
    credit = {x["account"]: float(x.get("credit", 0) or 0) for x in entries if float(x.get("credit", 0) or 0)}
    assert debit == {EXP_A: 20.0, EXP_B: 30.0}
    assert credit == {"1130-P": 50.0}  # one summed inventory credit
    assert abs(sum(debit.values()) - sum(credit.values())) < 1e-6


# --- validation (function level) -------------------------------------------

@pytest.mark.asyncio
async def test_writeoff_invalid_qty_and_account_rejected(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-VAL", loc=loc, qty=5, cost_total=50)
    wo = (await _writeoff(client, t, [a]))["id"]
    lid = await _line_id(client, t, wo, a)

    # qty_out invalid.
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=0, account=EXP_A)).status_code == 422
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=-1, account=EXP_A)).status_code == 422
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=6, account=EXP_A)).status_code == 422  # > on-hand

    # account invalid: nonexistent, wrong class (asset / revenue).
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=2, account="9999")).status_code == 422
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=2, account="1130-P")).status_code == 422
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=2, account="4100")).status_code == 422

    # valid combination accepted.
    assert (await _set_line(client, t, wo, line_id=lid, qty_out=2, account=EXP_A)).status_code == 200


# --- roles -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_writeoff_terminal_requires_manager(client, session):
    """The Write off stock terminal moves ledger value; it requires adjust_inventory (manager), exactly
    as the audit terminal does. An operator is rejected at the function level (403)."""
    t = await _register(client)
    operator = await _user_with_role(client, session, t, "operator")
    loc = await _location(client, t)
    a = await _item(client, t, "WO-ROLE", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    await _finalize(client, t, wo)
    assert (await _terminal(client, operator, wo)).status_code == 403


@pytest.mark.asyncio
async def test_writeoff_create_and_setline_require_edit_documents(client, session):
    """Carry-forward guard: both new list routes require edit_documents; a viewer (which lacks it) is
    rejected 403 on create_writeoff_list and on set_writeoff_line."""
    t = await _register(client)
    viewer = await _user_with_role(client, session, t, "viewer")
    loc = await _location(client, t)
    a = await _item(client, t, "WO-PERM", loc=loc, qty=4, cost_total=40)

    # create_writeoff_list: viewer 403.
    assert (await client.post("/lists/writeoff", headers=_h(viewer), json={"entity_ids": [a]})).status_code == 403

    # set_writeoff_line on an admin-created list: viewer 403.
    wo = (await _writeoff(client, t, [a]))["id"]
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=_h(viewer),
                          json={"item_id": a, "qty_out": 1, "account": EXP_A})
    assert r.status_code == 403


# --- weight/piece-tracked partial guard ------------------------------------

@pytest.mark.asyncio
async def test_writeoff_weighttracked_missing_weight_rejected(client):
    """A partial discard of a weight-tracked parcel with no discarded weight cannot be carved: the split
    primitive raises and the terminal returns 409 (nothing disposed, no JE)."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-WT", loc=loc, qty=10, cost_total=100, sell_by="gram")
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=3, account=EXP_A)
    await _finalize(client, t, wo)
    r = await _terminal(client, t, wo)
    assert r.status_code == 409, r.text
    # Nothing moved: no JE, item untouched.
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"


# --- seeding / empty selection ---------------------------------------------

@pytest.mark.asyncio
async def test_writeoff_empty_selection_rejected(client):
    t = await _register(client)
    r = await client.post("/lists/writeoff", headers=_h(t), json={"entity_ids": []})
    assert r.status_code == 422


# --- terminal idempotency (second run) -------------------------------------

@pytest.mark.asyncio
async def test_writeoff_second_terminal_rejected(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-2X", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    await _finalize(client, t, wo)
    assert (await _terminal(client, t, wo)).status_code == 200
    # A second terminal on the closed list is rejected; no duplicate JE, no double carve.
    assert (await _terminal(client, t, wo)).status_code == 409
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    assert len([e for e in ledger if wo in (e["data"].get("memo") or "")]) == 1


@pytest.mark.asyncio
async def test_writeoff_uncounted_lines_skipped_and_reported(client):
    """A seeded line left with qty_out unset is skipped (not disposed, no JE), counted in the reported
    skipped total; counted lines still post."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-CNT", loc=loc, qty=4, cost_total=40)
    b = await _item(client, t, "WO-SKIP", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a, b]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    # b left untouched (qty_out unset).
    await _finalize(client, t, wo)
    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    assert r.json()["written_off"] == 1 and r.json()["skipped"] == 1
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "disposed"
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["status"] == "available"  # untouched


# --- undo ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_writeoff_voids_je_and_restores_available(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-UNDO", loc=loc, qty=5, cost_total=50)
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=5, account=EXP_A)
    await _finalize(client, t, wo)
    assert (await _terminal(client, t, wo)).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "disposed"

    r = await client.post(f"/lists/{wo}/undo-write-off", headers=_h(t))
    assert r.status_code == 200, r.text
    # Disposed lot restored to available with its quantity; list reopened to finalized.
    restored = (await client.get(f"/items/{a}", headers=_h(t))).json()
    assert restored["status"] == "available" and float(restored["quantity"]) == 5.0
    st = await _state(client, t, wo)
    assert st["status"] == "finalized" and "result" not in st
    # The write-off JE is voided.
    je_events = [e for e in (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
                 if wo in (e.get("entity_id") or "")]
    assert any(e.get("event_type") == "acc.journal_entry.voided" for e in je_events)


# --- way back: draft deletable before the terminal -------------------------

@pytest.mark.asyncio
async def test_writeoff_draft_list_deletable_before_terminal(client):
    """J1 way back: before the terminal a seeded draft write-off list is deletable via the reused
    draft-only delete route, with no ledger effect."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-DEL", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    assert (await client.get(f"/lists/{wo}", headers=_h(t))).status_code == 200
    r = await client.delete(f"/lists/{wo}", headers=_h(t))
    assert r.status_code == 200, r.text
    assert (await client.get(f"/lists/{wo}", headers=_h(t))).status_code == 404
    # No ledger effect and the item is untouched.
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"


# --- disposed hidden from default list + inventory value -------------------

@pytest.mark.asyncio
async def test_disposed_excluded_from_default_and_inventory_value(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-HIDE", loc=loc, qty=5, cost_total=50)
    before = (await client.get("/items/valuation", headers=_h(t))).json()
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=5, account=EXP_A)
    await _finalize(client, t, wo)
    assert (await _terminal(client, t, wo)).status_code == 200

    # Hidden from the default list, present under the disposed filter.
    assert a not in {i["entity_id"] for i in (await client.get("/items", headers=_h(t))).json()["items"]}
    assert a in {i["entity_id"] for i in (await client.get("/items?status=disposed", headers=_h(t))).json()["items"]}
    # Excluded from active inventory value.
    after = (await client.get("/items/valuation", headers=_h(t))).json()
    assert after["active_item_count"] == before["active_item_count"] - 1
    assert abs(float(after["cost_total"]) - (float(before["cost_total"]) - 50.0)) < 1e-6


# --- audit-path regression around the JE-core extraction (pre-existing green) ---

@pytest.mark.asyncio
async def test_audit_adjustment_still_posts_after_refactor(client):
    """Guards the auto_je JE-core extraction: the audit adjustment still posts its shrinkage/overage JE
    through the shared core with the same accounts. Green at merge-base by definition (L3 corollary)."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "AUD-REG", loc=loc, qty=10, cost_total=100)  # unit cost 10
    audit = (await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})).json()["id"]
    await client.post(f"/lists/{audit}/finalize", headers=_h(t))
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    assert r.json()["shrinkage_value"] == 20.0
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if audit in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    assert {"5100", "1130-P"} <= {x["account"] for x in entries}
    assert abs(sum(float(x.get("debit", 0) or 0) for x in entries)
               - sum(float(x.get("credit", 0) or 0) for x in entries)) < 1e-6
