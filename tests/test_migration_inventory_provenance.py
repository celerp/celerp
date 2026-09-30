# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Documents a Manager migration brings in carry the same receipt and fulfilment provenance
Celerp's own receive and fulfil write, so the ordinary document actions work on them:
return goods to the supplier, undo a receipt, revert a fulfilment, void, credit and take
back a sale, refund and void a payment. After every action the stock on hand, its value,
the inventory books and the open balances still agree.

Starting position (specs.inventory_lifecycle_objects): 10 widgets on hand worth 47.50.
BILL-G received 10 at 4.00, BILL-F 5 at 6.00, BILL-P 5 of 8 at 5.00, BILL-U nothing.
INV-E delivered 2 at 4.75, INV-D delivered 4 at 4.75 and paid 50.00."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from fixtures.manager_io import specs
from fixtures.manager_io.support import INVENTORY, ref
from migration_support import OWNER_EMAIL, auth, maker, real_client, real_engine  # noqa: F401 - fixtures
from test_migration_e2e import _maps, _passing, _projections, migrate

MODES = [{"mode": "full_history"}, {"mode": "cutover", "cutover_date": specs.LIFECYCLE_CUTOVER.isoformat()}]
OFF_HAND = {"sold", "memo_out", "archived", "merged", "disposed"}
CENT = D("0.01")
# Account codes Celerp's own document actions post to, beside the migrated chart.
INVENTORY_CODES, AR_CODES, AP_CODES = {"1130-P"}, {"1120"}, {"2110"}
# The entry that puts a document on the receivable or payable.
RECOGNITION = re.compile(r"^je:auto:(doc:[^:]+):(?:fin|bill)(?::\d+)?$")


@dataclass
class Books:
    engine: object
    run: object
    maps: dict
    token: str
    location: str

    def id(self, source_type: str, label: str) -> str:
        return self.maps[(source_type, ref(label))]

    @property
    def headers(self) -> dict:
        return auth(self.token)


async def _migrated(real_engine, monkeypatch, tmp_path, decisions=MODES[0]) -> Books:
    from celerp.models.company import Company, User
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations
    from celerp.services.auth import issue_token_pair

    run, rejected = await migrate(real_engine, INVENTORY.read_bytes(), "lifecycle.manager", decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    async with maker(real_engine)() as s:
        await migrations.finalize(s, await s.get(MigrationRun, run.id))
    maps = {(m.source_type, m.source_external_id): m.target_entity_id for m in await _maps(real_engine, run)}
    items = await _projections(real_engine, run, "item")
    async with maker(real_engine)() as s:
        user = await s.scalar(select(User).where(User.email == OWNER_EMAIL))
        token = (await issue_token_pair(s, user=user, company=await s.get(Company, run.company_id),
                                        role="owner"))["access_token"]
    return Books(real_engine, run, maps, token, items[maps[("InventoryItem", ref("WID"))]]["location_id"])


async def _doc(books: Books, label: str, source_type: str | None = None) -> dict:
    source_type = source_type or ("SalesInvoice" if label.startswith("INV") else "PurchaseInvoice")
    return (await _projections(books.engine, books.run, "doc"))[books.id(source_type, label)]


async def _sold_lot(books: Books, label: str) -> str:
    invoice = books.id("SalesInvoice", label)
    (lot,) = [key for key, item in (await _projections(books.engine, books.run, "item")).items()
              if item.get("status") == "sold" and item.get("status_doc_id") == invoice]
    return lot


def _d(value) -> D:
    return D(str(value or 0)).quantize(CENT)


async def _position(books: Books) -> dict:
    """Stock on hand and its value, the inventory books, and whether every journal balances
    and the receivable and payable controls equal the open documents."""
    items = await _projections(books.engine, books.run, "item")
    on_hand = [i for i in items.values() if i.get("sku") == "WID-1" and i.get("status") not in OFF_HAND]
    stock = (sum(D(str(i.get("quantity") or 0)) for i in on_hand), sum(_d(i.get("cost_total")) for i in on_hand))

    account_of = {m: t for (_, m), t in books.maps.items()}
    inventory = INVENTORY_CODES | {account_of[ref("@BalanceSheetInventoryOnHandAccount")]}
    ar = AR_CODES | {account_of[ref("@BalanceSheetAccountsReceivableAccount")]}
    ap = AP_CODES | {account_of[ref("@BalanceSheetAccountsPayableAccount")]}
    balances = {"inventory": D(0), "ar": D(0), "ap": D(0)}
    recognized: set[str] = set()
    for je_id, je in (await _projections(books.engine, books.run, "journal_entry")).items():
        if je.get("status") == "void":
            continue
        lines = je.get("entries") or []
        assert sum(_d(l.get("debit")) for l in lines) == sum(_d(l.get("credit")) for l in lines), je
        recognition = RECOGNITION.match(je_id)
        if recognition:
            recognized.add(recognition.group(1))
        for line in lines:
            net = _d(line.get("debit")) - _d(line.get("credit"))
            # A supplier return takes the goods off the payable but leaves the bill's own
            # balance as it was: the supplier now owes that credit back.
            if ":rtn:" in je_id and line.get("account") in ap:
                net = D(0)
            for name, codes in (("inventory", inventory), ("ar", ar), ("ap", ap)):
                if line.get("account") in codes:
                    balances[name] += net

    # Only a document whose recognition entry posted is on the receivable or payable: a
    # credit note raised in Celerp posts no entry of its own until it is applied or refunded.
    open_docs = {"ar": D(0), "ap": D(0)}
    for doc_id, doc in (await _projections(books.engine, books.run, "doc")).items():
        if doc.get("status") in ("draft", "void") or doc_id not in recognized:
            continue
        outstanding = _d(doc.get("amount_outstanding"))
        if doc.get("doc_type") == "invoice":
            open_docs["ar"] += outstanding
        elif doc.get("doc_type") == "credit_note":
            open_docs["ar"] -= outstanding
        elif doc.get("doc_type") == "bill":
            open_docs["ap"] += outstanding
    assert balances["ar"] == open_docs["ar"], (balances, open_docs)
    assert -balances["ap"] == open_docs["ap"], (balances, open_docs)
    return {"stock": stock, "inventory": balances["inventory"]}


def _moved(before: dict, after: dict) -> tuple[tuple[D, D], D]:
    """(stock quantity and value moved, inventory books moved) between two positions."""
    return ((after["stock"][0] - before["stock"][0], after["stock"][1] - before["stock"][1]),
            after["inventory"] - before["inventory"])


async def _return(client, books: Books, bill: str, lot: str, qty: float):
    return await client.post(f"/docs/{books.id('PurchaseInvoice', bill)}/return-items", headers=books.headers,
                             json={"items": [{"item_id": lot, "quantity_returned": qty}]})


async def _credit_and_take_back(client, books: Books, invoice: str, qty: float):
    lot = await _sold_lot(books, invoice)
    r = await client.post("/docs", headers=books.headers, json={
        "doc_type": "credit_note", "original_doc_id": books.id("SalesInvoice", invoice),
        "line_items": [{"name": "Widget", "sku": "WID-1", "quantity": qty, "unit_price": 12.5, "sell_by": "unit"}],
        "subtotal": 12.5 * qty, "tax": 0, "total": 12.5 * qty})
    assert r.status_code == 200, r.text
    credit = r.json()["id"]
    r = await client.post(f"/docs/{credit}/finalize", headers=books.headers)
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{credit}/receive-return", headers=books.headers,
                          json={"items": [{"sku": "WID-1", "quantity": qty, "item_id": lot}]})
    assert r.status_code == 200, r.text
    return credit


@pytest.mark.parametrize("decisions", MODES)
async def test_post_migration_lifecycle_keeps_quantity_valuation_and_journal_invariants(
    real_engine, real_client, monkeypatch, tmp_path, decisions,
):
    """RED before the change: a sales invoice with item lines stops the migration, so the
    company never exists; before it, a migrated bill carried no lot it could act on."""
    books = await _migrated(real_engine, monkeypatch, tmp_path, decisions)
    wid = books.id("InventoryItem", "WID")
    start = await _position(books)
    assert start["stock"] == (D("10"), D("47.50"))

    # Four of BILL-G's widgets go back at the 4.00 they came in at.
    r = await _return(real_client, books, "BILLG", wid, 4)
    assert r.status_code == 200, r.text
    after_return = await _position(books)
    assert _moved(start, after_return) == ((D("-4"), D("-16.00")), D("-16.00"))

    # INV-E's delivery is reverted: its 2 widgets come back at 4.75, then the invoice is voided.
    lot = await _sold_lot(books, "INVE")
    r = await real_client.post(f"/docs/{books.id('SalesInvoice', 'INVE')}/revert-lines", headers=books.headers,
                               json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    after_revert = await _position(books)
    assert _moved(after_return, after_revert)[0] == (D("2"), D("9.50"))
    r = await real_client.post(f"/docs/{books.id('SalesInvoice', 'INVE')}/void", headers=books.headers, json={})
    assert r.status_code == 200, r.text
    after_void = await _position(books)
    assert after_void["stock"] == after_revert["stock"]

    # BILL-U's goods never arrived in Manager; receiving them now brings in 3 at 4.00, which
    # the bill already put in the books.
    r = await real_client.post(f"/docs/{books.id('PurchaseInvoice', 'BILLU')}/receive", headers=books.headers,
                               json={"location_id": books.location,
                                     "received_items": [{"po_line_index": 0, "quantity_received": 3}]})
    assert r.status_code == 200, r.text
    after_receive = await _position(books)
    assert _moved(after_void, after_receive) == ((D("3"), D("12.00")), D("0.00"))

    # One of INV-D's delivered widgets is credited and taken back at its 4.75 cost.
    await _credit_and_take_back(real_client, books, "INVD", 1)
    end = await _position(books)
    assert _moved(after_receive, end) == ((D("1"), D("4.75")), D("4.75"))
    assert end["stock"] == (D("12"), D("57.75"))


async def test_imported_po_receipt_can_be_returned(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: the migration is refused; before it, a migrated bill carried
    no receipt of any lot, so nothing could be returned (422)."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    wid = books.id("InventoryItem", "WID")
    start = await _position(books)
    for bill, qty, cost in (("BILLG", 4, D("16.00")), ("BILLF", 1, D("6.00"))):
        before = await _position(books)
        r = await _return(real_client, books, bill, wid, qty)
        assert r.status_code == 200, r.text
        assert _moved(before, await _position(books)) == ((D(-qty), -cost), -cost)
    assert (await _doc(books, "BILLG"))["status"] == "partial_returned"
    assert (await _position(books))["stock"] == (start["stock"][0] - 5, start["stock"][1] - D("22.00"))

    # Only what the bill received can go back.
    r = await _return(real_client, books, "BILLG", wid, 7)
    assert r.status_code == 422, r.text


async def test_imported_invoice_fulfilment_can_be_reverted_and_voided(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: the migration is refused; before it, a migrated invoice had
    no fulfilment, so its delivered goods could never be taken back."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    invoice = books.id("SalesInvoice", "INVE")
    assert (await _doc(books, "INVE"))["fulfillment_status"] == "fulfilled"

    # A fulfilled invoice cannot be voided until its goods are back.
    r = await real_client.post(f"/docs/{invoice}/void", headers=books.headers, json={})
    assert r.status_code == 409, r.text

    start = await _position(books)
    lot = await _sold_lot(books, "INVE")
    r = await real_client.post(f"/docs/{invoice}/revert-lines", headers=books.headers,
                               json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert "fulfillment_status" not in await _doc(books, "INVE")
    assert _moved(start, await _position(books))[0] == (D("2"), D("9.50"))

    r = await real_client.post(f"/docs/{invoice}/void", headers=books.headers, json={})
    assert r.status_code == 200, r.text
    assert (await _doc(books, "INVE"))["status"] == "void"
    await _position(books)


async def test_imported_fulfilled_sale_credit_and_return(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: the migration is refused; before it, a migrated sale left no
    sold lot to take back against a credit note."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    start = await _position(books)
    await _credit_and_take_back(real_client, books, "INVD", 2)
    assert _moved(start, await _position(books)) == ((D("2"), D("9.50")), D("9.50"))


async def test_imported_bill_reopen_keeps_receipt_provenance(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: the migration is refused; before it, a migrated bill's receipt
    named no lot, so it could neither be undone nor the bill reopened."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    bill = books.id("PurchaseInvoice", "BILLP")
    start = await _position(books)

    # A bill whose goods are in stock cannot go back to draft; the receipt stays as it was.
    received = (await _doc(books, "BILLP"))["received_items"]
    r = await real_client.post(f"/docs/{bill}/revert-to-draft", headers=books.headers, json={})
    assert r.status_code == 409, r.text
    assert (await _doc(books, "BILLP"))["received_items"] == received
    assert (await _position(books))["stock"] == start["stock"]

    # Undoing the receipt takes its 5 widgets at 25.00 back out; then the bill reopens.
    r = await real_client.delete(f"/docs/{bill}/receive", headers=books.headers)
    assert r.status_code == 200, r.text
    assert _moved(start, await _position(books))[0] == (D("-5"), D("-25.00"))
    r = await real_client.post(f"/docs/{bill}/revert-to-draft", headers=books.headers, json={})
    assert r.status_code == 200, r.text
    assert (await _doc(books, "BILLP"))["status"] == "draft"
    await _position(books)


async def test_imported_paid_invoice_refund_and_payment_void(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: a sales invoice with item lines stops the migration."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    invoice = books.id("SalesInvoice", "INVD")
    assert (await _doc(books, "INVD"))["status"] == "paid"
    start = await _position(books)

    r = await real_client.post(f"/docs/{invoice}/refund", headers=books.headers,
                               json={"payment_index": 0, "amount": 20, "payment_date": "2026-02-01"})
    assert r.status_code == 200, r.text
    doc = await _doc(books, "INVD")
    assert (_d(doc["amount_paid"]), _d(doc["amount_outstanding"])) == (D("30.00"), D("20.00"))
    await _position(books)

    r = await real_client.post(f"/docs/{invoice}/void-payment", headers=books.headers,
                               json={"payment_index": 0, "refund_date": "2026-02-02"})
    assert r.status_code == 200, r.text
    doc = await _doc(books, "INVD")
    assert (_d(doc["amount_paid"]), _d(doc["amount_outstanding"])) == (D("0.00"), D("50.00"))
    # Money moves; the delivered goods stay delivered.
    assert (await _position(books))["stock"] == start["stock"]
    assert doc["fulfillment_status"] == "fulfilled"


async def test_imported_partial_receipt_partial_return(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: the migration is refused; before it, a migrated bill showed
    every line received though only 5 of BILL-P's 8 widgets arrived."""
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    bill, wid = books.id("PurchaseInvoice", "BILLP"), books.id("InventoryItem", "WID")
    doc = await _doc(books, "BILLP")
    assert (doc["status"], doc["line_items"][0]["quantity_received"]) == ("partially_received", 5)

    start = await _position(books)
    r = await _return(real_client, books, "BILLP", wid, 2)
    assert r.status_code == 200, r.text
    assert _moved(start, await _position(books)) == ((D("-2"), D("-10.00")), D("-10.00"))
    r = await _return(real_client, books, "BILLP", wid, 4)
    assert r.status_code == 422, r.text

    # The 3 that never arrived can still be received, and no more.
    for qty, status in ((4, 422), (3, 200)):
        r = await real_client.post(f"/docs/{bill}/receive", headers=books.headers, json={
            "location_id": books.location, "received_items": [{"po_line_index": 0, "quantity_received": qty}]})
        assert r.status_code == status, r.text
    await _position(books)


async def test_migration_lifecycle_uses_canonical_domain_code_only(real_engine, monkeypatch, tmp_path):
    """RED before the change: the migration is refused; before it, a migrated bill's receipt
    named no lot and a migrated invoice had no fulfilment, so the canonical return and
    revert code found nothing to act on.

    The migration writes the provenance Celerp's own receive and fulfil write, and carries
    no reversal of its own: every undo is the ordinary document action."""
    from celerp.services import auto_je
    from celerp_docs.routes import _lot_additions, _returnable_quantities

    books = await _migrated(real_engine, monkeypatch, tmp_path)
    wid = books.id("InventoryItem", "WID")
    bill = await _doc(books, "BILLG")
    assert _lot_additions(bill) == {wid: (10.0, 40.0)}
    async with maker(real_engine)() as s:
        assert await _returnable_quantities(s, books.run.company_id, bill) == {wid: 10.0}
        invoice_id = books.id("SalesInvoice", "INVE")
        lot = await _sold_lot(books, "INVE")
        items = await _projections(real_engine, books.run, "item")
        assert await auto_je.doc_line_of_lot(s, books.run.company_id, invoice_id, await _doc(books, "INVE"),
                                             lot, items[lot]) == 0

    root = Path(__file__).resolve().parents[1] / "default_modules"
    reversals = ("doc.receive_undone", "doc.items_returned", "item.fulfillment_reversed", "doc.fulfillment_reversed",
                 "doc.voided", "doc.return_received", "doc.payment.voided", "doc.payment.refunded")
    for sink in sorted(root.glob("*/celerp_*/migration_sink.py")):
        source = sink.read_text()
        assert not [name for name in reversals if f'"{name}"' in source], sink


@pytest.mark.parametrize("cutover", ["2026-01-09", specs.LIFECYCLE_CUTOVER.isoformat()])
async def test_cutover_imported_documents_support_return_void_revert(
    real_engine, real_client, monkeypatch, tmp_path, cutover,
):
    """RED before the change: a sales invoice with item lines stops the migration; before
    it, a document carried across the cutover had no receipt or fulfilment to act on.

    On a cutover migration, documents carried across the cutover keep the receipts and
    deliveries made on either side of it. At 01-09, BILL-G's first receipt and BILL-P's
    bill date fall inside the opening; at 01-17, INV-E's delivery does, before its invoice.
    Each document action moves stock and books by exactly what the source recorded:
    10 / 47.50, less 4 returned at 4.00, plus INV-E's 2 and INV-D's 4 back at 4.75, less
    BILL-P's 5 at 5.00, is 7 / 35.00."""
    books = await _migrated(real_engine, monkeypatch, tmp_path, {"mode": "cutover", "cutover_date": cutover})
    wid = books.id("InventoryItem", "WID")
    start = await _position(books)
    assert start["stock"] == (D("10"), D("47.50"))

    r = await _return(real_client, books, "BILLG", wid, 4)
    assert r.status_code == 200, r.text
    returned = await _position(books)
    assert _moved(start, returned) == ((D("-4"), D("-16.00")), D("-16.00"))

    for invoice, back in (("INVE", (D("2"), D("9.50"))), ("INVD", (D("4"), D("19.00")))):
        before = await _position(books)
        r = await real_client.post(f"/docs/{books.id('SalesInvoice', invoice)}/revert-lines", headers=books.headers,
                                   json={"line_entity_ids": [await _sold_lot(books, invoice)]})
        assert r.status_code == 200, (invoice, r.text)
        assert _moved(before, await _position(books))[0] == back, invoice
    reverted = await _position(books)
    r = await real_client.post(f"/docs/{books.id('SalesInvoice', 'INVE')}/void", headers=books.headers, json={})
    assert r.status_code == 200, r.text
    assert (await _doc(books, "INVE"))["status"] == "void"
    assert (await _position(books))["stock"] == reverted["stock"]

    bill = books.id("PurchaseInvoice", "BILLP")
    before = await _position(books)
    r = await real_client.delete(f"/docs/{bill}/receive", headers=books.headers)
    assert r.status_code == 200, r.text
    assert _moved(before, await _position(books))[0] == (D("-5"), D("-25.00"))
    r = await real_client.post(f"/docs/{bill}/revert-to-draft", headers=books.headers, json={})
    assert r.status_code == 200, r.text
    assert (await _doc(books, "BILLP"))["status"] == "draft"
    assert (await _position(books))["stock"] == (D("7"), D("35.00"))
