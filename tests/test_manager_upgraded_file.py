# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A business file an older Manager has upgraded to the current format imports like one
created there.

Manager's upgrade keeps each record's old fields beside the current ones, adds fields
current Manager writes (a billing address, custom field values, a bank clearance status,
an inventory valuation method, a statement position and heading), stores the form defaults of a few
record types as objects keyed by their type, and leaves objects of types Manager itself
no longer reads. None of these changes the books, so none may stop the scan."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.support import adapter, artifact

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)

# Types an older Manager stored, kept in upgraded files: written out independently of the
# adapter's type table.
BUSINESS_LOGO = uuid.UUID("096d0af9-df72-425d-aae8-d59c0497f119")
RECEIPT_OR_PAYMENT = uuid.UUID("699401fe-23fd-4b52-9699-b88b36fa6b26")
ACTIVATION_KEY = uuid.UUID("9c3968b2-d79a-4134-837a-b906a4ee0c60")
PURCHASE_OVERPAYMENT_FIX = uuid.UUID("a0ef856b-8e18-43a3-91af-9b085abe5574")
SALES_OVERPAYMENT_FIX = uuid.UUID("a94200fd-d1f6-4a09-ab71-707f362d4a15")
TELEPHONE_FIELD, REGION_FIELD = specs.k("CF-TEL"), specs.k("CF-REGION")
MIN_DATE = {1: 1, 2: 15}                       # .NET DateTime.MinValue as protobuf-net writes it

BILL_TO = "90 Example Road\r\nExample City 4NB\r\n"


def _old_line(description: str, quantity: int, price: str) -> dict:
    """A line in the layout older Manager versions wrote, kept beside the current lines."""
    return {1: description, 6: {1: quantity}, 9: specs.k("WID"), 12: D(price)}


def _old_settlement(day: date, amount: str) -> dict:
    return {1: day, 2: specs.k("OPB"), 4: {2: specs.AR, 12: D(amount)}, 16: True, 21: True}


UPGRADE = {
    "INV1": {4: BILL_TO, 5: [_old_line("Consulting hours", 2, "50")], 21: 1, 48: True},
    "BILL1": {4: [_old_line("Widgets", 10, "4")], 22: True},
    "CN1": {4: BILL_TO, 5: [_old_line("Service credit", 1, "10")], 21: True},
    "CA": {4: "0522-556721", 12: [{1: TELEPHONE_FIELD, 2: "0522-556721"}]},
    "SA": {3: "(503) 555-9931", 9: [{1: TELEPHONE_FIELD, 2: "(503) 555-9931"}, {1: REGION_FIELD, 2: "North"}]},
    "R1": {6: "Acme Trading", 14: True, 21: _old_settlement(date(2026, 1, 20), "130"), 36: 1},
    "P1": {6: "Acme Trading", 14: True, 21: _old_settlement(date(2026, 2, 10), "50"), 37: 1},
    "TRADING": {2: "40", 5: 3},
}


def _upgraded(changes: dict[str, dict] = UPGRADE, extra: tuple = ()) -> list:
    out = []
    for o in specs.basic_objects():
        label = next((lbl for lbl in changes if o.key == specs.k(lbl)), None)
        out.append(dataclasses.replace(o, fields={**o.fields, **changes[label]}) if label else o)
    # A built-in account placed under a heading of the user's chart.
    builtin = specs.T["ProfitAndLossStatementAccountCurrencyGainsLosses"]
    out = [dataclasses.replace(o, fields={**o.fields, 3: specs.k("TRADING"), 10: 2}) if o.key == builtin else o
           for o in out]
    return [
        *out,
        # Form defaults: one object per record type, keyed by the type itself.
        specs.Obj(specs.T["SalesInvoice"], specs.T["SalesInvoice"], {1: MIN_DATE, 43: 1}),
        specs.Obj(specs.T["InventoryItem"], specs.T["InventoryItem"], {8: MIN_DATE, 38: 1}),
        # Objects of types current Manager no longer reads.
        specs.Obj(specs.k("LOGO"), BUSINESS_LOGO, {1: "image/png", 3: specs.PNG}),
        specs.Obj(RECEIPT_OR_PAYMENT, RECEIPT_OR_PAYMENT, {1: MIN_DATE, 12: 1, 16: 1}),
        specs.Obj(ACTIVATION_KEY, ACTIVATION_KEY, {2: 288707288}),
        specs.Obj(PURCHASE_OVERPAYMENT_FIX, PURCHASE_OVERPAYMENT_FIX, {}),
        specs.Obj(SALES_OVERPAYMENT_FIX, SALES_OVERPAYMENT_FIX, {}),
        *extra,
    ]


def _source(tmp_path, objects, name="upgraded.manager"):
    return artifact(specs.write_manager_file(tmp_path / name, objects, specs.basic_blobs()))


def _rows(source) -> dict:
    return {r.source_type: r for r in adapter().inspect([source]).coverage}


def _books(manifest) -> dict:
    """What the migration posts: every document, settlement, journal and transfer, figure by figure."""
    bundle = manifest.bundle
    return {
        "documents": [d.model_dump(exclude={"metadata"}) for d in bundle.documents],
        "settlements": [s.model_dump() for s in bundle.settlements],
        "journals": [j.model_dump() for j in bundle.journals],
        "transfers": [t.model_dump() for t in bundle.bank_transfers],
        "accounts": [a.model_dump() for a in bundle.accounts],
    }


def test_upgraded_file_scans_without_blockers_and_posts_the_same_books(tmp_path):
    """RED before the change: the old fields, the new fields, the form defaults and the
    obsolete objects each stop the scan as an unknown field or an unknown type."""
    upgraded = _source(tmp_path, _upgraded())
    rows = _rows(upgraded)
    stopping = {label: r.note for label, r in rows.items()
                if r.coverage_class in (CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER, CoverageClass.UNCLASSIFIED)}
    assert stopping == {}
    plain = _source(tmp_path, specs.basic_objects(), "plain.manager")
    assert _books(adapter().build_manifest([upgraded], FULL)) == _books(adapter().build_manifest([plain], FULL))


def test_upgraded_file_carries_billing_address_and_telephone(tmp_path):
    """RED before the change: the scan stops on the billing address and the telephone."""
    bundle = adapter().build_manifest([_source(tmp_path, _upgraded())], FULL).bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    assert docs[str(specs.k("INV1"))].metadata["billing_address"] == BILL_TO
    assert docs[str(specs.k("CN1"))].metadata["billing_address"] == BILL_TO
    assert "billing_address" not in docs[str(specs.k("INV2"))].metadata
    contacts = {c.source_external_id: c for c in bundle.contacts if c.source_type in ("Customer", "Supplier")}
    assert contacts[str(specs.k("CA"))].phone == "0522-556721"
    assert contacts[str(specs.k("SA"))].phone == "(503) 555-9931"


def test_upgraded_file_discloses_only_what_is_left_behind(tmp_path):
    """RED before the change: the scan stops before any loss is listed, and the payer name
    on a receipt from a customer is reported lost though it is that customer's name."""
    rows = _rows(_source(tmp_path, _upgraded()))
    # The supplier's region custom field has no Celerp equivalent; its telephone is carried.
    assert rows["Supplier (custom fields)"].count == 1
    assert rows["Supplier (custom fields)"].coverage_class == CoverageClass.MAPPED_WITH_LOSS
    assert "Customer (custom fields)" not in rows
    # Only the walk-in receipt's typed payer name is left behind: the customer receipt and
    # the supplier payment name the contact they settle.
    assert rows["Receipt (payer name)"].count == 1
    assert "Payment (payer name)" not in rows
    assert not [label for label in rows if "clearance" in label]
    assert rows["Obsolete81.BusinessLogo"].coverage_class == CoverageClass.UNSUPPORTED_NONFINANCIAL
    for label in ("Obsolete66.ReceiptOrPayment", "Obsolete39.ActivationKey39",
                  "Obsolete34.PurchaseInvoiceOverpaymentFix34", "Obsolete34.SalesInvoiceOverpaymentFix34",
                  "SalesInvoice form defaults", "InventoryItem form defaults"):
        assert rows[label].coverage_class == CoverageClass.IGNORED_NON_BUSINESS, label


def test_bank_clearance_date_is_disclosed(tmp_path):
    """RED before the change: the clearance date stops the scan as an unknown field."""
    changes = {**UPGRADE, "R1": {**UPGRADE["R1"], 9: date(2026, 1, 24)}}
    rows = _rows(_source(tmp_path, _upgraded(changes)))
    assert rows["Receipt (bank clearance date)"].count == 1
    assert rows["Receipt (bank clearance date)"].coverage_class == CoverageClass.MAPPED_WITH_LOSS


@pytest.mark.parametrize("method", [{}, {38: 0}, {38: 1}, {38: 2}], ids=["default", "fifo", "average", "manual"])
@pytest.mark.parametrize(("unit_costs", "cost"), [((), None), ((("UCF1", date(2026, 1, 1), D("4.5")),), D("67.50"))],
                         ids=["no_unit_cost", "unit_cost"])
def test_cost_of_sales_is_the_unit_cost_whatever_the_valuation_method(tmp_path, method, unit_costs, cost):
    """10 bought at 4, then 10 at 6, then 15 sold. Manager books the sale at 15 x the unit
    cost set on or before its date, 15 x 4.50 = 67.50, or at nothing when none is set,
    whatever the item's valuation method: the method only shapes what its cost correction
    suggests. RED before the change: first in, first out costs 70, average 75, and a manual
    method stops the scan."""
    def valued(o):
        return {**{n: v for n, v in o.fields.items() if n != 38}, **method}
    objects = [dataclasses.replace(o, fields=valued(o)) if o.key == specs.k("WID") else o
               for o in specs._stocked_masters()]
    objects += [specs.obj("InventoryUnitCost", label, {1: day, 2: specs.k("WID"), 3: unit})
                for label, day, unit in unit_costs]
    objects += [specs._bill("BF1", date(2026, 1, 5), {"WID": (D("10"), D("4"))}),
                specs._bill("BF2", date(2026, 1, 6), {"WID": (D("10"), D("6"))}),
                specs._sale("SF1", date(2026, 1, 10), {"WID": D("15")})]
    source = _source(tmp_path, objects, "costed.manager")
    assert "InventoryItem (unsupported feature)" not in _rows(source)
    bundle = adapter().build_manifest([source], FULL).bundle
    sale = next(d for d in bundle.documents if d.source_external_id == str(specs.k("SF1")))
    assert sale.line_items[0].cost_basis == cost


def test_unknown_type_still_stops_the_scan(tmp_path):
    """An object of a type that is neither current nor one Manager's upgrade leaves behind
    still stops Full history: its meaning is unknown."""
    stray = uuid.UUID("0e1d2c3b-4a59-4687-9a1b-2c3d4e5f6a7b")
    rows = _rows(_source(tmp_path, _upgraded(extra=(specs.Obj(stray, stray, {2: 1}),))))
    assert rows[f"Unknown type {stray}"].coverage_class == CoverageClass.UNCLASSIFIED


@pytest.mark.parametrize("method", [{}, {38: 1}])
def test_sales_after_an_item_is_oversold_are_refused(tmp_path, method):
    """10 bought, 15 sold, 10 more bought, 5 sold. Manager clears the 5 oversold with later
    cost corrections, so the second sale's cost is not what Celerp can work out either.
    RED before the change: only the first sale is refused, and the second is costed from a
    position Manager does not book."""
    def valued(o):
        return {**{n: v for n, v in o.fields.items() if n != 38}, **method}
    objects = [dataclasses.replace(o, fields=valued(o)) if o.key == specs.k("WID") else o
               for o in specs._stocked_masters()]
    objects += [specs._bill("BO1", date(2026, 1, 5), {"WID": (D("10"), D("4"))}),
                specs._sale("SO1", date(2026, 1, 6), {"WID": D("15")}),
                specs._bill("BO2", date(2026, 1, 7), {"WID": (D("10"), D("4"))}),
                specs._sale("SO2", date(2026, 1, 8), {"WID": D("5")})]
    rows = _rows(_source(tmp_path, objects, "oversold.manager"))
    assert rows["SalesInvoice (negative quantity owned)"].count == 2
