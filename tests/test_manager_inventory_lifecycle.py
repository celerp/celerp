# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager stock moves on its physical records: goods receipts and delivery notes, or the
invoice itself when it is flagged to move its own stock. An invoice or bill with no physical
record moves no stock, a partial movement stays partial, and nothing is counted twice.

Every expected figure is worked out by hand in specs.inventory_lifecycle_objects and
recorded in checkpoints.json under "inventory", never read back from the adapter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions
from celerp.importers.schema import CIFMode
from fixtures.manager_io import specs
from fixtures.manager_io.support import CHECKPOINTS, INVENTORY, actual_rows, adapter, artifact, ref
from migration_support import real_engine  # noqa: F401 - fixture

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=specs.LIFECYCLE_CUTOVER)
WID = ref("WID")
_FULL, _CUT = CHECKPOINTS["inventory"]["full_history"], CHECKPOINTS["inventory"]["cutover"]
assert date.fromisoformat(_CUT["cutover_date"]) == specs.LIFECYCLE_CUTOVER

# (source type, movement, date, quantity, value) in the order stock moved.
MOVES = [(source_type, label, date.fromisoformat(day), D(qty), D(value))
         for source_type, label, day, qty, value in _FULL["moves"]]
HELD = (D(_FULL["inventory_quantity"]["WID"]), D(_FULL["inventory_value"]["WID"]))
OPENING = (D(_CUT["opening_quantity"]["WID"]), D(_CUT["opening_value"]["WID"]))   # after DN2
# Physical records per document: (movement, date, line, quantity, value).
LINKS = {doc: [(m, day, line, D(qty), D(value)) for m, day, line, qty, value in links]
         for doc, links in _FULL["links"].items()}


@pytest.fixture
def lifecycle():
    return INVENTORY


def _manifest(path, decisions=FULL):
    return adapter().build_manifest([artifact(path)], decisions)


def _moves(manifest) -> list[tuple]:
    return [(a.source_type, a.source_external_id, a.adjustment_date, a.item_external_id, a.quantity, a.value)
            for a in manifest.bundle.inventory_adjustments if a.kind == "adjustment"]


def _expected_moves(labels=None) -> list[tuple]:
    return [(source_type, f"{ref(label)}:stock:1", day, WID, qty, value)
            for source_type, label, day, qty, value in MOVES if labels is None or label in labels]


def _held(manifest) -> dict[str, tuple[D, D]]:
    out: dict[str, tuple[D, D]] = {}
    for a in manifest.bundle.inventory_adjustments:
        qty, value = out.get(a.item_external_id, (D(0), D(0)))
        out[a.item_external_id] = (qty + a.quantity, value + (a.value or D(0)))
    return out


def _documents(manifest) -> dict:
    return {d.source_external_id: d for d in manifest.bundle.documents}


def _links(document) -> list[tuple]:
    """The physical records a document carries, as (movement, date, line, quantity, value)."""
    key = "deliveries" if document.doc_type == "invoice" else "receipts"
    return [(m["source"], m["date"], line["line"], D(line["quantity"]), D(line["value"]))
            for m in document.metadata.get(key) or [] for line in m["lines"]
            if line["item"] == WID]


def _inventory_rows(manifest) -> set:
    return {(measure, key, value) for measure, key, _, value in actual_rows(manifest.reconciliation_expectations)
            if measure in ("inventory_quantity", "inventory_value")}


def test_manager_stock_matches_independent_source_oracle(lifecycle):
    """RED before the change: stock moves on the bills at their own dates and quantities,
    the goods receipts, delivery notes and unit costs are ignored, and sales never take
    stock out, so the item holds the 26 bought at 122.00 rather than 10 at 47.50."""
    full = _manifest(lifecycle)
    assert _moves(full) == _expected_moves()
    assert _held(full) == {WID: HELD}
    assert _inventory_rows(full) == {("inventory_quantity", WID, HELD[0]), ("inventory_value", WID, HELD[1])}

    cut = _manifest(lifecycle, CUTOVER)
    (opening,) = [a for a in cut.bundle.inventory_adjustments if a.kind == "opening"]
    assert (opening.adjustment_date, opening.item_external_id, opening.quantity, opening.value) == (
        specs.LIFECYCLE_CUTOVER, WID, *OPENING)
    assert _moves(cut) == _expected_moves({"DN3", "DN1", "INVX"})
    assert _held(cut) == {WID: HELD}
    assert _inventory_rows(cut) == _inventory_rows(full)


def test_manager_invoice_without_physical_movement_moves_no_stock(lifecycle):
    """RED before the change: the bill with no goods receipt moves its 3 widgets into stock
    on its own date, and a sales invoice with item lines cannot be read at all."""
    manifest = _manifest(lifecycle)
    documents = _documents(manifest)
    for label in ("INVN", "BILLU"):
        assert not [m for m in _moves(manifest) if m[1].startswith(ref(label))]
        assert _links(documents[ref(label)]) == []
        (line,) = documents[ref(label)].line_items
        assert line.item_external_id == WID


def test_manager_delivery_after_invoice_moves_stock_at_delivery(lifecycle):
    """RED before the change: delivery notes are not read, so INV-D's goods never leave."""
    manifest = _manifest(lifecycle)
    invoice = _documents(manifest)[ref("INVD")]
    assert invoice.issue_date == date(2026, 1, 15)
    assert [m for m in _moves(manifest) if m[1].startswith(ref("DN1"))] == _expected_moves({"DN1"})
    assert _links(invoice) == [(ref("DN1"), "2026-01-20", 0, D("4"), D("19.00"))]


def test_manager_goods_receipt_full_and_partial(lifecycle):
    """RED before the change: each bill moves its whole quantity on the bill date, so the
    8 widgets of BILL-P arrive though only 5 were received."""
    manifest = _manifest(lifecycle)
    documents = _documents(manifest)
    assert _links(documents[ref("BILLG")]) == [(ref(m), day, line, qty, value)
                                              for m, day, line, qty, value in LINKS["BILLG"]]
    assert sum(link[3] for link in _links(documents[ref("BILLG")])) == documents[ref("BILLG")].line_items[0].quantity
    assert _links(documents[ref("BILLP")]) == [(ref("GR3"), "2026-01-10", 0, D("5"), D("25.00"))]
    assert documents[ref("BILLP")].line_items[0].quantity == D("8")
    assert [m for m in _moves(manifest) if m[0] == "GoodsReceipt"] == _expected_moves({"GR1", "GR2", "GR3"})


def test_manager_delivery_before_invoice_moves_stock_at_delivery(lifecycle):
    """RED before the change: DN-2 is ignored, so its 2 widgets never leave on 01-16."""
    full = _manifest(lifecycle)
    assert [m for m in _moves(full) if m[1].startswith(ref("DN2"))] == _expected_moves({"DN2"})
    assert _documents(full)[ref("INVE")].issue_date == date(2026, 1, 22)

    # Delivered before the cutover, invoiced after it: the delivery is inside the opening
    # position and is not moved again, while the invoice still names it.
    cut = _manifest(lifecycle, CUTOVER)
    assert not [m for m in _moves(cut) if m[1].startswith(ref("DN2"))]
    assert _links(_documents(cut)[ref("INVE")]) == [(ref("DN2"), "2026-01-16", 0, D("2"), D("9.50"))]


def test_manager_physical_movement_with_separate_financial_timing(lifecycle):
    """RED before the change: the books take inventory in and out on the bill and invoice
    dates and cost of sales is never posted, so inventory on hand holds all 122.00 bought.

    The books follow the invoices: 122.00 bought less 13 sold at the 5.00 unit cost is
    57.00 on hand. The stock follows the physical records: 10 widgets worth 47.50."""
    from celerp.importers.adapters.manager_io.book import read_book
    from celerp.importers.adapters.manager_io.ledger import build_ledger
    from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader

    with ManagerReader(lifecycle) as reader:
        book = read_book(reader)
    ledger = build_ledger(book, FULL)
    balances: dict[str, D] = {}
    for p in ledger.postings:
        balances[p.account] = balances.get(p.account, D(0)) + p.amount
    assert {account: balances[ref(account)] for account in _FULL["balances"]} == {
        account: D(amount) for account, amount in _FULL["balances"].items()}
    assert _held(_manifest(lifecycle)) == {WID: HELD}


def test_manager_partial_delivery_stays_partial(lifecycle):
    """RED before the change: DN-3 is ignored, so none of INV-P's 5 widgets leave."""
    manifest = _manifest(lifecycle)
    invoice = _documents(manifest)[ref("INVP")]
    assert invoice.line_items[0].quantity == D("5")
    assert _links(invoice) == [(ref("DN3"), "2026-01-19", 0, D("3"), D("14.25"))]
    assert [m for m in _moves(manifest) if m[1].startswith(ref("DN3"))] == _expected_moves({"DN3"})


def test_manager_delivery_receipt_linkage_preserved(lifecycle):
    """RED before the change: no document names the physical records that moved its goods."""
    for decisions in (FULL, CUTOVER):
        documents = _documents(_manifest(lifecycle, decisions))
        for label, links in LINKS.items():
            assert _links(documents[ref(label)]) == [(ref(m), day, line, qty, value)
                                                    for m, day, line, qty, value in links], label


def test_manager_no_double_count_invoice_and_physical_record(lifecycle):
    """RED before the change: the bills move stock and the physical records do not, so a
    bill received by goods receipt is counted on its own date."""
    manifest = _manifest(lifecycle)
    sources = [m[1].split(":stock:")[0] for m in _moves(manifest)]
    assert len(sources) == len(set(sources)) == len(MOVES)
    for label in ("BILLG", "BILLP", "BILLU", "INVD", "INVE", "INVP", "INVN"):
        assert ref(label) not in sources, label
    assert _held(manifest) == {WID: HELD}


@pytest.mark.parametrize("decisions", [
    {"mode": "full_history"},
    {"mode": "cutover", "cutover_date": specs.LIFECYCLE_CUTOVER.isoformat()},
])
async def test_manager_single_default_location_imports_with_exact_item_location_reconciliation(
    real_engine, monkeypatch, tmp_path, decisions,
):
    """RED before the change: the goods receipts and delivery notes are not carried, so the
    item holds the 26 widgets bought instead of the 10 on hand, worth 47.50."""
    from sqlalchemy import select

    from celerp.models.company import Location
    from migration_support import maker
    from test_migration_e2e import _maps, _passing, _projections, migrate

    run, rejected = await migrate(real_engine, INVENTORY.read_bytes(), "lifecycle.manager", decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    held = {(r["check"], r["key"]): D(str(r["celerp"])) for r in run.reconciliation["rows"]
            if r["check"] in ("inventory_quantity", "inventory_value")}
    assert held == {("inventory_quantity", WID): HELD[0], ("inventory_value", WID): HELD[1]}

    maps = {(m.source_type, m.source_external_id): m.target_entity_id for m in await _maps(real_engine, run)}
    items = await _projections(real_engine, run, "item")
    parent = items[maps[("InventoryItem", WID)]]
    assert (D(str(parent["quantity"])), D(str(parent["cost_total"]))) == HELD
    async with maker(real_engine)() as s:
        (default,) = (await s.execute(select(Location.id).where(
            Location.company_id == run.company_id, Location.is_default.is_(True)))).scalars().all()
    assert parent["location_id"] == str(default)

    # Delivered goods left the location as sold lots of the invoices they were delivered on,
    # including a delivery made before the cutover on an invoice carried after it.
    sold = sorted((i["status_doc_id"], float(i["quantity"])) for i in items.values() if i.get("status") == "sold")
    assert sold == sorted((maps[("SalesInvoice", ref(label))], qty) for label, qty in (
        ("INVD", 4.0), ("INVE", 2.0), ("INVP", 3.0), ("INVX", 1.0)))
