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


def _links(document, item: str = WID) -> list[tuple]:
    """The physical records that moved one item of a document, as (movement, date, line,
    quantity, value)."""
    key = "deliveries" if document.doc_type == "invoice" else "receipts"
    return [(m["source"], m["date"], line["line"], D(line["quantity"]), D(line["value"]))
            for m in document.metadata.get(key) or [] for line in m["lines"]
            if line["item"] == item]


def _inventory_rows(manifest) -> set:
    return {(measure, key, value) for measure, key, _, value in actual_rows(manifest.reconciliation_expectations)
            if measure in ("inventory_quantity", "inventory_value")}


def test_manager_stock_matches_independent_source_oracle(lifecycle):
    """RED before the change: stock moves on the bills at their own dates and quantities,
    the goods receipts, delivery notes and unit costs are ignored, and sales never take
    stock out, so the item holds the 26 bought at 122.00 rather than 10 at 45.00."""
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
    assert _links(invoice) == [(ref("DN1"), "2026-01-20", 0, D("4"), D("20.00"))]


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
    assert _links(_documents(cut)[ref("INVE")]) == [(ref("DN2"), "2026-01-16", 0, D("2"), D("10.00"))]


def test_manager_physical_movement_with_separate_financial_timing(lifecycle):
    """RED before the change: the books take inventory in and out on the bill and invoice
    dates and cost of sales is never posted, so inventory on hand holds all 122.00 bought.

    The books follow the invoices: 122.00 bought less 13 sold at the 5.00 unit cost is
    57.00 on hand. The stock follows the physical records: 10 widgets worth 45.00."""
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
    assert _links(invoice) == [(ref("DN3"), "2026-01-19", 0, D("3"), D("15.00"))]
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
    item holds the 26 widgets bought instead of the 10 on hand, worth 45.00."""
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


# ── Two items, physical records straddling the cutover (specs.inventory_oracle_objects) ──
# Worked out by hand in the spec's docstring, never read back from the adapter.

GAD = ref("GAD")
ORACLE = {WID: (D("9"), D("29.71")), GAD: (D("2"), D("14.00"))}
ORACLE_OPENING = {WID: (D("17"), D("55.66")), GAD: (D("3"), D("21.00"))}
ORACLE_MODES = [
    (FULL, {"mode": "full_history"}),
    (MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=specs.ORACLE_CUTOVER),
     {"mode": "cutover", "cutover_date": specs.ORACLE_CUTOVER.isoformat()}),
]
ORACLE_CUT = ORACLE_MODES[1][0]


@pytest.fixture
def oracle(tmp_path):
    return specs.build_inventory_oracle(tmp_path / "oracle.manager")


def _oracle_rows(held: dict) -> set:
    return {row for item, (qty, value) in held.items()
            for row in (("inventory_quantity", item, qty), ("inventory_value", item, value))}


def _source_rows(path, decisions) -> set:
    expectations = adapter().source_expectations([artifact(path)], decisions)
    return {(measure, key, value) for measure, key, _, value in actual_rows(expectations)
            if measure in ("inventory_quantity", "inventory_value")}


def _adjusted(manifest, sign: int) -> dict[str, tuple[D, D]]:
    """Quantity and value moved per item by the stock adjustments going one way."""
    out: dict[str, tuple[D, D]] = {}
    for a in manifest.bundle.inventory_adjustments:
        if a.kind == "adjustment" and a.quantity * sign > 0:
            qty, value = out.get(a.item_external_id, (D(0), D(0)))
            out[a.item_external_id] = (qty + a.quantity, value + a.value)
    return out


def _sources(manifest) -> list[str]:
    """The source record behind each stock adjustment, one entry per record."""
    return sorted({a.source_external_id.split(":stock:")[0] for a in manifest.bundle.inventory_adjustments
                   if a.kind == "adjustment"})


async def _migrated_stock(real_engine, monkeypatch, tmp_path, path, decisions: dict):
    """Migrate *path*; return the run, its source-to-entity map, and its item projections."""
    from test_migration_e2e import _maps, _projections, migrate

    run, rejected = await migrate(real_engine, path.read_bytes(), "oracle.manager", decisions, monkeypatch, tmp_path)
    assert rejected == []
    maps = {(m.source_type, m.source_external_id): m.target_entity_id for m in await _maps(real_engine, run)}
    return run, maps, await _projections(real_engine, run, "item")


def _on_hand(maps, items) -> dict[str, tuple[D, D]]:
    return {item: (D(str(items[maps[("InventoryItem", item)]]["quantity"])),
                   D(str(items[maps[("InventoryItem", item)]]["cost_total"]))) for item in ORACLE}


def _sold(maps, items) -> dict[tuple[str, str], D]:
    """Quantity sold per (invoice source label, sku), from the sold lots the migration wrote."""
    invoices = {maps[("SalesInvoice", ref(label))]: label for label in ("SO1", "SO2", "SO3")}
    out: dict[tuple[str, str], D] = {}
    for item in items.values():
        if item.get("status") == "sold":
            key = (invoices[item["status_doc_id"]], item["sku"])
            out[key] = out.get(key, D(0)) + D(str(item["quantity"]))
    return out


@pytest.mark.parametrize("decisions, requested", ORACLE_MODES, ids=["full_history", "cutover"])
async def test_manager_stock_quantity_and_valuation_match_independent_source_oracle(
    real_engine, monkeypatch, tmp_path, oracle, decisions, requested,
):
    """RED before the change: goods receipts and delivery notes are not read and sales take
    no stock out, so each item holds what its bills list (WID 18, GAD 9) at the bill value.

    Every item's quantity and value at its one location equal the hand-worked figures: in
    the source oracle, in the manifest, in the reconciliation, and in the company."""
    from sqlalchemy import select

    from celerp.models.company import Location
    from migration_support import maker
    from test_migration_e2e import _passing

    assert _source_rows(oracle, decisions) == _oracle_rows(ORACLE)
    manifest = _manifest(oracle, decisions)
    assert _held(manifest) == ORACLE
    assert _inventory_rows(manifest) == _oracle_rows(ORACLE)

    run, maps, items = await _migrated_stock(real_engine, monkeypatch, tmp_path, oracle, requested)
    _passing(run)
    measured = {(r["check"], r["key"], D(r["celerp"])) for r in run.reconciliation["rows"]
                if r["check"] in ("inventory_quantity", "inventory_value")}
    assert measured == _oracle_rows(ORACLE)
    assert _on_hand(maps, items) == ORACLE
    async with maker(real_engine)() as s:
        (default,) = (await s.execute(select(Location.id).where(
            Location.company_id == run.company_id, Location.is_default.is_(True)))).scalars().all()
    held_at = {(items[maps[("InventoryItem", item)]]["location_id"], item) for item in ORACLE}
    assert held_at == {(str(default), item) for item in ORACLE}


@pytest.mark.parametrize("step", ["valuation", "linking", "stock adjustments"])
async def test_manager_source_oracle_independent_of_manifest_decode(real_engine, monkeypatch, tmp_path, step):
    """RED before the change: the oracle reads the valued stock movements the manifest is
    built from, so a step that mis-carries DN-1 moves the oracle with it and reconciliation
    passes the wrong stock.

    Each step is broken to mis-carry DN-1's 4 widgets. The oracle still states the 10
    widgets worth 45.00 the source lines give, and the run is refused at reconciliation."""
    from celerp.importers.adapters.manager_io import book as book_module
    from celerp.importers.adapters.manager_io import mappings
    from test_migration_e2e import migrate

    dn1 = ref("DN1")
    if step == "valuation":
        value_stock = book_module._value_stock

        def dropped(book):
            value_stock(book)
            book.moves[:] = [m for m in book.moves if m.key != dn1]
        monkeypatch.setattr(book_module, "_value_stock", dropped)
    elif step == "linking":
        link = book_module._link

        def halved(book, movement):
            link(book, movement)
            if movement.key == dn1:
                for line in movement.lines:
                    line.quantity /= 2
        monkeypatch.setattr(book_module, "_link", halved)
    else:
        stock = mappings._stock
        monkeypatch.setattr(mappings, "_stock", lambda book, ledger: [
            a.model_copy(update={"quantity": a.quantity / 2, "value": a.value / 2})
            if a.source_external_id.startswith(dn1) else a for a in stock(book, ledger)])

    assert _source_rows(INVENTORY, FULL) == {("inventory_quantity", WID, HELD[0]), ("inventory_value", WID, HELD[1])}
    assert _held(_manifest(INVENTORY)) != {WID: HELD}

    run, _ = await migrate(real_engine, INVENTORY.read_bytes(), "lifecycle.manager", {"mode": "full_history"},
                           monkeypatch, tmp_path)
    assert run.status == "failed"
    failing = {(r["check"], r["key"], D(r["source"])) for r in run.reconciliation["rows"] if r["result"] == "fail"}
    assert {("inventory_quantity", WID, HELD[0]), ("inventory_value", WID, HELD[1])} <= failing


def test_manager_physical_quantity_differs_from_financial_document(oracle):
    """RED before the change: stock moves the quantities the bills list, so SO-1's 7 widgets
    invoiced are never told apart from the 5 delivered, and BO-3's gadgets arrive unreceived.

    The documents keep what was billed and invoiced; stock moves what was received and
    delivered."""
    for decisions in (FULL, ORACLE_CUT):
        manifest = _manifest(oracle, decisions)
        documents = _documents(manifest)
        so1, bo1, bo3 = documents[ref("SO1")], documents[ref("BO1")], documents[ref("BO3")]
        assert [(ln.item_external_id, ln.quantity) for ln in so1.line_items] == [(WID, D("7")), (GAD, D("2"))]
        assert so1.total == D("127.50")
        assert [(q, v) for _, _, _, q, v in _links(so1)] == [(D("5"), D("15.94"))]
        assert [(q, v) for _, _, _, q, v in _links(so1, GAD)] == [(D("2"), D("14.00"))]
        assert [(ln.item_external_id, ln.quantity) for ln in bo1.line_items] == [(WID, D("10")), (GAD, D("4"))]
        assert [(m, q) for m, _, _, q, _ in _links(bo1, GAD)] == [(ref("GRO1"), D("3")), (ref("GRO2"), D("1"))]
        assert [(ln.item_external_id, ln.quantity) for ln in bo3.line_items] == [(GAD, D("5"))]
        assert bo3.total == D("40.00")
        assert _held(manifest) == ORACLE


def test_manager_bill_without_goods_receipt_moves_no_stock(oracle):
    """RED before the change: every bill moves its own quantity on its own date, so BO-3's
    5 gadgets come into stock though no goods receipt ever received them."""
    for decisions in (FULL, ORACLE_CUT):
        manifest = _manifest(oracle, decisions)
        bo3 = _documents(manifest)[ref("BO3")]
        assert bo3.issue_date == date(2026, 2, 9)
        assert _links(bo3, GAD) == []
        assert ref("BO3") not in _sources(manifest)
        assert _held(manifest)[GAD] == ORACLE[GAD]


def _adjustment_sources(labels) -> list[str]:
    return sorted(ref(label) for label in labels)


async def test_manager_no_double_count_invoice_and_delivery(real_engine, monkeypatch, tmp_path, oracle):
    """RED before the change: delivery notes are not read, so nothing leaves stock at all.

    Goods leave once, on the delivery note, or on the invoice only when it is flagged to
    move its own stock: never on both, and never on an invoice a delivery note fulfils."""
    manifest = _manifest(oracle)
    assert _sources(manifest) == _adjustment_sources(["GRO1", "GRA", "BO2", "DNA", "GRO2", "DNO1", "SO2"])
    assert _adjusted(manifest, -1) == {WID: (D("-9"), D("-29.29")), GAD: (D("-2"), D("-14.00"))}

    run, maps, items = await _migrated_stock(real_engine, monkeypatch, tmp_path, oracle, {"mode": "full_history"})
    assert _sold(maps, items) == {("SO1", "WID-1"): D("5"), ("SO1", "GAD-1"): D("2"), ("SO2", "WID-1"): D("3"),
                                  ("SO3", "WID-1"): D("1")}
    assert _on_hand(maps, items) == ORACLE


async def test_manager_no_double_count_bill_and_goods_receipt(real_engine, monkeypatch, tmp_path, oracle):
    """RED before the change: each bill moves its whole quantity and goods receipts are not
    read, so BO-1's gadgets arrive 4 on the bill date instead of 3 then 1 as received.

    Goods arrive once, on the goods receipt, or on the bill only when it is flagged to move
    its own stock: never on both, and never on a bill a goods receipt receives."""
    manifest = _manifest(oracle)
    assert _adjusted(manifest, 1) == {WID: (D("18"), D("59.00")), GAD: (D("4"), D("28.00"))}
    for label in ("BO1", "BO3", "BO4"):
        assert ref(label) not in _sources(manifest), label

    run, maps, items = await _migrated_stock(real_engine, monkeypatch, tmp_path, oracle, {"mode": "full_history"})
    from test_migration_e2e import _projections
    docs = await _projections(real_engine, run, "doc")
    received = {label: [line.get("quantity_received") for line in docs[maps[("PurchaseInvoice", ref(label))]]["line_items"]]
                for label in ("BO1", "BO4", "BO3")}
    assert received == {"BO1": [10, 4], "BO4": [2], "BO3": [None]}
    assert _on_hand(maps, items) == ORACLE


def test_manager_cutover_delivery_receipt_straddling_cutover(oracle):
    """RED before the change: goods receipts and delivery notes are not read, so the opening
    holds what the bills listed and nothing moves after the cutover but the bills.

    Across the 02-06 cutover, in both directions: BO-1 billed before and GRO-2 received
    after; BO-4 billed after and GRA received before; SO-1 invoiced before and DNO-1
    delivered after; SO-3 invoiced after and DNA delivered before. What moved before the
    cutover is in the opening and is not moved again; what moved after is moved once."""
    manifest = _manifest(oracle, ORACLE_CUT)
    opening = {a.item_external_id: (a.quantity, a.value) for a in manifest.bundle.inventory_adjustments
               if a.kind == "opening"}
    assert opening == ORACLE_OPENING
    assert {a.adjustment_date for a in manifest.bundle.inventory_adjustments if a.kind == "opening"} == {
        specs.ORACLE_CUTOVER}
    assert _moves(manifest) == [
        ("GoodsReceipt", f"{ref('GRO2')}:stock:1", date(2026, 2, 7), GAD, D("1"), D("7.00")),
        ("DeliveryNote", f"{ref('DNO1')}:stock:1", date(2026, 2, 7), WID, D("-5"), D("-15.94")),
        ("DeliveryNote", f"{ref('DNO1')}:stock:2", date(2026, 2, 7), GAD, D("-2"), D("-14.00")),
        ("SalesInvoice", f"{ref('SO2')}:stock:1", date(2026, 2, 8), WID, D("-3"), D("-10.01")),
    ]
    documents = _documents(manifest)
    assert _links(documents[ref("BO1")], GAD) == [(ref("GRO1"), "2026-02-03", 1, D("3"), D("21.00")),
                                                  (ref("GRO2"), "2026-02-07", 1, D("1"), D("7.00"))]
    assert _links(documents[ref("BO4")]) == [(ref("GRA"), "2026-02-04", 0, D("2"), D("8.00"))]
    assert _links(documents[ref("SO1")]) == [(ref("DNO1"), "2026-02-07", 0, D("5"), D("15.94"))]
    assert _links(documents[ref("SO3")]) == [(ref("DNA"), "2026-02-05", 0, D("1"), D("3.34"))]
    assert _held(manifest) == ORACLE
