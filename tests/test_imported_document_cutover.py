# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Purchase orders and bills imported with their value already in the opening balances.

The opening balances are authoritative and an imported document is detail only: the
import posts nothing for a purchase order or bill, since the goods received on it are
in opening stock and what is owed on it is in opening payables. Only what happens on
the document after the import posts: receipts, returns, payments, voids and reverts.

Every step of every flow checks two things: the lot accounts carry exactly the stock
recorded on them (plus the goods billed and not yet received, which a bill carries in
transit), and accounts payable holds exactly what the documents still owe.

A company that imported under the earlier behaviour, where the import booked the
document's whole total again, is repaired once: the entries the import itself posted
are reversed and the books end where they would have been.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from celerp.accounting_roles import AccountRole as R
from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from stock_books import assert_books_carry_stock
from test_cost_restatement import _item, _state
from test_receipt_accounting import _OPENING, _books

pytestmark = pytest.mark.asyncio

PRICE = 14.0
GOODS, IN_TRANSIT, AP = "1130-OB", "1130-P", "2110"
FIN, VOID, UNVOID, REV = "/docs/{d}/finalize", "/docs/{d}/void", "/docs/{d}/unvoid", "/docs/{d}/revert-to-draft"
RCV, RET, PAY = "/docs/{d}/receive", "/docs/{d}/return-items", "/docs/{d}/payment"


def _line_price(st: dict) -> dict[str, float]:
    return {li.get("item_id"): float(li.get("unit_price") or 0) for li in st.get("line_items") or []}


def _payable(st: dict) -> float:
    """What the document still owes the supplier. A bill's outstanding already nets its
    supplier returns (returned_credit); an order owes what it received less returns."""
    if st.get("status") in ("void", "draft"):
        return 0.0
    if st.get("doc_type") == "bill":
        return float(st.get("amount_outstanding") or 0)
    price = _line_price(st)
    returned = sum(float(x.get("quantity_returned") or 0) * price.get(x.get("item_id"), 0)
                   for x in st.get("returned_items") or [])
    received = sum(float(x.get("quantity_received") or 0) * price.get(x.get("item_id"), 0)
                   for x in st.get("received_items") or [])
    return received - returned - float(st.get("amount_paid") or 0)


def _in_transit(st: dict) -> Decimal:
    """Goods a live bill has booked and not yet received."""
    if st.get("doc_type") != "bill" or st.get("status") in ("void", "draft"):
        return Decimal("0")
    owed = Decimal("0")
    for index, li in enumerate(st.get("line_items") or []):
        got = sum(float(x.get("quantity_received") or 0) for x in st.get("received_items") or []
                  if int(x.get("po_line_index", -1)) == index)
        owed += Decimal(str(round((float(li.get("quantity") or 0) - got) * float(li.get("unit_price") or 0), 2)))
    return owed


async def _check(session, auth, docs: list[str], step: str) -> None:
    """Books carry the stock and payables are what the documents owe."""
    states = [await _state(session, auth, d) for d in docs]
    transit = sum((_in_transit(st) for st in states), Decimal("0"))
    try:
        await assert_books_carry_stock(session, auth["company_id"], in_transit={IN_TRANSIT: transit})
    except AssertionError as exc:
        raise AssertionError(f"{step}: {exc}") from None
    owed = sum(_payable(st) for st in states)
    books = await _books(session, auth, AP)
    assert abs(owed + float(books[AP])) < 1e-6, f"{step}: payables {books[AP]} != -{owed} owed"


async def _step(client, session, auth, docs, name, method, path, expect=200, **kw):
    r = await getattr(client, method)(path.format(d=docs[0]), headers=auth["headers"], **kw)
    assert r.status_code == expect, f"{name}: {r.status_code} {r.text}"
    await _check(session, auth, docs, name)
    return r


async def _doc_jes(session, auth, doc: str) -> dict[str, dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(f"je:auto:{doc}:", autoescape=True)))).scalars().all()
    return {r.entity_id[len(f"je:auto:{doc}:"):]: r.state for r in rows}


async def _opening(client, auth, owed: float, in_transit: float = 0.0) -> None:
    """The opening balances: what the imported documents owe, and any goods billed in transit."""
    entries = [{"account": AP, "credit": owed}]
    if in_transit:
        entries.append({"account": IN_TRANSIT, "debit": in_transit})
    if owed - in_transit:
        entries.append({"account": "3200", "debit": owed - in_transit})
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-01-01", "memo": "Opening balances", "idempotency_token": uuid.uuid4().hex, "entries": entries})
    assert r.status_code == 200, r.text


def _snapshot(lot: str, doc_type: str, qty: int, received: int, paid: float = 0.0,
              treatment: str | None = "opening_balances") -> dict:
    """The document as imported. Its value is in the opening balances, so it is imported
    with that treatment; ``treatment=None`` is a document as an earlier release stored it."""
    total = PRICE * qty
    if doc_type == "purchase_order":
        status = "received" if received >= qty else "partially_received"
    else:
        status = "awaiting_payment"
    return {"doc_type": doc_type, "contact_id": "supplier:1", "status": status,
            "doc_number": f"IMP-{uuid.uuid4().hex[:6]}", "issue_date": "2026-01-01",
            "line_items": [{"item_id": lot, "name": "Lot", "quantity": qty, "unit_price": PRICE,
                            "line_id": str(uuid.uuid4())}],
            "subtotal": total, "total": total, "amount_outstanding": total - paid, "amount_paid": paid,
            "received_items": [{"item_id": lot, "po_line_index": 0, "quantity_received": float(received),
                                "receive_as": "stock"}] if received else [],
            "received_item_ids": [], **({"import_treatment": treatment} if treatment else {})}


def _owed(doc_type: str, qty: int, received: int) -> tuple[float, float]:
    """What the opening balances hold for the document: payables, and goods in transit."""
    if doc_type == "purchase_order":
        return PRICE * received, 0.0
    return PRICE * qty, PRICE * (qty - received)


async def _import(client, auth, lot, doc_type="purchase_order", qty=5, received=5) -> str:
    doc = f"doc:{uuid.uuid4()}"
    await _opening(client, auth, *_owed(doc_type, qty, received))
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc, "event_type": "doc.created", "source": "test",
        "idempotency_key": uuid.uuid4().hex, "data": _snapshot(lot, doc_type, qty, received)})
    assert r.status_code == 200, r.text
    return doc


def _receive(qty: int, lot: str) -> dict:
    return {"json": {"location_id": "", "received_items": [
        {"po_line_index": 0, "item_id": lot, "quantity_received": qty, "receive_as": "stock"}]}}


def _ret(qty: int, lot: str) -> dict:
    return {"json": {"items": [{"item_id": lot, "quantity_returned": qty}]}}


def _pay(amount: float) -> dict:
    return {"json": {"amount": amount, "payment_date": "2026-02-01", "bank_account": "1111"}}


async def _lot(session, auth, lot) -> tuple[float, float]:
    st = await _state(session, auth, lot)
    return float(st["quantity"]), float(st["cost_total"])


# --- imported documents --------------------------------------------------------------


async def test_imported_po_fully_received_posts_nothing_and_settles(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _import(client, auth, lot)
    await _check(session, auth, [po], "imported")
    assert await _doc_jes(session, auth, po) == {}
    await _step(client, session, auth, [po], "return 2", "post", RET, **_ret(2, lot))
    assert await _lot(session, auth, lot) == (8.0, _OPENING - 2 * PRICE)
    await _step(client, session, auth, [po], "finalize", "post", FIN)
    assert not [s for s, je in (await _doc_jes(session, auth, po)).items()
                if s.startswith("bill") and je.get("status") == "posted"]
    await _step(client, session, auth, [po], "pay 20", "post", PAY, **_pay(20.0))


async def test_imported_po_partly_received_then_receive_the_rest(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _import(client, auth, lot, qty=10, received=4)
    await _check(session, auth, [po], "imported")
    await _step(client, session, auth, [po], "receive 6", "post", RCV, **_receive(6, lot))
    assert await _lot(session, auth, lot) == (16.0, _OPENING + 6 * PRICE)
    await _step(client, session, auth, [po], "finalize", "post", FIN)
    await _step(client, session, auth, [po], "return 3", "post", RET, **_ret(3, lot))


async def test_imported_po_finalized_before_the_rest_arrives(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _import(client, auth, lot, qty=10, received=4)
    await _step(client, session, auth, [po], "finalize", "post", FIN)
    posted = {s: je for s, je in (await _doc_jes(session, auth, po)).items() if je.get("status") == "posted"}
    assert sum(float(e.get("credit") or 0) for je in posted.values() for e in je["entries"]
               if e["account"] == AP) == 6 * PRICE
    await _step(client, session, auth, [po], "receive 6", "post", RCV, **_receive(6, lot))


async def test_imported_po_mixed_with_own_receipts(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _import(client, auth, lot, qty=10, received=4)
    await _step(client, session, auth, [po], "receive 3", "post", RCV, **_receive(3, lot))
    await _step(client, session, auth, [po], "return 5", "post", RET, **_ret(5, lot))
    assert await _lot(session, auth, lot) == (8.0, _OPENING + 3 * PRICE - 5 * PRICE)
    await _step(client, session, auth, [po], "finalize", "post", FIN)


async def test_imported_bill_received_returns_and_pays(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _import(client, auth, lot, doc_type="bill")
    await _check(session, auth, [bill], "imported")
    assert await _doc_jes(session, auth, bill) == {}
    await _step(client, session, auth, [bill], "return 1", "post", RET, **_ret(1, lot))
    # A document with returns takes no payment (natively too), so pay a second import.
    paid = await _import(client, auth, lot, doc_type="bill")
    await _step(client, session, auth, [paid, bill], "pay 30", "post", PAY, **_pay(30.0))
    await _step(client, session, auth, [paid, bill], "void", "post", VOID, expect=409, json={})


async def test_imported_bill_not_received_voids_unvoids_reverts(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _import(client, auth, lot, doc_type="bill", received=0)
    await _check(session, auth, [bill], "imported")
    await _step(client, session, auth, [bill], "void", "post", VOID, json={})
    await _step(client, session, auth, [bill], "unvoid", "post", UNVOID, json={})
    await _step(client, session, auth, [bill], "void again", "post", VOID, json={})
    await _step(client, session, auth, [bill], "unvoid again", "post", UNVOID, json={})
    await _step(client, session, auth, [bill], "revert", "post", REV, json={})
    await _step(client, session, auth, [bill], "finalize", "post", FIN)
    await _step(client, session, auth, [bill], "receive 5", "post", RCV, **_receive(5, lot))


async def test_imported_bill_not_received_receive_then_undo(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _import(client, auth, lot, doc_type="bill", received=0)
    await _step(client, session, auth, [bill], "receive 5", "post", RCV, **_receive(5, lot))
    await _step(client, session, auth, [bill], "undo receipt", "delete", RCV)
    await _step(client, session, auth, [bill], "void", "post", VOID, json={})


async def test_undoing_goods_received_before_the_import_is_refused(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _import(client, auth, lot, doc_type="bill")
    r = await _step(client, session, auth, [bill], "undo receipt", "delete", RCV, expect=409)
    assert "already in stock when" in r.json()["detail"]["message"]
    assert await _lot(session, auth, lot) == (10.0, _OPENING)


async def test_batch_import_posts_nothing_for_orders_and_bills(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po, bill = f"doc:{uuid.uuid4()}", f"doc:{uuid.uuid4()}"
    await _opening(client, auth, PRICE * 4 + PRICE * 5)
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [
        {"entity_id": po, "event_type": "doc.created", "source": "test", "idempotency_key": uuid.uuid4().hex,
         "data": _snapshot(lot, "purchase_order", 10, 4)},
        {"entity_id": bill, "event_type": "doc.created", "source": "test", "idempotency_key": uuid.uuid4().hex,
         "data": _snapshot(lot, "bill", 5, 5)},
    ]})
    assert r.status_code == 200 and r.json()["created"] == 2, r.text
    assert await _doc_jes(session, auth, po) == {} and await _doc_jes(session, auth, bill) == {}
    await _check(session, auth, [po, bill], "imported")
    await _step(client, session, auth, [po, bill], "return 2", "post", RET, **_ret(2, lot))


# --- the repair for documents imported under the earlier behaviour ------------------


async def _legacy(client, session, auth, lot, doc_type="purchase_order", qty=10, received=4) -> str:
    """An imported document as the earlier release left it: the import booked the whole
    total again on top of the opening balances, and its goods carry no receipt record."""
    doc = f"doc:{uuid.uuid4()}"
    await _opening(client, auth, *_owed(doc_type, qty, received))
    cid, uid = auth["company_id"], auth["user_id"]
    await emit_event(session, company_id=cid, entity_id=doc, entity_type="doc", event_type="doc.created",
                     data=_snapshot(lot, doc_type, qty, received, treatment=None), actor_id=uid, location_id=None,
                     source="test", idempotency_key=uuid.uuid4().hex,
                     metadata_={auto_je.IMPORTED_SNAPSHOT: True})
    if doc_type == "purchase_order":
        await auto_je._post_po_receipt(session, company_id=cid, user_id=uid, po_id=doc, receipt_key=None,
                                       debits={R.INVENTORY_PURCHASED: PRICE * qty}, receive_date="2026-01-01")
    else:
        await auto_je._emit_auto_posted_je(
            session, company_id=cid, user_id=uid, je_id=f"je:auto:{doc}:bill",
            idem_create=auto_je.je_idempotency_key(doc, "po.converted_to_bill:0", "c"),
            idem_posted=auto_je.je_idempotency_key(doc, "po.converted_to_bill:0", "p"),
            memo=f"Auto JE for {doc} converted to bill", ts="2026-01-01",
            entries=[auto_je._line(IN_TRANSIT, R.INVENTORY_PURCHASED, debit=PRICE * qty),
                     auto_je._line(AP, R.PAYABLE, credit=PRICE * qty)],
            metadata_={"trigger": "doc.converted_to_bill", "doc_id": doc})
    await session.commit()
    return doc


async def _earlier_release(client, monkeypatch, auth, doc: str, *paths: str) -> None:
    """Run operations on a document as the earlier release did, when nothing knew it was
    imported: a void or revert reversed nothing the opening balances hold, and an unvoid
    restored the import's own entry."""
    async def unknown(*_a, **_k):
        return None

    with monkeypatch.context() as m:
        m.setattr(auto_je, "imported_document", unknown)
        for path in paths:
            r = await client.post(path.format(d=doc), headers=auth["headers"], json={})
            assert r.status_code == 200, f"{path}: {r.text}"


async def _repair(session) -> dict:
    from celerp.migrations._data_reconcile import set_meta
    from celerp_docs.imported_cutover import CUTOVER_KEY, repair_imported_documents

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, CUTOVER_KEY, ""))
    result = await repair_imported_documents(session)
    await session.commit()
    return result


async def _ledger_size(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def _repaired(session, auth, docs: list[str]) -> None:
    """Repair, check, and prove a second run changes nothing."""
    with pytest.raises(AssertionError):
        await _check(session, auth, docs, "before the repair")
    await _repair(session)
    await _check(session, auth, docs, "repaired")
    size = await _ledger_size(session, auth)
    await _repair(session)
    assert await _ledger_size(session, auth) == size
    await _check(session, auth, docs, "repaired twice")


async def test_repair_legacy_po_then_carry_on(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    await _repaired(session, auth, [po])
    await _step(client, session, auth, [po], "return 2", "post", RET, **_ret(2, lot))
    await _step(client, session, auth, [po], "receive 6", "post", RCV, **_receive(6, lot))
    await _step(client, session, auth, [po], "finalize", "post", FIN)


async def test_repair_legacy_po_already_converted_to_a_bill(client, session, auth, monkeypatch):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    await _earlier_release(client, monkeypatch, auth, po, FIN)
    await _repaired(session, auth, [po])
    await _step(client, session, auth, [po], "receive 6", "post", RCV, **_receive(6, lot))


async def test_repair_legacy_bill_then_return(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=5)
    await _repaired(session, auth, [bill])
    await _step(client, session, auth, [bill], "return 1", "post", RET, **_ret(1, lot))
    assert await _lot(session, auth, lot) == (9.0, _OPENING - PRICE)


async def test_repair_legacy_bill_voided_before_the_repair(client, session, auth, monkeypatch):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=0)
    await _earlier_release(client, monkeypatch, auth, bill, VOID)
    await _repaired(session, auth, [bill])
    await _step(client, session, auth, [bill], "unvoid", "post", UNVOID, json={})
    await _step(client, session, auth, [bill], "revert", "post", REV, json={})
    await _step(client, session, auth, [bill], "finalize", "post", FIN)


@pytest.mark.parametrize("undone", [VOID, REV])
async def test_repair_legacy_bill_with_goods_received_undone_before_the_repair(client, session, auth, monkeypatch,
                                                                               undone):
    """5 billed, 2 already received when imported: the opening balances hold 70 owed and the
    3 in transit (42); the 2 received are in the lot's opening stock. Voided or reverted to
    draft under the earlier release, the repair takes out of the opening balances the 70 owed
    and the 42 in transit. The 2 received stay in stock where the lot holds them, so
    the 28 owed for them leaves against the equity the opening balances held it against."""
    lot = await _item(client, auth, _OPENING, qty=10)
    equity = (await _books(session, auth, "3200"))["3200"]
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=2)
    await _earlier_release(client, monkeypatch, auth, bill, undone)
    await _repaired(session, auth, [bill])
    assert await _books(session, auth, IN_TRANSIT, AP, "3200") == {IN_TRANSIT: 0.0, AP: 0.0, "3200": equity}
    assert await _lot(session, auth, lot) == (10.0, _OPENING)


async def test_repair_legacy_bill_reverted_and_refinalized_before_the_repair(client, session, auth, monkeypatch):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=0)
    await _earlier_release(client, monkeypatch, auth, bill, REV, FIN)
    await _repaired(session, auth, [bill])
    await _step(client, session, auth, [bill], "void", "post", VOID, json={})
    await _step(client, session, auth, [bill], "unvoid", "post", UNVOID, json={})


async def test_repair_legacy_bill_reverted_before_the_repair(client, session, auth, monkeypatch):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=0)
    await _earlier_release(client, monkeypatch, auth, bill, REV)
    await _repaired(session, auth, [bill])
    await _step(client, session, auth, [bill], "finalize", "post", FIN)
    await _step(client, session, auth, [bill], "receive 5", "post", RCV, **_receive(5, lot))


async def test_repair_legacy_bill_voided_and_unvoided_before_the_repair(client, session, auth, monkeypatch):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=0)
    await _earlier_release(client, monkeypatch, auth, bill, VOID, UNVOID)
    await _repaired(session, auth, [bill])
    await _step(client, session, auth, [bill], "void", "post", VOID, json={})


async def test_repair_tells_the_owner_once(client, session, auth):
    from celerp.models.notification import Notification

    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    await _repair(session)
    await _repair(session)
    notices = (await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"], Notification.category == "system",
        Notification.i18n["title"].as_string() == "notice.imported_doc_cutover.title"))).scalars().all()
    assert len(notices) == 1 and notices[0].priority == "high"
    body = notices[0].body
    assert (await _state(session, auth, po))["doc_number"] in body
    written = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"],
        LedgerEntry.metadata_[auto_je.IMPORTED_CUTOVER].as_boolean().is_(True)))).scalars().all()
    assert {(e.entity_id, e.event_type) for e in written} == {
        (f"je:auto:{po}:rcv", "acc.journal_entry.voided"), (po, "doc.updated")}


async def test_repair_waits_while_no_open_date_exists(client, session, auth):
    """A lock covering today leaves no open date for the correction: it waits for a
    later start and nothing is written."""
    from celerp.migrations._data_reconcile import get_meta
    from celerp_docs.imported_cutover import CUTOVER_KEY

    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    lock = "/accounting/period-lock"
    assert (await client.post(lock, headers=auth["headers"], json={"lock_date": "2999-12-31"})).status_code == 200
    size = await _ledger_size(session, auth)
    assert (await _repair(session))["deferred"] == 1
    assert await _ledger_size(session, auth) == size
    conn = await session.connection()
    assert not await conn.run_sync(lambda c: get_meta(c, CUTOVER_KEY))
    assert (await client.post(lock, headers=auth["headers"], json={"lock_date": None})).status_code == 200
    session.expire_all()
    await _repaired(session, auth, [po])


async def test_repair_in_a_locked_period_reverses_on_an_open_date(client, session, auth):
    """The old entry stays in its locked period and is reversed on the business date
    today, so the repair does not wait for the period to be unlocked."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    assert (await client.post("/accounting/period-lock", headers=auth["headers"],
                              json={"lock_date": "2026-06-30"})).status_code == 200
    assert (await _repair(session))["deferred"] == 0
    session.expire_all()
    rcv = (await session.get(Projection, (auth["company_id"], f"je:auto:{po}:rcv"))).state
    assert rcv["status"] == "void" and rcv["reversed_on"] > "2026-06-30", rcv


async def test_repair_leaves_documents_imported_now_alone(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = await _import(client, auth, lot, doc_type="bill", received=0)
    size = await _ledger_size(session, auth)
    await _repair(session)
    assert await _ledger_size(session, auth) == size
    await _check(session, auth, [bill], "after the repair")


async def test_an_imported_receipt_is_costed_from_the_line_naming_its_goods(client, session, auth):
    """A receipt whose line index points at another item's line is costed from the line
    that names the goods it received, as a receipt in Celerp is matched to its line."""
    a = await _item(client, auth, 30.0, qty=3)
    b = await _item(client, auth, 150.0, qty=3)
    await _opening(client, auth, 180.0, 0.0)
    doc = f"doc:{uuid.uuid4()}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc, "event_type": "doc.created", "source": "test", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "purchase_order", "contact_id": "supplier:1", "status": "received", "doc_number": "IMP-IDX",
                 "import_treatment": "opening_balances",
                 "issue_date": "2026-01-01", "subtotal": 180, "total": 180, "amount_outstanding": 180, "amount_paid": 0,
                 "line_items": [{"item_id": a, "name": "A", "quantity": 3, "unit_price": 10},
                                {"item_id": b, "name": "B", "quantity": 3, "unit_price": 50}],
                 "received_items": [{"item_id": b, "po_line_index": 0, "quantity_received": 3.0, "receive_as": "stock"}]}})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, doc))["received_items"][0]["lot_cost_added"] == 150.0

