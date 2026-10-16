# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager's inventory records beyond buying and selling: locations, transfers between
them, write-offs and production orders.

Every location is carried, Manager's default first, and the stock Manager holds at any of
them arrives at the default location, listed as such, to be moved with a Celerp transfer.
A transfer between locations changes no figure, so it is listed as not carried. A
write-off takes its stock out at the item's unit cost on its date, or at nothing when none
is set, as Manager books it, against its write-off account. A production order takes the
items it consumes out at their unit cost and brings in what it makes at that cost plus the
expenses it adds, credited to their accounts. Every figure is worked by hand in the
docstring of its spec."""

from __future__ import annotations

import re
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.support import actual_rows, adapter, artifact, ref
from migration_support import real_client, real_engine  # noqa: F401 - fixtures
from test_migration_inventory_provenance import OFF_HAND, _migrated, _position

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
WID, GAD = ref("WID"), ref("GAD")
INVENTORY = ref("@BalanceSheetInventoryOnHandAccount")
WRITE_OFFS = ref("@ProfitAndLossStatementAccountInventoryWriteOffs")
STOPPING = (CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER, CoverageClass.UNCLASSIFIED)


def _source(tmp_path, objects, name: str = "stock.manager"):
    return artifact(specs.write_manager_file(tmp_path / name, objects))


def _rows(source) -> dict:
    return {r.source_type: r for r in adapter().inspect([source]).coverage}


def _held(manifest) -> dict[str, tuple[D, D]]:
    held: dict[str, tuple[D, D]] = {}
    for a in manifest.bundle.inventory_adjustments:
        qty, value = held.get(a.item_external_id, (D(0), D(0)))
        held[a.item_external_id] = (qty + a.quantity, value + (a.value or D(0)))
    return held


def _journal(manifest, label: str) -> list[tuple[str, D, D]]:
    lines = [(line.account_external_id, line.debit, line.credit) for j in manifest.bundle.journals
             if j.source_external_id.split(":")[0] == ref(label) for line in j.lines]
    return sorted(lines)


def _stock_rows(rows) -> dict[tuple[str, str], D]:
    return {(measure, key): value for measure, key, _, value in rows
            if (measure, key) in {("inventory_quantity", WID), ("inventory_value", WID), ("inventory_quantity", GAD),
                                  ("inventory_value", GAD), ("trial_balance", INVENTORY)}}


def test_locations_transfers_write_offs_and_production_scan_without_blockers(tmp_path):
    """RED before the change: the custom location, the transfer, the write-off, the
    production order and the automatic revaluation each stop the scan, and so does the
    flagged bill that brings stock into Store 2."""
    rows = _rows(_source(tmp_path, specs.stock_records_objects()))
    assert {label: r.note for label, r in rows.items() if r.coverage_class in STOPPING} == {}
    assert (rows["CustomInventoryLocation"].coverage_class, rows["CustomInventoryLocation"].target) == (
        CoverageClass.MAPPED_WITH_LOSS, "location")
    for label in ("InventoryWriteOff", "ProductionOrder"):
        assert (rows[label].coverage_class, rows[label].target) == (CoverageClass.MAPPED, "inventory_adjustment")
    assert rows["InventoryTransfer"].coverage_class == CoverageClass.UNSUPPORTED_NONFINANCIAL
    assert "not carried" in rows["InventoryTransfer"].note
    assert rows["InventoryAutomaticRevaluation"].coverage_class == CoverageClass.IGNORED_NON_BUSINESS
    for label in ("PurchaseInvoice (location)", "InventoryWriteOff (location)"):
        assert (rows[label].count, rows[label].coverage_class) == (1, CoverageClass.MAPPED_WITH_LOSS), label
        assert "default location" in rows[label].note, label


def test_every_location_is_carried_with_the_default_first(tmp_path):
    """RED before the change: the scan stops on the custom location, so no location is
    carried and Manager's default location arrives under Celerp's own default name."""
    manifest = adapter().build_manifest([_source(tmp_path, specs.stock_records_objects())], FULL)
    assert [(loc.source_external_id, loc.name) for loc in manifest.bundle.locations] == [
        (str(specs.T["DefaultInventoryLocation"]), "Main warehouse"), (ref("LOC2"), "Store 2")]


def test_write_off_and_production_move_stock_and_post_as_manager_books_them(tmp_path):
    """RED before the change: the scan stops, so nothing is carried.

    WO1 takes 2 widgets out at 4.00 against Inventory - write-offs; PO1 takes 3 widgets out
    at 12.00 and brings in 1 gadget at 18.00, the 6.00 of expenses it adds credited to
    office expenses. 5 widgets / 20.00 and 1 gadget / 18.00 are left, and the inventory
    account is 38.00, in the bundle and in the figures the import is checked against."""
    manifest = adapter().build_manifest([_source(tmp_path, specs.stock_records_objects())], FULL)
    moved = {(a.source_type, a.item_external_id): (a.quantity, a.value)
             for a in manifest.bundle.inventory_adjustments if a.source_type in ("InventoryWriteOff", "ProductionOrder")}
    assert moved == {("InventoryWriteOff", WID): (D("-2"), D("-8.00")), ("ProductionOrder", WID): (D("-3"), D("-12.00")),
                     ("ProductionOrder", GAD): (D("1"), D("18.00"))}
    assert _held(manifest) == {WID: (D("5"), D("20.00")), GAD: (D("1"), D("18.00"))}
    assert _journal(manifest, "WO1") == [(INVENTORY, D(0), D("8.00")), (WRITE_OFFS, D("8.00"), D(0))]
    assert _journal(manifest, "PO1") == [(INVENTORY, D("6.00"), D(0)), (ref("OFF"), D(0), D("6.00"))]
    expected = {("inventory_quantity", WID): D("5"), ("inventory_value", WID): D("20.00"),
                ("inventory_quantity", GAD): D("1"), ("inventory_value", GAD): D("18.00"),
                ("trial_balance", INVENTORY): D("38.00")}
    assert _stock_rows(actual_rows(manifest.reconciliation_expectations)) == expected
    source = adapter().source_expectations([_source(tmp_path, specs.stock_records_objects())], FULL)
    assert _stock_rows(actual_rows(source)) == expected


def test_write_off_with_no_unit_cost_takes_out_nothing(tmp_path):
    """RED before the change: the write-off stops the scan.

    With no unit cost set Manager books WO0 at nothing: the widget leaves at 0.00, no
    journal posts, and 4 widgets worth 20.00 are left, as is the inventory account."""
    manifest = adapter().build_manifest([_source(tmp_path, specs.unpriced_write_off_objects())], FULL)
    assert [(a.quantity, a.value) for a in manifest.bundle.inventory_adjustments
            if a.source_type == "InventoryWriteOff"] == [(D("-1"), D("0.00"))]
    assert _journal(manifest, "WO0") == []
    assert _held(manifest) == {WID: (D("4"), D("20.00"))}
    assert _stock_rows(actual_rows(manifest.reconciliation_expectations)) == {
        ("inventory_quantity", WID): D("4"), ("inventory_value", WID): D("20.00"),
        ("trial_balance", INVENTORY): D("20.00")}


@pytest.mark.parametrize("objects, label", [
    (specs.over_owned_write_off_objects, "InventoryWriteOff (negative quantity owned)"),
    (specs.unflagged_production_expense_objects, "ProductionOrder (unsupported feature)"),
])
def test_stock_record_manager_books_differently_is_refused(tmp_path, objects, label):
    """RED before the change: the record stops the scan as an unsupported type, not for
    what it holds.

    A write-off of more than the business owns relies on Manager's negative inventory
    clearing, and a production order listing expenses it does not add to its cost holds
    figures Manager leaves out; each is refused naming the record."""
    with pytest.raises(ScanError, match=re.escape(f"{label} (1)")):
        adapter().build_manifest([_source(tmp_path, objects())], FULL)


async def test_migrated_stock_records_leave_the_stock_and_inventory_account_manager_shows(
    real_engine, real_client, monkeypatch, tmp_path,
):
    """RED before the change: the scan stops, so no company is migrated.

    After the import, 5 widgets / 20.00 and 1 gadget / 18.00 are on hand, the inventory
    account is 38.00, and both locations exist, Manager's default as the default."""
    from sqlalchemy import select

    from celerp.models.company import Location
    from migration_support import maker
    from test_migration_e2e import _projections

    source = specs.write_manager_file(tmp_path / "stock.manager", specs.stock_records_objects())
    books = await _migrated(real_engine, monkeypatch, tmp_path, source=source)
    assert await _position(books) == {"stock": (D("5"), D("20.00")), "inventory": D("38.00")}
    gadgets = [i for i in (await _projections(real_engine, books.run, "item")).values()
               if i.get("sku") == "GAD-1" and i.get("status") not in OFF_HAND]
    assert (sum(D(str(i.get("quantity") or 0)) for i in gadgets),
            sum(D(str(i.get("cost_total") or 0)) for i in gadgets).quantize(D("0.01"))) == (D("1"), D("18.00"))
    async with maker(real_engine)() as s:
        locations = (await s.execute(select(Location.name, Location.is_default)
                                     .where(Location.company_id == books.run.company_id))).all()
    assert sorted(locations) == [("Main warehouse", True), ("Store 2", False)]
