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

# The default shrinkage/write-off account seeded on every write-off line and posted by the audit
# terminal's shrinkage leg.
SHRINK_ACCT = "6970"


async def _company_id(session):
    """The single company's id (each test registers exactly one)."""
    from celerp_accounting.models import Account
    from sqlalchemy import select
    return (await session.execute(select(Account.company_id).limit(1))).scalar_one()


async def _delete_account(session, code: str) -> None:
    """Remove a chart account for the test's company (simulate a company whose COA lacks it)."""
    from celerp_accounting.models import Account
    from sqlalchemy import delete
    cid = await _company_id(session)
    await session.execute(delete(Account).where(Account.company_id == cid, Account.code == code))
    await session.commit()


async def _clear_line_account(session, wo_id: str, line_id: str) -> None:
    """Blank a seeded line's destination account (the set-line route never sets an account to None, so a
    'quantity entered, no account' line is built directly on the projection state)."""
    from celerp.models.projections import Projection
    from sqlalchemy import select
    cid = await _company_id(session)
    row = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_id == wo_id))).scalar_one()
    state = dict(row.state)
    lines = [dict(l) for l in state.get("line_items") or []]
    for l in lines:
        if l.get("line_id") == line_id:
            l["account"] = None
    state["line_items"] = lines
    row.state = state
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(row, "state")
    await session.commit()


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
    assert a not in {i["id"] for i in (await client.get("/items", headers=_h(t))).json()["items"]}

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


@pytest.mark.asyncio
async def test_writeoff_overdraw_across_lines_rejected(client):
    """The same item on two lines whose write-off quantities SUM above live stock is rejected: the
    aggregate-per-item check fires before any disposal, so the terminal returns 422, nothing is
    disposed, no JE posts, and the item stays available."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-OVERDRAW", loc=loc, qty=5, cost_total=50)  # unit cost 10
    wo = (await _writeoff(client, t, [a]))["id"]
    # Same item on two lines (two accounts), quantities 1 + 5 = 6 > 5 on hand.
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=1, account=EXP_A)
    assert (await _set_line(client, t, wo, item_id=a, qty_out=5, account=EXP_B)).status_code == 200
    await _finalize(client, t, wo)

    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("first, second", [(2, 3), (3, 2)])
async def test_writeoff_exact_exhaustion_disposes_parent_no_phantom(client, first, second):
    """The same item on two lines whose quantities exactly exhaust live stock (2 + 3 of 5, either
    order): the line consuming the remainder disposes the original row in place, so the item ends
    `disposed` with no phantom zero-quantity available parent, and the Inventory credit equals the
    item's full cost."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-EXACT", loc=loc, qty=5, cost_total=50)  # unit cost 10
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=first, account=EXP_A)
    assert (await _set_line(client, t, wo, item_id=a, qty_out=second, account=EXP_B)).status_code == 200
    await _finalize(client, t, wo)

    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    assert r.json()["written_off"] == 2 and r.json()["value"] == 50.0

    # The original item row ends disposed (no phantom 0-qty available parent): hidden from the default
    # list and present under the disposed filter.
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "disposed"
    assert a not in {i["id"] for i in (await client.get("/items", headers=_h(t))).json()["items"]}

    # One balanced JE crediting the full item cost to Inventory.
    je = await _je_for(client, t, wo)
    assert je is not None
    credit = {x["account"]: float(x.get("credit", 0) or 0) for x in je["data"]["entries"]
              if float(x.get("credit", 0) or 0)}
    assert credit == {"1130-P": 50.0}


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
    assert a not in {i["id"] for i in (await client.get("/items", headers=_h(t))).json()["items"]}
    assert a in {i["id"] for i in (await client.get("/items?status=disposed", headers=_h(t))).json()["items"]}
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
    assert {"6970", "1130-P"} <= {x["account"] for x in entries}
    assert abs(sum(float(x.get("debit", 0) or 0) for x in entries)
               - sum(float(x.get("credit", 0) or 0) for x in entries)) < 1e-6


# --- honest failure: an intended (qty'd) line that cannot dispose rejects the WHOLE action ---

@pytest.mark.asyncio
async def test_writeoff_qty_no_account_rejects(client, session):
    """A line with a quantity but no destination account rejects the whole terminal with an explanatory
    422: nothing is disposed, no JE posts, the item stays available and the list stays pre-terminal."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-NOACCT", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    # Quantity entered, but the seeded default account cleared so the line carries no destination.
    lid = await _line_id(client, t, wo, a)
    await _set_line(client, t, wo, line_id=lid, qty_out=4)
    await _clear_line_account(session, wo, lid)
    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert "account" in r.json()["detail"].lower()
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"
    st = await _state(client, t, wo)
    assert st.get("result") != "written_off" and st["status"] != "closed"


@pytest.mark.asyncio
async def test_writeoff_all_blank_rejects(client):
    """Every line blank (nothing to write off) rejects with the explanatory 422; no JE, nothing disposed,
    list stays pre-terminal."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-BLANK", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert "Enter a quantity and a destination account" in r.json()["detail"]
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"


@pytest.mark.asyncio
async def test_writeoff_multiline_atomic_rejects(client, session):
    """One fully-entered line plus one intended-but-incomplete line (qty, no account): the WHOLE action
    rejects. The complete line's item is NOT disposed, no JE posts, the list stays pre-terminal."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-OK", loc=loc, qty=4, cost_total=40)
    b = await _item(client, t, "WO-BAD", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a, b]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    # b: quantity entered but its account cleared -> intended but incomplete.
    b_lid = await _line_id(client, t, wo, b)
    await _set_line(client, t, wo, line_id=b_lid, qty_out=4)
    await _clear_line_account(session, wo, b_lid)
    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert await _je_for(client, t, wo) is None
    # a stays available (nothing disposed), list stays pre-terminal.
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["status"] == "available"
    st = await _state(client, t, wo)
    assert st.get("result") != "written_off" and st["status"] != "closed"


# --- single step: the terminal accepts a DRAFT and finalizes+disposes atomically ---

@pytest.mark.asyncio
async def test_writeoff_single_step_finalizes_and_disposes(client):
    """One click of the terminal on a DRAFT write-off finalizes, disposes the stock, posts the JE and
    closes the list - no separate finalize step."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-1STEP", loc=loc, qty=4, cost_total=40)  # unit cost 10
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    # No finalize call: the terminal runs straight from DRAFT.
    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    assert r.json()["written_off"] == 1 and r.json()["value"] == 40.0
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "disposed"
    st = await _state(client, t, wo)
    assert st["status"] == "closed" and st["result"] == "written_off"
    je = await _je_for(client, t, wo)
    assert je is not None
    debit = {x["account"] for x in je["data"]["entries"] if float(x.get("debit", 0) or 0)}
    assert EXP_A in debit


@pytest.mark.asyncio
async def test_writeoff_default_line_account_is_6970(client):
    """A seeded write-off line defaults its destination account to 6970; a default (unedited-account)
    write-off debits 6970."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-DEF", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    # The seeded line carries account 6970 without any edit.
    lines = (await _state(client, t, wo))["line_items"]
    assert next(l for l in lines if l["item_id"] == a)["account"] == SHRINK_ACCT
    # Enter only the quantity (leave the default account) and run the single-step terminal.
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4)
    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    je = await _je_for(client, t, wo)
    debit = {x["account"] for x in je["data"]["entries"] if float(x.get("debit", 0) or 0)}
    assert debit == {SHRINK_ACCT}


@pytest.mark.asyncio
async def test_writeoff_line_account_override(client):
    """On a DRAFT write-off, overriding the seeded 6970 to another valid expense account and clicking the
    terminal once posts the JE to the overridden account."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-OVR", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    r = await _terminal(client, t, wo)
    assert r.status_code == 200, r.text
    je = await _je_for(client, t, wo)
    debit = {x["account"] for x in je["data"]["entries"] if float(x.get("debit", 0) or 0)}
    assert debit == {EXP_A}  # overridden account, not the 6970 default


@pytest.mark.asyncio
async def test_writeoff_account_absent_rejects(client, session):
    """A company whose COA lacks 6970: a qty'd line carrying the 6970 default reaches the terminal ->
    422, no JE, the item stays available (never a false success posting to a missing account)."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-ABSENT", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    lid = await _line_id(client, t, wo, a)
    await _set_line(client, t, wo, line_id=lid, qty_out=4)  # keep the seeded 6970 default account
    await _delete_account(session, SHRINK_ACCT)  # this company no longer has 6970
    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"


@pytest.mark.asyncio
async def test_writeoff_item_gone_rejects(client):
    """A qty'd line whose item is no longer available -> the WHOLE action rejects 422, nothing disposed,
    no JE."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-GONE", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo, line_id=await _line_id(client, t, wo, a), qty_out=4, account=EXP_A)
    # The item leaves availability before the terminal (disposed via its own write-off).
    wo0 = (await _writeoff(client, t, [a]))["id"]
    await _set_line(client, t, wo0, line_id=await _line_id(client, t, wo0, a), qty_out=4, account=EXP_A)
    assert (await _terminal(client, t, wo0)).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "disposed"
    # Now the first list's line points at a no-longer-available item.
    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert await _je_for(client, t, wo) is None


@pytest.mark.asyncio
async def test_writeoff_qty_exceeds_stock_rejects(client, session):
    """A line whose qty_out exceeds live stock -> the WHOLE action rejects 422, nothing disposed, no
    JE. (set-line guards qty<=on_hand, so the over-qty is written straight onto the projection.)"""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "WO-OVERQTY", loc=loc, qty=4, cost_total=40)
    wo = (await _writeoff(client, t, [a]))["id"]
    lid = await _line_id(client, t, wo, a)
    await _set_line(client, t, wo, line_id=lid, qty_out=4, account=EXP_A)
    # Force qty_out above live stock directly on the projection (set-line would reject qty > on_hand).
    from celerp.models.projections import Projection
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    cid = await _company_id(session)
    row = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_id == wo))).scalar_one()
    state = dict(row.state)
    lines = [dict(l) for l in state.get("line_items") or []]
    for l in lines:
        if l.get("line_id") == lid:
            l["qty_out"] = 99
    state["line_items"] = lines
    row.state = state
    flag_modified(row, "state")
    await session.commit()
    r = await _terminal(client, t, wo)
    assert r.status_code == 422, r.text
    assert await _je_for(client, t, wo) is None
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["status"] == "available"


# --- audit shrinkage now posts to 6970, and guards a company lacking it ---

@pytest.mark.asyncio
async def test_audit_shrinkage_missing_account_rejects(client, session):
    """Audit shrinkage on a company whose COA lacks 6970 -> 422, no phantom-account shrinkage JE."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "AUD-NO6970", loc=loc, qty=10, cost_total=100)
    await _delete_account(session, SHRINK_ACCT)  # remove the shrinkage destination
    audit = (await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})).json()["id"]
    await client.post(f"/lists/{audit}/finalize", headers=_h(t))
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 422, r.text
    # No shrinkage JE posted to a missing account.
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    assert not [e for e in ledger if audit in (e["data"].get("memo") or "")]
