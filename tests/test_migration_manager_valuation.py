# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager's books and its warehouse, kept apart and each carried faithfully.

A cutover keeps every document a later stock movement depends on. Each delivery of an
invoice line becomes its own sold lot, so a later revert brings back exactly what that
delivery took. An invoice's cost of sales is what Manager booked from the stock it owned
when invoicing, whatever had physically arrived, and the goods delivered leave at that
cost. Every figure is worked by hand in the docstring of its spec."""

from __future__ import annotations

import re
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode
from fixtures.manager_io import specs
from fixtures.manager_io.support import actual_rows, adapter, artifact, ref
from migration_support import maker, real_client, real_engine  # noqa: F401 - fixtures
from test_migration_e2e import _projections
from test_migration_inventory_provenance import _moved, _migrated, _position, _sold_lots

WID = ref("WID")
INVENTORY_ACCOUNT = ref("@BalanceSheetInventoryOnHandAccount")


def _cutover(day) -> MigrationDecisions:
    return MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=day)


def _manifest(path, decisions=MigrationDecisions(mode=CIFMode.FULL_HISTORY)):
    return adapter().build_manifest([artifact(path)], decisions)


def _documents(manifest) -> dict:
    return {d.source_external_id: d for d in manifest.bundle.documents}


def _links(document) -> list[tuple]:
    key = "deliveries" if document.doc_type == "invoice" else "receipts"
    return [(m["source"], m["date"], line["line"], D(line["quantity"]), D(line["value"]))
            for m in document.metadata.get(key) or [] for line in m["lines"] if line["item"] == WID]


def _held(manifest) -> tuple[D, D]:
    adjustments = [a for a in manifest.bundle.inventory_adjustments if a.item_external_id == WID]
    return sum(a.quantity for a in adjustments), sum(a.value or D(0) for a in adjustments)


def _reconciled(manifest) -> dict[tuple[str, str], D]:
    return {(measure, key): value for measure, key, _, value in actual_rows(manifest.reconciliation_expectations)
            if (measure, key) in (("inventory_quantity", WID), ("inventory_value", WID),
                                  ("trial_balance", INVENTORY_ACCOUNT))}


def _write(tmp_path, name: str, objects) -> object:
    return specs.write_manager_file(tmp_path / f"{name}.manager", objects)


async def _revert(client, books, invoice: str, lot: str):
    r = await client.post(f"/docs/{books.id('SalesInvoice', invoice)}/revert-lines", headers=books.headers,
                          json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text


# --- 1. Cutover dependency closure --------------------------------------------------------

def test_cutover_carries_settled_documents_whose_goods_move_after_it(tmp_path):
    """RED before the change: INV-C and BILL-C are settled in full before the cutover, so
    neither is carried, and DN-C and GR-C land as stock adjustments naming no document."""
    manifest = _manifest(_write(tmp_path, "dependency", specs.cutover_dependency_objects()),
                         _cutover(specs.DEPENDENCY_CUTOVER))
    documents = _documents(manifest)
    assert _links(documents[ref("INVC")]) == [(ref("DNC"), "2026-01-21", 0, D("2"), D("8.00"))]
    assert _links(documents[ref("BILLC")]) == [(ref("GRC"), "2026-01-20", 0, D("10"), D("40.00"))]
    assert (documents[ref("INVC")].status, documents[ref("BILLC")].status) == ("paid", "paid")
    assert _held(manifest) == (D("13"), D("52.00"))


async def test_cutover_settled_documents_keep_their_later_goods_movements(
    real_engine, real_client, monkeypatch, tmp_path,
):
    """RED before the change: INV-C and BILL-C are not carried, so the delivery has no sold
    lot to revert and the receipt no bill to undo it on.

    From 13 / 52.00, reverting INV-C's delivery brings back 2 at 4.00, and undoing BILL-C's
    receipt takes out the 10 it brought in at 4.00."""
    source = _write(tmp_path, "dependency", specs.cutover_dependency_objects())
    books = await _migrated(real_engine, monkeypatch, tmp_path,
                            {"mode": "cutover", "cutover_date": specs.DEPENDENCY_CUTOVER.isoformat()}, source)
    start = await _position(books)
    assert start["stock"] == (D("13"), D("52.00"))
    (lot,) = await _sold_lots(books, "INVC")
    await _revert(real_client, books, "INVC", lot)
    reverted = await _position(books)
    assert _moved(start, reverted)[0] == (D("2"), D("8.00"))
    r = await real_client.delete(f"/docs/{books.id('PurchaseInvoice', 'BILLC')}/receive", headers=books.headers)
    assert r.status_code == 200, r.text
    assert _moved(reverted, await _position(books))[0] == (D("-10"), D("-40.00"))


# --- 2. One sold lot per delivery ---------------------------------------------------------

def test_each_delivery_of_an_invoice_line_moves_its_own_share_of_cost(tmp_path):
    """RED before the change: the deliveries leave at the moving average of the widgets
    physically held (4.00 and 4.44), not their share of the 16.33 Manager booked."""
    manifest = _manifest(_write(tmp_path, "batched", specs.batched_delivery_objects()))
    assert _links(_documents(manifest)[ref("INVB")]) == [
        (ref("DNB1"), "2026-01-06", 0, D("1"), D("5.44")), (ref("DNB2"), "2026-01-09", 0, D("2"), D("10.89"))]
    assert _held(manifest) == (D("6"), D("32.67"))


async def test_each_delivery_of_an_invoice_line_is_its_own_sold_lot(real_engine, real_client, monkeypatch, tmp_path):
    """RED before the change: INV-B's two deliveries become one sold lot of 3 dated at the
    last, so which goods left when, and at what cost, is lost.

    DN-B1's 1 widget at 5.44 and DN-B2's 2 at 10.89 are two sold lots, both recognized
    against INV-B's line. Reverting the line, as on an invoice fulfilled from two lots in
    Celerp, brings back both, each lot at the cost its own delivery took."""
    from celerp.services import auto_je

    books = await _migrated(real_engine, monkeypatch, tmp_path,
                            source=_write(tmp_path, "batched", specs.batched_delivery_objects()))
    lots = {(D(str(item["quantity"])), D(str(item["cost_total"])).quantize(D("0.01"))): key
            for key, item in (await _sold_lots(books, "INVB")).items()}
    assert set(lots) == {(D("1"), D("5.44")), (D("2"), D("10.89"))}
    first, second = lots[(D("1"), D("5.44"))], lots[(D("2"), D("10.89"))]

    async with maker(real_engine)() as s:
        recognized = await auto_je.recognized_cogs(s, books.run.company_id, books.id("SalesInvoice", "INVB"))
    line = recognized.allocations["0"]
    assert (line["amount"], line["provisional_qty"]) == (16.33, 0.0)
    assert sorted((lot["lot_entity_id"], lot["qty"], lot["unit_cost"]) for lot in line["lots"]) == sorted(
        [(first, 1.0, 5.44), (second, 2.0, 5.445)])

    start = await _position(books)
    await _revert(real_client, books, "INVB", second)
    assert _moved(start, await _position(books))[0] == (D("3"), D("16.33"))
    assert await _sold_lots(books, "INVB") == {}
    items = await _projections(real_engine, books.run, "item")
    assert {lot: (items[lot]["status"], D(str(items[lot]["cost_total"])).quantize(D("0.01")))
            for lot in (first, second)} == {
        first: ("available", D("5.44")), second: ("available", D("10.89"))}


# --- 3. Ownership valuation apart from physical provenance ---------------------------------

@pytest.mark.parametrize("decisions", [MigrationDecisions(mode=CIFMode.FULL_HISTORY),
                                       _cutover(specs.OWNERSHIP_CUTOVER)])
def test_cost_of_sales_follows_what_manager_owned_not_what_had_arrived(tmp_path, decisions):
    """RED before the change: with no unit cost set, INV-O books no cost of sales, and DN-O
    takes its 5 widgets out at the 4.00 of what had physically arrived.

    Manager owns 20 widgets worth 100.00 when INV-O is invoiced, so it books 25.00; DN-O
    takes that out; the 15 on hand are worth 75.00, as is the inventory account."""
    manifest = _manifest(_write(tmp_path, "ownership", specs.ownership_interleaving_objects()), decisions)
    invoice = _documents(manifest)[ref("INVO")]
    assert invoice.line_items[0].cost_basis == D("25.00")
    assert _links(invoice) == [(ref("DNO"), "2026-01-08", 0, D("5"), D("25.00"))]
    assert _held(manifest) == (D("15"), D("75.00"))
    assert _reconciled(manifest) == {("inventory_quantity", WID): D("15"), ("inventory_value", WID): D("75.00"),
                                     ("trial_balance", INVENTORY_ACCOUNT): D("75.00")}


@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": specs.OWNERSHIP_CUTOVER.isoformat()}])
async def test_migrated_sale_recognizes_the_cost_manager_booked(real_engine, monkeypatch, tmp_path, decisions):
    """RED before the change: INV-O recognizes no cost of sales and its sold lot is worth
    the 20.00 physical average, so stock and the inventory account disagree by 5.00."""
    from celerp.services import auto_je

    books = await _migrated(real_engine, monkeypatch, tmp_path, decisions,
                            _write(tmp_path, "ownership", specs.ownership_interleaving_objects()))
    ((lot, item),) = (await _sold_lots(books, "INVO")).items()
    assert (D(str(item["quantity"])), D(str(item["cost_total"])).quantize(D("0.01"))) == (D("5"), D("25.00"))
    async with maker(real_engine)() as s:
        recognized = await auto_je.recognized_cogs(s, books.run.company_id, books.id("SalesInvoice", "INVO"))
    assert recognized.allocations["0"]["amount"] == 25.0
    assert await _position(books) == {"stock": (D("15"), D("75.00")), "inventory": D("75.00")}


def test_sale_with_nothing_owned_and_no_unit_cost_refused_at_scan(tmp_path):
    """RED before the change: INV-Z is carried with no cost of sales, which Manager's books
    may not show."""
    with pytest.raises(ScanError, match=re.escape("SalesInvoice (no unit cost) (1)")):
        _manifest(_write(tmp_path, "unowned", specs.unowned_sale_objects()))
