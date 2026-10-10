"""Goods an invoice has set aside stay set aside on the older paths into stock: an invoice
finalized before cost snapshots existed, a lot taken out of stock by a type change, a
credit note against an unshipped invoice, a CSV import refused by the hold. The hold check
reads only the invoices that name the lot, runs under the company lock, and every writer
of projections either goes through it or cannot take goods an invoice holds."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.lot_origin import RECORDED
from stock_books import assert_settled
from test_consignment_in_sale import _consign
from test_cost_follows_goods import _doc_number, _invoice, _ship
from test_cost_restatement import _state
from test_invoice_unshipped_books import _lot
from test_set_aside_goods_every_exit import _refused

pytestmark = pytest.mark.asyncio


async def _held(session, auth, lot: str) -> dict[str, float]:
    """{invoice number: quantity} the lot holds for invoices."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})
    return (await auto_je.set_aside(session, auth["company_id"], [row])).get(lot, {})


async def _net(session, auth, account: str, prefix: str = "je:auto:") -> float:
    """Debit less credit on ``account`` across posted entries whose id starts with ``prefix``."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"{prefix}%")))).scalars().all()
    return round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
                     for p in rows if (p.state or {}).get("status") == "posted"
                     for e in p.state.get("entries", []) if e.get("account") == account), 2)


async def _strip_snapshot(session, auth, doc: str) -> None:
    """Make ``doc`` an invoice finalized before cost snapshots existed: its finalize entry
    carries no cost allocations, which is exactly what such an invoice has."""
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.event_type == "acc.journal_entry.created",
        LedgerEntry.entity_id.like(f"je:auto:{doc}:fin%")))).scalars().all()
    assert rows
    for r in rows:
        md = dict(r.metadata_ or {})
        md.pop("cogs_allocations", None)
        r.metadata_ = md
        flag_modified(r, "metadata_")
    await session.commit()
    assert await auto_je.recognized_cogs(session, auth["company_id"], doc) is None


async def _unrecorded(session, auth, lot: str) -> None:
    """A lot created before lots recorded their inventory account: no record of one, in
    its state or its history."""
    await session.execute(delete(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == lot,
        LedgerEntry.event_type == RECORDED))
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})
    st = dict(row.state)
    st.pop("inventory_account_code", None)
    row.state = st
    flag_modified(row, "state")
    await session.commit()


async def _imported_lot(client, auth, sku: str, qty: float, cost: float) -> str:
    eid = f"item:{uuid.uuid4()}"
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"upsert": True, "records": [
        {"entity_id": eid, "event_type": "item.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
         "data": {"sku": sku, "name": "Lot", "quantity": qty, "sell_by": "piece", "status": "available",
                  "cost_total": cost, "inventory_type": "stocked"}}]})
    assert r.status_code == 200 and not r.json().get("errors"), r.text
    return eid


def _csv_row(lot: str, data: dict) -> dict:
    return {"entity_id": lot, "event_type": "item.patched", "data": data, "source": "csv",
            "idempotency_key": f"older-{uuid.uuid4().hex}"}


async def _csv(client, auth, *rows: dict):
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"upsert": True, "records": list(rows)})
    assert r.status_code == 200, r.text
    return r.json()


def _rejected_naming(body: dict, number: str, sku: str) -> None:
    """Every refused row is counted rejected (skipped) and names the invoice and the row's SKU."""
    assert body["errors"] and body["skipped"] == len(body["errors"]), body
    for e in body["errors"]:
        assert f"SKU={sku}" in e["message"] and number in e["message"], body


async def _credit_note(client, auth, invoice: str, lot: str, sku: str, qty: float, *, tax: float = 0.0) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": invoice, "total": 40.0 * qty + tax, "tax": tax,
        "line_items": [{"name": "Lot", "sku": sku, "entity_id": lot, "quantity": qty, "unit_price": 40.0,
                        "line_total": 40.0 * qty, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    f = await client.post(f"/docs/{cn}/finalize", headers=auth["headers"], json={})
    assert f.status_code == 200, f.text
    return cn


# Invoices finalized before cost snapshots


async def test_an_invoice_without_a_snapshot_cannot_ship_goods_another_invoice_holds(client, session, auth):
    sku = f"OLD-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    a = await _invoice(client, auth, [(lot, sku, 2)])
    b = await _invoice(client, auth, [(lot, sku, 3)])
    await _strip_snapshot(session, auth, b)
    r = await client.post(f"/docs/{b}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    await _refused(session, auth, r, a, None)
    assert float((await _state(session, auth, lot))["quantity"]) == 3
    # A keeps its 2; B, holding at no recorded cost, has the one unit that was free.
    assert await _held(session, auth, lot) == {await _doc_number(session, auth, a): 2.0,
                                               await _doc_number(session, auth, b): 1.0}
    assert await _net(session, auth, "5100") == 30.0

    # Once the holder is voided the goods are free and leave once. (Stock against books is
    # not checked here: this invoice recognized cost only for the unit free when it was
    # finalized, which an invoice from before snapshots never did.)
    assert (await client.post(f"/docs/{a}/void", headers=auth["headers"], json={})).status_code == 200
    await _ship(client, auth, b, lot)
    assert await _net(session, auth, "5100") <= 30.0


async def test_an_invoice_cannot_claim_units_an_older_invoice_without_a_snapshot_holds(client, session, auth):
    sku = f"OLD-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    b = await _invoice(client, auth, [(lot, sku, 3)])
    await _strip_snapshot(session, auth, b)
    a = await _invoice(client, auth, [(lot, sku, 2)])
    held = await _held(session, auth, lot)
    assert held == {await _doc_number(session, auth, b): 3.0}, held
    await _ship(client, auth, b, lot)
    assert await _net(session, auth, "5100") <= 30.0
    assert await _held(session, auth, lot) == {}
    assert (await client.post(f"/docs/{a}/void", headers=auth["headers"], json={})).status_code == 200
    await assert_settled(client, session, auth)


# Taking a lot out of stock by changing its type


@pytest.mark.parametrize("via", ["patch", "csv"])
@pytest.mark.parametrize("origin", ["unrecorded", "recorded", "imported"])
async def test_a_held_lot_cannot_become_a_service(client, session, auth, via, origin):
    sku = f"TYP-{uuid.uuid4().hex[:4]}"
    lot = (await _imported_lot(client, auth, sku, 3, 30.0) if origin == "imported"
           else await _lot(client, auth, sku, 3, 30.0))
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    if origin == "unrecorded":
        await _unrecorded(session, auth, lot)
    number = await _doc_number(session, auth, inv)
    before = await _held(session, auth, lot)
    if via == "patch":
        r = await client.patch(f"/items/{lot}", headers=auth["headers"], json={
            "fields_changed": {"inventory_type": {"old": "stocked", "new": "service"}}})
        assert r.status_code in (409, 422), r.text
        await session.rollback()
        if r.status_code == 409:
            assert number in r.json()["detail"], r.text
    else:
        _rejected_naming(await _csv(client, auth, _csv_row(lot, {"inventory_type": "service"})), number, sku)
        await session.rollback()
    st = await _state(session, auth, lot)
    assert st.get("inventory_type") == "stocked"
    assert await _held(session, auth, lot) == before
    t = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 0})
    await _refused(session, auth, t, inv, 2)


@pytest.mark.parametrize("via", ["patch", "csv"])
async def test_a_held_consigned_lot_keeps_its_consignment(client, session, auth, via):
    _c, lot = await _consign(client, session, auth, qty=3, unit_price=10.0)
    sku = (await _state(session, auth, lot))["sku"]
    await _invoice(client, auth, [(lot, sku, 2)])
    if via == "patch":
        await client.patch(f"/items/{lot}", headers=auth["headers"], json={
            "fields_changed": {"consignment_flag": {"old": "in", "new": None}}})
    else:
        await _csv(client, auth, _csv_row(lot, {"consignment_flag": None}))
    await session.rollback()
    assert (await _state(session, auth, lot)).get("consignment_flag") == "in"
    assert sum((await _held(session, auth, lot)).values()) == 2
    await assert_settled(client, session, auth)


# Credit notes against an unshipped invoice


async def test_a_credit_note_posts_and_releases_the_credited_units(client, session, auth):
    sku = f"CRN-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    number = await _doc_number(session, auth, inv)
    cn = await _credit_note(client, auth, inv, lot, sku, 1)
    assert await _net(session, auth, "4100", f"je:auto:{cn}:") == 40.0
    assert await _net(session, auth, "1120", f"je:auto:{cn}:") == -40.0
    assert await _net(session, auth, "1120") == 40.0
    assert float((await _state(session, auth, inv))["amount_outstanding"]) == 40.0
    assert await _held(session, auth, lot) == {number: 1.0}
    # The credited unit is free again, the other one is still held.
    assert (await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 1})).status_code == 200
    t = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 0})
    await _refused(session, auth, t, inv, 1)
    await assert_settled(client, session, auth)


async def test_a_credit_note_credits_output_tax(client, session, auth):
    sku = f"CRT-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    cn = await _credit_note(client, auth, inv, lot, sku, 1, tax=4.0)
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{cn}:fin"})
    lines = {(e["account"], float(e.get("debit") or 0), float(e.get("credit") or 0)) for e in row.state["entries"]}
    assert ("4100", 40.0, 0.0) in lines and ("1120", 0.0, 44.0) in lines, lines
    assert sum(d for _a, d, _c in lines) == sum(c for _a, _d, c in lines) == 44.0, lines


async def test_voiding_a_credit_note_holds_the_units_again(client, session, auth):
    sku = f"CRV-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    number = await _doc_number(session, auth, inv)
    cn = await _credit_note(client, auth, inv, lot, sku, 1)
    assert (await client.post(f"/docs/{cn}/void", headers=auth["headers"], json={})).status_code == 200
    assert await _held(session, auth, lot) == {number: 2.0}
    assert await _net(session, auth, "1120") == 80.0
    await assert_settled(client, session, auth)


async def test_a_credit_note_cannot_be_voided_once_its_units_have_left(client, session, auth):
    sku = f"CRG-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 2, 20.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    number = await _doc_number(session, auth, inv)
    cn = await _credit_note(client, auth, inv, lot, sku, 1)
    assert (await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 1})).status_code == 200
    r = await client.post(f"/docs/{cn}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    await session.rollback()
    assert number in str(r.json()["detail"]), r.text
    assert (await _state(session, auth, cn)).get("status") != "void"
    await assert_settled(client, session, auth)


# CSV rows the hold refuses


async def test_csv_rows_the_hold_refuses_are_rejected_by_sku(client, session, auth):
    sku = f"CSV-{uuid.uuid4().hex[:4]}"
    held = await _lot(client, auth, sku, 3, 30.0)
    free = await _lot(client, auth, sku + "F", 3, 30.0)
    inv = await _invoice(client, auth, [(held, sku, 2)])
    number = await _doc_number(session, auth, inv)
    body = await _csv(client, auth,
                      _csv_row(free, {"name": "Renamed free", "quantity": 1}),
                      _csv_row(held, {"name": "Renamed held", "quantity": 0}),
                      _csv_row(held, {"quantity": 2}),
                      _csv_row(held, {"quantity": 1}))
    assert body["updated"] == 2 and len(body["errors"]) == 2, body
    _rejected_naming(body, number, sku)
    h, f = await _state(session, auth, held), await _state(session, auth, free)
    assert (h["name"], float(h["quantity"])) == ("Lot", 2.0)
    assert (f["name"], float(f["quantity"])) == ("Renamed free", 1.0)
    await assert_settled(client, session, auth)


# The hold check reads only the invoices naming the lot


async def test_the_hold_check_reads_only_invoices_naming_the_lot(client, session, auth, monkeypatch):
    sku = f"BND-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 10, 100.0)
    inv = await _invoice(client, auth, [(lot, sku, 1)])
    for i in range(4):
        other = await _lot(client, auth, f"{sku}-{i}", 2, 2.0)
        await _invoice(client, auth, [(other, f"{sku}-{i}", 1)])
    read: list[set[str]] = []
    real = auto_je._read_books

    async def counting(session_, company_id, doc_ids, *args, **kwargs):
        read.append(set(doc_ids))
        return await real(session_, company_id, doc_ids, *args, **kwargs)

    monkeypatch.setattr(auto_je, "_read_books", counting)
    r = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 5})
    assert r.status_code == 200, r.text
    assert read and set().union(*read) == {inv}, read


# The company lock comes first


async def test_every_change_to_an_existing_item_takes_the_company_lock(client, session, auth, monkeypatch):
    """Whatever the change looks like before it is applied, the company lock comes before
    the row lock the apply takes, so a change that turns out to take goods is judged in order."""
    from celerp.events.engine import emit_event
    from celerp.services import company_lock

    lot = await _lot(client, auth, f"LCK-{uuid.uuid4().hex[:4]}", 3, 30.0)
    taken: list = []
    real = company_lock.lock_company

    async def recording(session_, company_id):
        taken.append(company_id)
        return await real(session_, company_id)

    monkeypatch.setattr(company_lock, "lock_company", recording)
    await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                     event_type="item.patched", data={"name": "Renamed"}, actor_id=auth["user_id"],
                     location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    assert taken, "an item change ran without the company lock"


# Every writer of projections is accounted for

# A projection written outside emit_event never passes the hold check, so each one must
# be unable to take goods an invoice holds. A new writer fails this test until it is
# routed through emit_event or added here with the reason it cannot.
_WRITERS = {
    ("celerp/projections/engine.py", "Projection("): "the apply core every event goes through",
    ("celerp/projections/engine.py", "await session.execute(delete(Projection) if company_id is None else "
     "delete(Projection).where(Projection.company_id == company_id))"): "rebuild from the ledger, whose events were each judged when written",
    ("celerp/events/engine.py", "transition = await ProjectionEngine.apply_event(session, entry)"): "emit_event, the hold check itself",
    ("celerp/routers/ledger.py", "await ProjectionEngine.rebuild(session, company_id=company_id)"): "rebuild from the judged ledger",
    ("celerp/services/company_backup.py", "await ProjectionEngine.rebuild(session, company_id)"): "whole-company restore",
    ("celerp/services/dev_release_guard.py", "await ProjectionEngine.rebuild(session)"): "rebuild from the judged ledger",
    ("default_modules/celerp-admin/celerp_admin/routes.py", "await ProjectionEngine.rebuild(session, company_id=company_id)"): "rebuild from the judged ledger",
    ("default_modules/celerp-admin/celerp_admin/routes.py", "proj.state = replayed"): "stale projection replayed from the judged ledger",
    ("celerp/connectors/outbound_queue.py", "self.state = state"): "an outbound delivery's own state, not a projection",
    ("celerp/routers/companies.py", "row.state = new_state"): "category rename, changes only the category",
    ("celerp/services/status_doc_backfill.py", "proj.state = new_state"): "writes only status_doc_id and status_doc_number",
    ("default_modules/celerp-docs/celerp_docs/legacy_receipts.py", "row.state = state"): "bill receipt fields; bills hold nothing",
    ("default_modules/celerp-docs/celerp_docs/received_legacy.py", "await ProjectionEngine.apply_event(session, entry)"): "imported received documents, not items",
    ("celerp/services/item_erasure.py", "await session.execute(sa.delete(Projection).where("): "erases only items no document line names",
    ("default_modules/celerp-docs/celerp_docs/routes.py", "await session.execute(_sa.delete(Projection).where("
     "Projection.company_id == company_id, Projection.entity_id == eid))"): "deletes draft documents",
    ("default_modules/celerp-docs/celerp_docs/routes.py", "await session.execute(_sa.delete(Projection).where("
     "Projection.company_id == company_id, Projection.entity_id == entity_id))"): "deletes draft documents and lists",
}
_WRITE = re.compile(r"\.state = |\.state\[[^]]+\] = |(?<![\w.])Projection\(|ProjectionEngine\.(apply_event|rebuild)\(|"
                    r"(insert|update|delete)\(Projection\)")


async def test_every_projection_writer_is_accounted_for():
    root = Path(__file__).resolve().parents[1]
    found: dict[tuple[str, str], int] = {}
    for base in ("celerp", "default_modules"):
        for path in sorted((root / base).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                code = line.strip()
                if code.startswith(("#", "class ")) or not _WRITE.search(code):
                    continue
                found[(rel, code)] = found.get((rel, code), 0) + 1
    assert set(found) == set(_WRITERS), {
        "new": sorted(set(found) - set(_WRITERS)), "gone": sorted(set(_WRITERS) - set(found))}
