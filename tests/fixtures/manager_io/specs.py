# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Specs of the synthetic Manager business files.

Each spec is a list of objects built from hand-written field numbers. Keys are
derived from a label with `k()`, so `checkpoints.json` can name accounts,
contacts and documents by label.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from .encoder import Blob, Obj, write_manager_file

_NS = uuid.UUID("5d1f0c4e-2b7a-4c1e-9a55-0c6f3e2d9b10")

# Content-type GUIDs, written out independently of the adapter's type table.
T = {name: uuid.UUID(guid) for name, guid in {
    "BusinessDetails": "38cf4712-6e95-4ce1-b53a-bff03edad273",
    "BaseCurrency": "39dde4fc-7af8-44cc-8572-3b1ff4cfb918",
    "ForeignCurrency": "6116531b-cb3d-4f85-b239-745972943a6b",
    "ExchangeRate": "14240c19-3d08-4fe6-94bb-6dd17c4bcda6",
    "LockDate": "4c5dac8f-2d5e-4634-a51b-0bbdd021a499",
    "BalanceSheetAccount": "6ef13e42-ad89-4d42-9480-546e0c04a411",
    "ProfitAndLossStatementAccount": "26b9e4a5-ce10-4f30-94c7-23a1ca4428f9",
    "BalanceSheetGroup": "c03d1921-7a45-4eda-8742-a2d9082dcf4f",
    "ProfitAndLossStatementGroup": "5770616c-0e01-46ca-a172-f7042275da6c",
    "Assets": "4c05c221-ca57-4c7c-be62-115669302ed4",
    "Liabilities": "ed5a19f6-12c5-45cc-b4b7-4e79f7ef50bc",
    "Equity": "9275ff4c-4cff-41d0-b7b5-f31c783f03d8",
    "BalanceSheetAccountsReceivableAccount": "d1489e95-bb28-4f5d-b42e-67d3291b3893",
    "BalanceSheetAccountsPayableAccount": "dac7ba37-0ccd-45e5-906e-548e6c50df37",
    "BalanceSheetTaxPayableAccount": "30c697fa-4196-438a-ab5a-1957478034b1",
    "BalanceSheetRetainedEarningsAccount": "74dfd025-d68e-4a99-9c78-5d43e17c0e09",
    "BalanceSheetInventoryOnHandAccount": "0fb45a62-fc42-43a8-a776-782e8b5ffc96",
    "ProfitAndLossStatementAccountCurrencyGainsLosses": "635ddd64-1176-4d35-b1c2-2d7d3bb12bb6",
    "BankOrCashAccount": "1408c33b-6284-4f50-9e31-48cbea21f3cf",
    "TaxCode": "7f368d97-8b7f-4b39-b156-dc66afd9496a",
    "Customer": "ec37c11e-2b67-49c6-8a58-6eccb7dd75ee",
    "Supplier": "6d2dc48d-2053-4e45-8330-285ebd431242",
    "InventoryItem": "0dbdbf8a-d80c-48e6-b453-bb7862445b7c",
    "SalesInvoice": "ad12b60b-23bf-4421-94df-8be79cef533e",
    "PurchaseInvoice": "58b9eb90-f6b8-4abc-8ea1-12fd77b8336e",
    "CreditNote": "245e5943-0092-409d-96ae-e2ee10eac75b",
    "DebitNote": "274fc6d0-2eac-43d0-8286-79c856e644aa",
    "Receipt": "7662b887-c8d8-486e-98fd-f9dbcd41c6dc",
    "Payment": "79f99d26-e43a-4ecb-a9c9-0774601a9b2e",
    "InterAccountTransfer": "dea4f923-c498-4504-b3ef-30be3c33175e",
    "JournalEntry": "5ea52bc4-90ae-4e4a-aec4-ef1224b279ad",
    "Attachment": "2e541a82-94d7-42fc-a388-26bdc0803455",
    "SalesQuote": "ba89de75-cb87-4bde-b20f-314f01b31037",
    "TrialBalance": "e5dc98ef-4662-4a68-8a9d-b3e2d12b55d6",
    "DeliveryNote": "a0f6a539-f6a4-4a38-a69a-546a608a1f6d",
    "GoodsReceipt": "866217a4-f841-47de-a4e6-87152405c88d",
    "InventoryTransfer": "7eaafddc-54c9-4235-98d2-e8a1ee438150",
    "CustomInventoryLocation": "fae8151d-252e-45e3-b1f4-e048075b8983",
    "DefaultInventoryLocation": "d63413bc-622e-4e39-86bc-15e95eb4e81c",
    "InventoryUnitCost": "d5d7bad5-0abd-4501-af7f-cb6289cabc30",
    "ProfitAndLossStatementAccountInventorySales": "ea44f579-9548-4954-baf0-48538aceff1e",
    "ProfitAndLossStatementAccountInventoryPurchases": "aa80b662-3642-4c08-b328-2fccf132ceb1",
}.items()}

INCOME = uuid.UUID("95713fac-30d3-42e4-b536-dd7bc4f7a80e")
EXPENSES = uuid.UUID("fd003045-876e-439e-b923-1904453f5c30")
# Built-in accounts are referenced by their content-type GUID.
AR, AP = T["BalanceSheetAccountsReceivableAccount"], T["BalanceSheetAccountsPayableAccount"]
CASH_AT_BANK = uuid.UUID("6d4af96a-0959-4bb2-9160-fa825ec67c43")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24 + b"synthetic image"

# Enum values used in the specs.
PAID_BY_OTHER, PAID_BY_CUSTOMER, PAID_BY_SUPPLIER = 0, 1, 2
CUSTOM_RATE, SINGLE_RATE = 2, 0


def k(label: str) -> uuid.UUID:
    return uuid.uuid5(_NS, label)


def obj(type_name: str, label: str | None, fields: dict) -> Obj:
    ctype = T[type_name]
    return Obj(ctype if label is None else k(label), ctype, fields)


def _chart() -> list[Obj]:
    return [
        obj("Assets", None, {1: "Assets"}),
        obj("Liabilities", None, {1: "Liabilities"}),
        obj("Equity", None, {1: "Equity"}),
        obj("BalanceSheetAccountsReceivableAccount", None, {1: "Accounts receivable"}),
        obj("BalanceSheetAccountsPayableAccount", None, {1: "Accounts payable"}),
        obj("BalanceSheetTaxPayableAccount", None, {1: "Tax payable"}),
        obj("BalanceSheetRetainedEarningsAccount", None, {1: "Retained earnings"}),
        obj("BalanceSheetInventoryOnHandAccount", None, {1: "Inventory on hand"}),
        obj("ProfitAndLossStatementAccountCurrencyGainsLosses", None, {1: "Foreign exchange gains and losses"}),
    ]


def masters(company: str = "Example Trading") -> list[Obj]:
    """USD company, 2 decimal places, VAT 10%: the chart, contacts and item every spec shares."""
    return [
        obj("BusinessDetails", None, {1: company, 2: "1 Example Street, Example City"}),
        obj("BaseCurrency", None, {2: "US dollar", 3: "USD", 5: 2}),
        *_chart(),
        obj("BalanceSheetGroup", "OWNER_FUNDS", {1: "Owner funds", 3: T["Equity"]}),
        obj("ProfitAndLossStatementGroup", "TRADING", {1: "Trading income", 6: 0}),
        obj("ProfitAndLossStatementAccount", "S1", {1: "Sales", 11: "4000", 3: INCOME}),
        obj("ProfitAndLossStatementAccount", "S2", {1: "Sales", 11: "4010", 3: k("TRADING")}),
        obj("ProfitAndLossStatementAccount", "OFF", {1: "Office expenses", 11: "6100", 3: EXPENSES}),
        obj("BalanceSheetAccount", "EQ", {1: "Owner equity", 17: "3000", 3: k("OWNER_FUNDS")}),
        obj("BankOrCashAccount", "OPB", {1: "Operating bank", 13: "1010"}),
        obj("BankOrCashAccount", "PC", {1: "Petty cash", 13: "1020"}),
        obj("TaxCode", "VAT", {1: "VAT 10%", 5: CUSTOM_RATE, 6: SINGLE_RATE, 4: D("10")}),
        obj("Customer", "CA", {1: "Acme Trading", 13: "C-001", 3: "accounts@acme.example.com", 2: "2 Example Road"}),
        obj("Supplier", "SA", {1: "Acme Trading", 10: "S-001", 2: "accounts@acme.example.com", 7: "2 Example Road"}),
        obj("InventoryItem", "WID", {1: "WID-1", 11: "Widget", 13: "each", 32: True, 3: D("12.50"), 31: True, 2: D("4")}),
    ]


def funding(label: str, ref: str, day: date, amount: D) -> Obj:
    """Owner money paid into the operating bank."""
    return obj("JournalEntry", label, {1: day, 2: ref, 3: "Owner funding", 14: [
        {1: CASH_AT_BANK, 29: k("OPB"), 13: amount},
        {1: k("EQ"), 14: amount},
    ]})


def basic_objects(company: str = "Example Trading") -> list[Obj]:
    """The masters plus a quarter of trading. Figures are worked out in checkpoints.json."""
    vat = k("VAT")
    ca, sa = k("CA"), k("SA")
    opb, pc = k("OPB"), k("PC")
    s1, s2, off = k("S1"), k("S2"), k("OFF")
    wid = k("WID")
    return [
        *masters(company),
        funding("JE1", "JE-1", date(2026, 1, 2), D("500")),
        obj("SalesInvoice", "INV1", {1: date(2026, 1, 10), 2: "INV-1", 3: ca, 22: 30, 12: "Consulting", 49: [
            {2: s1, 17: "Consulting hours", 18: D("2"), 19: D("50"), 21: vat},
        ]}),
        obj("SalesInvoice", "INV2", {1: date(2026, 1, 15), 2: "INV-2", 3: ca, 8: True, 49: [
            {2: s1, 17: "Support, tax included", 18: D("1"), 19: D("55"), 21: vat},
        ]}),
        obj("Receipt", "R1", {1: date(2026, 1, 20), 2: "R-1", 3: PAID_BY_CUSTOMER, 4: ca, 7: opb, 11: [
            {2: AR, 3: ca, 4: k("INV1"), 18: D("110")},
            {2: AR, 3: ca, 4: k("INV2"), 18: D("20")},
        ]}),
        obj("PurchaseInvoice", "BILL1", {1: date(2026, 2, 1), 2: "BILL-1", 3: sa, 64: True, 23: [
            {1: wid, 17: "Widgets", 18: D("10"), 19: D("4"), 21: vat},
        ]}),
        obj("PurchaseInvoice", "BILL2", {1: date(2026, 2, 5), 2: "BILL-2", 3: sa, 64: True, 23: [
            {2: off, 17: "Stationery", 18: D("1"), 19: D("30")},
            {1: wid, 17: "Widgets", 18: D("5"), 19: D("4")},
        ]}),
        obj("Payment", "P1", {1: date(2026, 2, 10), 2: "P-1", 3: PAID_BY_SUPPLIER, 5: sa, 7: opb, 11: [
            {2: AP, 7: sa, 8: k("BILL2"), 18: D("50")},
        ]}),
        obj("Receipt", "R2", {1: date(2026, 2, 12), 2: "R-2", 3: PAID_BY_OTHER, 6: "Walk-in sale", 7: pc, 11: [
            {2: s2, 15: "Cash sale, tax included", 18: D("22"), 20: vat},
        ]}),
        obj("InterAccountTransfer", "IAT1", {1: date(2026, 2, 15), 6: "T-1", 2: opb, 8: D("15"), 3: pc, 9: D("15")}),
        obj("CreditNote", "CN1", {1: date(2026, 3, 5), 2: "CN-1", 3: ca, 8: k("INV2"), 22: [
            {2: s1, 17: "Service credit", 18: D("1"), 19: D("10"), 21: vat},
        ]}),
        obj("DebitNote", "DN1", {1: date(2026, 3, 6), 2: "DN-1", 3: sa, 6: k("BILL1"), 16: [
            {2: off, 17: "Price adjustment", 18: D("1"), 19: D("4"), 21: vat},
        ]}),
        obj("SalesInvoice", "INV3", {1: date(2026, 3, 10), 2: "INV-3", 3: ca, 31: True, 32: 0, 49: [
            {2: s1, 17: "Project work", 18: D("2"), 19: D("125"), 23: D("20")},
        ]}),
        obj("SalesQuote", "Q1", {1: date(2026, 3, 12), 2: "Q-1", 3: ca}),
        obj("TrialBalance", "TB", {1: "Trial balance"}),
        attachment_object("ATT1", "receipt-scan.png", PNG, k("INV1")),
    ]


def attachment_object(label: str, name: str, content: bytes, target: uuid.UUID, size: int | None = None) -> Obj:
    return obj("Attachment", label, {
        1: date(2026, 1, 10), 2: name, 4: len(content) if size is None else size, 6: target,
        12: hashlib.sha256(content).digest(),
    })


def basic_blobs() -> tuple[Blob, ...]:
    return (Blob(k("ATT1"), "receipt-scan.png", "image/png", PNG),)


FX_LOCKED_THROUGH = date(2026, 1, 31)


def fx_objects() -> list[Obj]:
    """USD base with EUR customer, supplier and bank, books locked through 01-31. Rates are
    USD per EUR unless inverse."""
    eur = k("EUR")
    cust, supp, ebank = k("ECUST"), k("ESUPP"), k("EBANK")
    return [
        obj("BusinessDetails", None, {1: "Example Exports"}),
        obj("BaseCurrency", None, {2: "US dollar", 3: "USD", 5: 2}),
        obj("ForeignCurrency", "EUR", {1: "Euro", 2: "EUR", 4: 2}),
        obj("ExchangeRate", "RATE1", {1: date(2026, 1, 1), 2: eur, 6: D("1.2")}),
        obj("ExchangeRate", "RATE2", {1: date(2026, 3, 31), 2: eur, 6: D("1.3")}),
        obj("LockDate", None, {1: FX_LOCKED_THROUGH, 2: True}),
        *_chart(),
        obj("ProfitAndLossStatementAccount", "SALES", {1: "Export sales", 3: INCOME}),
        obj("ProfitAndLossStatementAccount", "PURCH", {1: "Purchases", 3: EXPENSES}),
        obj("BankOrCashAccount", "EBANK", {1: "Euro bank", 3: eur}),
        obj("BankOrCashAccount", "MBANK", {1: "Main bank"}),
        obj("Customer", "ECUST", {1: "Euro customer", 14: eur}),
        obj("Supplier", "ESUPP", {1: "Euro supplier", 11: eur}),
        obj("SalesInvoice", "INVE1", {1: date(2026, 1, 10), 2: "INV-E1", 3: cust, 64: D("1.2"), 49: [
            {2: k("SALES"), 18: D("1"), 19: D("100")},
        ]}),
        obj("Receipt", "RE1", {1: date(2026, 2, 10), 2: "R-E1", 3: PAID_BY_CUSTOMER, 4: cust, 7: ebank, 45: D("1.25"), 11: [
            {2: AR, 3: cust, 4: k("INVE1"), 18: D("100")},
        ]}),
        obj("PurchaseInvoice", "BILLE1", {1: date(2026, 2, 15), 2: "BILL-E1", 3: supp, 61: D("0.8"), 62: True, 23: [
            {2: k("PURCH"), 18: D("1"), 19: D("80")},
        ]}),
    ]


# The cutover fixture's boundary: the day its opening position is taken.
CUTOVER_DATE = date(2026, 1, 31)


def cutover_objects() -> list[Obj]:
    """Trading on both sides of CUTOVER_DATE: a closed sale and a paid purchase before it, an
    invoice still open at it, and a receipt, purchase, transfer and journal after it. Stock
    moves on both sides. Figures are worked out in checkpoints.json under "cutover_fixture"."""
    vat, ca, sa, opb, pc = k("VAT"), k("CA"), k("SA"), k("OPB"), k("PC")
    s1, off, wid = k("S1"), k("OFF"), k("WID")
    return [
        *masters(),
        funding("JE1", "JE-1", date(2026, 1, 2), D("500")),
        obj("SalesInvoice", "INVA", {1: date(2026, 1, 10), 2: "INV-A", 3: ca, 49: [
            {2: s1, 17: "Consulting", 18: D("1"), 19: D("100"), 21: vat},
        ]}),
        obj("PurchaseInvoice", "BILLA", {1: date(2026, 1, 15), 2: "BILL-A", 3: sa, 64: True, 23: [
            {1: wid, 17: "Widgets", 18: D("10"), 19: D("4")},
        ]}),
        obj("Payment", "PA", {1: date(2026, 1, 18), 2: "P-A", 3: PAID_BY_SUPPLIER, 5: sa, 7: opb, 11: [
            {2: AP, 7: sa, 8: k("BILLA"), 18: D("40")},
        ]}),
        obj("Receipt", "RA", {1: date(2026, 1, 20), 2: "R-A", 3: PAID_BY_CUSTOMER, 4: ca, 7: opb, 11: [
            {2: AR, 3: ca, 4: k("INVA"), 18: D("110")},
        ]}),
        obj("SalesInvoice", "INVB", {1: date(2026, 1, 25), 2: "INV-B", 3: ca, 49: [
            {2: s1, 17: "Support", 18: D("2"), 19: D("50"), 21: vat},
        ]}),
        obj("Receipt", "RB", {1: date(2026, 2, 5), 2: "R-B", 3: PAID_BY_CUSTOMER, 4: ca, 7: opb, 11: [
            {2: AR, 3: ca, 4: k("INVB"), 18: D("110")},
        ]}),
        obj("PurchaseInvoice", "BILLB", {1: date(2026, 2, 8), 2: "BILL-B", 3: sa, 64: True, 23: [
            {1: wid, 17: "Widgets", 18: D("5"), 19: D("4")},
        ]}),
        obj("InterAccountTransfer", "IATB", {1: date(2026, 2, 10), 6: "T-B", 2: opb, 8: D("25"), 3: pc, 9: D("25")}),
        obj("JournalEntry", "JEB", {1: date(2026, 2, 12), 2: "JE-B", 3: "Stationery", 14: [
            {1: off, 13: D("15")},
            {1: CASH_AT_BANK, 29: opb, 14: D("15")},
        ]}),
    ]


BASIC_CHANGES = 5


def build_basic(path: Path, company: str = "Example Trading") -> Path:
    return write_manager_file(path, basic_objects(company), basic_blobs(), changes=BASIC_CHANGES)


def build_fx(path: Path) -> Path:
    return write_manager_file(path, fx_objects())


def build_cutover(path: Path) -> Path:
    return write_manager_file(path, cutover_objects())


# Attachment label -> the basic record it is attached to: a customer, an item, a receipt
# settling an invoice, a receipt kept whole as a journal, a transfer, a journal and a debit note.
ATTACHMENT_TARGETS = {"ACON": "CA", "AITEM": "WID", "AREC": "R1", "AFALL": "R2", "ATRF": "IAT1", "AJE": "JE1",
                      "ADN": "DN1"}


def build_attachment_targets(path: Path) -> Path:
    """The basic books with one more attachment on each record in ATTACHMENT_TARGETS."""
    objects = [*basic_objects(), *(attachment_object(label, f"{label.lower()}.png", PNG, k(target))
                                   for label, target in ATTACHMENT_TARGETS.items())]
    blobs = (*basic_blobs(), *(Blob(k(label), f"{label.lower()}.png", "image/png", PNG) for label in ATTACHMENT_TARGETS))
    return write_manager_file(path, objects, blobs)


def inactive_masters() -> list[Obj]:
    """Masters Manager marks inactive: a customer, a supplier, an item, a tax code and a currency."""
    return [
        obj("Customer", "CX", {1: "Former Customer", 13: "C-900", 15: True}),
        obj("Supplier", "SX", {1: "Former Supplier", 10: "S-900", 12: True}),
        obj("InventoryItem", "OLD", {1: "OLD-1", 11: "Retired widget", 13: "each", 10: True}),
        obj("TaxCode", "OLDTAX", {1: "Old rate 5%", 5: CUSTOM_RATE, 6: SINGLE_RATE, 4: D("5"), 10: True}),
        obj("ForeignCurrency", "OLDCUR", {1: "Euro", 2: "EUR", 4: 2, 6: True}),
    ]


def build_inactive_masters(path: Path) -> Path:
    """The basic books plus the inactive masters."""
    return write_manager_file(path, [*basic_objects(), *inactive_masters()], basic_blobs())


def _variant(label: str, day: int, line: dict, header: dict | None = None) -> Obj:
    return obj("SalesInvoice", label, {1: date(2026, 1, day), 2: label, 3: k("CA"), **(header or {}), 49: [
        {2: k("S1"), 17: label.lower(), **line}]})


def line_variant_objects() -> list[Obj]:
    """One unpaid invoice per line form: a percentage and a fixed discount, tax exclusive,
    tax inclusive, no tax, and a price whose line amount rounds at the currency precision."""
    vat = k("VAT")
    return [
        *masters(),
        _variant("LPCT", 5, {18: D("2"), 19: D("125"), 23: D("20")}, {31: True, 32: 0}),
        _variant("LFIX", 6, {18: D("1"), 19: D("100"), 24: D("15")}, {31: True, 32: 1}),
        _variant("LEXC", 7, {18: D("2"), 19: D("50"), 21: vat}),
        _variant("LINC", 8, {18: D("1"), 19: D("55"), 21: vat}, {8: True}),
        _variant("LNOT", 9, {18: D("1"), 19: D("30")}),
        _variant("LRND", 10, {18: D("3"), 19: D("3.335"), 21: vat}),
    ]


def build_line_variants(path: Path) -> Path:
    return write_manager_file(path, line_variant_objects())


# ── Physical stock movements ──────────────────────────────────────────────────
# Manager moves stock on the goods receipt or delivery note, or on the invoice itself when
# the invoice is flagged to move its own stock (field 64 on a bill, 69 on a sales invoice).

LIFECYCLE_CUTOVER = date(2026, 1, 17)


ITEM_NAMES = {"WID": "Widgets", "GAD": "Gadgets"}
SALE_PRICES = {"WID": D("12.50"), "GAD": D("20")}


def _moved_lines(lines: dict[str, D]) -> list[dict]:
    return [{1: k(item), 2: ITEM_NAMES[item], 3: qty} for item, qty in lines.items()]


def goods_receipt(label: str, day: date, bill: str, lines: dict[str, D], location: uuid.UUID | None = None) -> Obj:
    fields = {1: label, 3: day, 4: k("SA"), 15: _moved_lines(lines), 17: k(bill)}
    return obj("GoodsReceipt", label, fields | ({11: location} if location else {}))


def delivery_note(label: str, day: date, invoice: str, lines: dict[str, D]) -> Obj:
    return obj("DeliveryNote", label, {1: label, 3: day, 4: k("CA"), 15: _moved_lines(lines), 19: k(invoice)})


def _bill(label: str, day: date, lines: dict[str, tuple[D, D]], header: dict | None = None) -> Obj:
    """A bill with one line per item: {item: (quantity, unit price)}."""
    return obj("PurchaseInvoice", label, {1: day, 2: label, 3: k("SA"), **(header or {}), 23: [
        {1: k(item), 17: ITEM_NAMES[item], 18: qty, 19: price} for item, (qty, price) in lines.items()]})


def _sale(label: str, day: date, lines: dict[str, D], header: dict | None = None) -> Obj:
    """A sales invoice with one line per item at the item's sale price: {item: quantity}."""
    return obj("SalesInvoice", label, {1: day, 2: label, 3: k("CA"), **(header or {}), 49: [
        {1: k(item), 17: ITEM_NAMES[item], 18: qty, 19: SALE_PRICES[item]} for item, qty in lines.items()]})


def _stocked_masters() -> list[Obj]:
    """The shared masters with the inventory accounts and the default location Manager adds
    once inventory is used."""
    return [
        *masters(),
        obj("ProfitAndLossStatementAccountInventorySales", None, {1: "Inventory - sales"}),
        obj("ProfitAndLossStatementAccountInventoryPurchases", None, {1: "Inventory - cost"}),
        obj("DefaultInventoryLocation", None, {1: "Main warehouse"}),
    ]


def inventory_lifecycle_objects() -> list[Obj]:
    """One item bought and sold every way Manager moves stock. Hand-worked figures:

    Receipts (bill, physical record, quantity, value):
      BILLF 5 @ 6, flagged: moves its own stock on 01-06, 5 / 30.00
      BILLG 10 @ 4: GR1 01-08 6 / 24.00 at the default location, GR2 01-12 4 / 16.00
      BILLU 3 @ 4: no physical record, moves nothing
      BILLP 8 @ 5: GR3 01-10 5 / 25.00, the other 3 never arrive
    Running position: 01-06 5 / 30.00, 01-08 11 / 54.00, 01-10 16 / 79.00, 01-12 20 / 95.00.
    Deliveries at the moving average cost of 4.75:
      INVE 2 (01-22): DN2 01-16, delivered before the invoice, 9.50, leaves 18 / 85.50
      INVP 5 (01-18): DN3 01-19 delivers 3 only, 14.25, leaves 15 / 71.25
      INVD 4 (01-15): DN1 01-20, delivered after the invoice, 19.00, leaves 11 / 52.25
      INVN 1 (01-21): no physical record, moves nothing
      INVX 1 (01-23), flagged: moves its own stock, 4.75, leaves 10 / 47.50
    Books: bills 122.00 to payables, invoices 13 @ 12.50 = 162.50 to receivables, cost of
    sales 13 x 5.00 (the unit cost from 01-14) = 65.00, so inventory on hand is 57.00. R1
    pays INVD in full on 01-25. At the 01-17 cutover the opening stock is 18 / 85.50.
    These figures are recorded in checkpoints.json under "inventory"."""
    return [
        *_stocked_masters(),
        obj("InventoryUnitCost", "UC1", {1: date(2026, 1, 14), 2: k("WID"), 3: D("5")}),
        _bill("BILLG", date(2026, 1, 5), {"WID": (D("10"), D("4"))}),
        _bill("BILLF", date(2026, 1, 6), {"WID": (D("5"), D("6"))}, {64: True}),
        _bill("BILLU", date(2026, 1, 7), {"WID": (D("3"), D("4"))}),
        _bill("BILLP", date(2026, 1, 9), {"WID": (D("8"), D("5"))}),
        goods_receipt("GR1", date(2026, 1, 8), "BILLG", {"WID": D("6")}, T["DefaultInventoryLocation"]),
        goods_receipt("GR3", date(2026, 1, 10), "BILLP", {"WID": D("5")}),
        goods_receipt("GR2", date(2026, 1, 12), "BILLG", {"WID": D("4")}),
        _sale("INVD", date(2026, 1, 15), {"WID": D("4")}),
        _sale("INVE", date(2026, 1, 22), {"WID": D("2")}),
        _sale("INVP", date(2026, 1, 18), {"WID": D("5")}),
        _sale("INVN", date(2026, 1, 21), {"WID": D("1")}),
        _sale("INVX", date(2026, 1, 23), {"WID": D("1")}, {69: True}),
        delivery_note("DN2", date(2026, 1, 16), "INVE", {"WID": D("2")}),
        delivery_note("DN3", date(2026, 1, 19), "INVP", {"WID": D("3")}),
        delivery_note("DN1", date(2026, 1, 20), "INVD", {"WID": D("4")}),
        obj("Receipt", "R1", {1: date(2026, 1, 25), 2: "R-1", 3: PAID_BY_CUSTOMER, 4: k("CA"), 7: k("OPB"), 11: [
            {2: AR, 3: k("CA"), 4: k("INVD"), 18: D("50")},
        ]}),
    ]


def build_inventory_lifecycle(path: Path) -> Path:
    return write_manager_file(path, inventory_lifecycle_objects())


ORACLE_CUTOVER = date(2026, 2, 6)


def inventory_oracle_objects() -> list[Obj]:
    """Two items, each bought and sold across physical records that straddle the 02-06
    cutover in both directions. Hand-worked figures, receipts before deliveries on a day:

      02-03 GRO1 receives BO1's WID 10 of 10 @ 3.00 = 30.00 and GAD 3 of 4 @ 7.00 = 21.00
      02-04 GRA receives BO4's WID 2 @ 4.00 = 8.00, before BO4 is billed on 02-08
      02-05 BO2, flagged, brings in its own WID 6 @ 3.50 = 21.00: WID 18 / 59.00
      02-05 DNA delivers SO3's WID 1, before SO3 is invoiced on 02-09: 59.00 / 18 = 3.28,
            WID 17 / 55.72
      Opening at the 02-06 cutover: WID 17 / 55.72, GAD 3 / 21.00
      02-07 GRO2 receives BO1's last GAD 1, BO1 billed before the cutover: 28.00 - 21.00 = 7.00,
            GAD 4 / 28.00
      02-07 DNO1 delivers SO1's WID 5 of 7 and GAD 2 of 2, SO1 invoiced before the cutover:
            WID 55.72 x 5 / 17 = 16.39, 12 / 39.33; GAD 28.00 x 2 / 4 = 14.00, 2 / 14.00
      02-08 SO2, flagged, takes out its own WID 3: 39.33 x 3 / 12 = 9.83, WID 9 / 29.50
      02-09 BO3 bills GAD 5 @ 8.00 with no goods receipt: moves nothing
    On hand: WID 9 / 29.50, GAD 2 / 14.00, all at the default location."""
    return [
        *_stocked_masters(),
        obj("InventoryItem", "GAD", {1: "GAD-1", 11: "Gadget", 13: "each", 32: True, 3: SALE_PRICES["GAD"], 31: True}),
        _bill("BO1", date(2026, 2, 2), {"WID": (D("10"), D("3")), "GAD": (D("4"), D("7"))}),
        goods_receipt("GRO1", date(2026, 2, 3), "BO1", {"WID": D("10"), "GAD": D("3")}),
        goods_receipt("GRA", date(2026, 2, 4), "BO4", {"WID": D("2")}),
        _bill("BO2", date(2026, 2, 5), {"WID": (D("6"), D("3.50"))}, {64: True}),
        _sale("SO1", date(2026, 2, 5), {"WID": D("7"), "GAD": D("2")}),
        delivery_note("DNA", date(2026, 2, 5), "SO3", {"WID": D("1")}),
        goods_receipt("GRO2", date(2026, 2, 7), "BO1", {"GAD": D("1")}),
        delivery_note("DNO1", date(2026, 2, 7), "SO1", {"WID": D("5"), "GAD": D("2")}),
        _sale("SO2", date(2026, 2, 8), {"WID": D("3")}, {69: True}),
        _bill("BO4", date(2026, 2, 8), {"WID": (D("2"), D("4"))}),
        _sale("SO3", date(2026, 2, 9), {"WID": D("1")}),
        _bill("BO3", date(2026, 2, 9), {"GAD": (D("5"), D("8"))}),
    ]


def build_inventory_oracle(path: Path) -> Path:
    return write_manager_file(path, inventory_oracle_objects())


NEGATIVE_CUTOVER = date(2026, 1, 9)


def negative_recovery_objects() -> list[Obj]:
    """Stock that runs below zero and is later made good. 5 widgets come in on 01-05; a
    delivery of 9 on 01-08 and a flagged invoice taking out 6 on 01-09 each take more than
    is held; 10 more are received on 01-12. The history ends holding stock, but at the
    01-09 cutover Manager's opening position is 5 - 9 - 6 = -10."""
    return [
        *_stocked_masters(),
        _bill("BILLS", date(2026, 1, 5), {"WID": (D("5"), D("4"))}, {64: True}),
        _sale("INVNEG", date(2026, 1, 6), {"WID": D("9")}),
        delivery_note("DNNEG", date(2026, 1, 8), "INVNEG", {"WID": D("9")}),
        _sale("INVFN", date(2026, 1, 9), {"WID": D("6")}, {69: True}),
        _bill("BILLR", date(2026, 1, 10), {"WID": (D("10"), D("4"))}),
        goods_receipt("GRR", date(2026, 1, 12), "BILLR", {"WID": D("10")}),
    ]


def build_negative_recovery(path: Path) -> Path:
    return write_manager_file(path, negative_recovery_objects())


def single_named_location_objects() -> list[Obj]:
    """Stock held at one named location and nowhere else: a goods receipt and a flagged
    bill both put widgets into Store 2, which is not Manager's default location."""
    return [
        *masters(),
        obj("CustomInventoryLocation", "LOC2", {1: "Store 2", 3: "LOC2"}),
        _bill("BILLN", date(2026, 1, 5), {"WID": (D("5"), D("4"))}),
        goods_receipt("GRL", date(2026, 1, 6), "BILLN", {"WID": D("5")}, k("LOC2")),
        _bill("BILLS", date(2026, 1, 7), {"WID": (D("3"), D("4"))}, {64: True, 13: k("LOC2")}),
    ]


def build_single_named_location(path: Path) -> Path:
    return write_manager_file(path, single_named_location_objects())


def inventory_safety_objects(locations: int = 0, transfers: int = 0, negative: bool = False) -> list[Obj]:
    """A flagged bill bringing in 5 widgets, plus what Celerp cannot carry yet: `locations`
    extra inventory locations (the bill stocks the first), `transfers` stock transfers, and
    a delivery of 9 widgets against the 5 held, which takes stock below zero."""
    objects = [
        *masters(),
        _bill("BILLS", date(2026, 1, 5), {"WID": (D("5"), D("4"))}, {64: True, **({13: k("LOC2")} if locations else {})}),
        *(obj("CustomInventoryLocation", f"LOC{n}", {1: f"Store {n}", 3: f"LOC{n}"})
          for n in range(2, locations + 2)),
        *(obj("InventoryTransfer", f"TRF{n}", {1: f"T-{n}", 2: date(2026, 1, 10), 6: [{1: k("WID"), 3: D("2")}]})
          for n in range(1, transfers + 1)),
    ]
    if negative:
        objects += [_sale("INVNEG", date(2026, 1, 12), {"WID": D("9")}),
                    delivery_note("DNNEG", date(2026, 1, 15), "INVNEG", {"WID": D("9")})]
    return objects


def build_inventory_safety(path: Path, **kinds) -> Path:
    return write_manager_file(path, inventory_safety_objects(**kinds))


DEPENDENCY_CUTOVER = date(2026, 1, 15)


def cutover_dependency_objects() -> list[Obj]:
    """A bill and an invoice each settled in full before the 01-15 cutover, whose goods move
    only after it. BILLS brings in its own 5 widgets @ 4.00 on 01-03. BILLC bills 10 @ 4.00
    on 01-05, PAYC pays it on 01-06 and GRC receives the 10 on 01-20. INVC sells 2 on 01-07,
    at the 4.00 average of the 15 owned: cost of sales 8.00. RECC pays it on 01-08 and DNC
    delivers the 2 on 01-21. Opening stock 5 / 20.00; on hand at the end 13 / 52.00."""
    return [
        *_stocked_masters(),
        _bill("BILLS", date(2026, 1, 3), {"WID": (D("5"), D("4"))}, {64: True}),
        _bill("BILLC", date(2026, 1, 5), {"WID": (D("10"), D("4"))}),
        obj("Payment", "PAYC", {1: date(2026, 1, 6), 2: "P-C", 3: PAID_BY_SUPPLIER, 5: k("SA"), 7: k("OPB"), 11: [
            {2: AP, 7: k("SA"), 8: k("BILLC"), 18: D("40")},
        ]}),
        _sale("INVC", date(2026, 1, 7), {"WID": D("2")}),
        obj("Receipt", "RECC", {1: date(2026, 1, 8), 2: "R-C", 3: PAID_BY_CUSTOMER, 4: k("CA"), 7: k("OPB"), 11: [
            {2: AR, 3: k("CA"), 4: k("INVC"), 18: D("25")},
        ]}),
        goods_receipt("GRC", date(2026, 1, 20), "BILLC", {"WID": D("10")}),
        delivery_note("DNC", date(2026, 1, 21), "INVC", {"WID": D("2")}),
    ]


def batched_delivery_objects() -> list[Obj]:
    """One invoice line shipped in two batches, with dearer stock arriving between them.
    BILLS brings in its own 6 widgets @ 4.00 and BILLT 1 @ 5.00; BILLU bills 2 @ 10.00 on
    01-02, received by GRU on 01-07. INVB sells 3 on 01-05, when 9 are owned for 49.00:
    cost of sales 49.00 x 3 / 9 = 16.33. DNB1 delivers 1 on 01-06 (16.33 / 3 = 5.44) and
    DNB2 the other 2 on 01-09 (16.33 - 5.44 = 10.89). On hand at the end 6 / 32.67."""
    return [
        *_stocked_masters(),
        _bill("BILLU", date(2026, 1, 2), {"WID": (D("2"), D("10"))}),
        _bill("BILLS", date(2026, 1, 3), {"WID": (D("6"), D("4"))}, {64: True}),
        _bill("BILLT", date(2026, 1, 4), {"WID": (D("1"), D("5"))}, {64: True}),
        _sale("INVB", date(2026, 1, 5), {"WID": D("3")}),
        delivery_note("DNB1", date(2026, 1, 6), "INVB", {"WID": D("1")}),
        goods_receipt("GRU", date(2026, 1, 7), "BILLU", {"WID": D("2")}),
        delivery_note("DNB2", date(2026, 1, 9), "INVB", {"WID": D("2")}),
    ]


OWNERSHIP_CUTOVER = date(2026, 1, 10)


def ownership_interleaving_objects() -> list[Obj]:
    """Goods sold and delivered after a purchase is billed but before it arrives. BILL1 bills
    10 @ 4.00 on 01-02, received by GR1 on 01-03. BILL2 bills 10 @ 6.00 on 01-05; its goods
    arrive with GR2 on 01-12. INVO sells 5 on 01-07 and DNO delivers them on 01-08. Manager
    owns 20 widgets worth 100.00 when INVO is invoiced, so its cost of sales is 5 @ 5.00 =
    25.00, though only the 10 @ 4.00 are physically held when they leave. On hand at the
    end 15 / 75.00, which is the inventory account's balance: 40.00 + 60.00 - 25.00."""
    return [
        *_stocked_masters(),
        _bill("BILL1", date(2026, 1, 2), {"WID": (D("10"), D("4"))}),
        goods_receipt("GR1", date(2026, 1, 3), "BILL1", {"WID": D("10")}),
        _bill("BILL2", date(2026, 1, 5), {"WID": (D("10"), D("6"))}),
        _sale("INVO", date(2026, 1, 7), {"WID": D("5")}),
        delivery_note("DNO", date(2026, 1, 8), "INVO", {"WID": D("5")}),
        goods_receipt("GR2", date(2026, 1, 12), "BILL2", {"WID": D("10")}),
    ]


def unowned_sale_objects() -> list[Obj]:
    """A widget invoiced before any is bought, with no unit cost set: nothing says what
    cost of sales Manager booked for it."""
    return [
        *_stocked_masters(),
        _sale("INVZ", date(2026, 1, 3), {"WID": D("1")}),
        _bill("BILLZ", date(2026, 1, 5), {"WID": (D("4"), D("4"))}, {64: True}),
    ]
