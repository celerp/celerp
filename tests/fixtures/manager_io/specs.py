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
    "SalesOrder": "2dac5598-128d-4954-b2e4-21515047b3a7",
    "PurchaseQuote": "38d606f7-6358-4ace-9d1d-f6c0b9ea9d68",
    "PurchaseOrder": "a26bbea1-57aa-4fd9-b7c9-e26b83114385",
    "TrialBalance": "e5dc98ef-4662-4a68-8a9d-b3e2d12b55d6",
    "DeliveryNote": "a0f6a539-f6a4-4a38-a69a-546a608a1f6d",
    "GoodsReceipt": "866217a4-f841-47de-a4e6-87152405c88d",
    "InventoryTransfer": "7eaafddc-54c9-4235-98d2-e8a1ee438150",
    "InventoryWriteOff": "d7ff6694-f1ef-419f-8ae2-55527a02e95f",
    "ProductionOrder": "da996523-e77e-493c-b134-31c6c6ccae4a",
    "InventoryAutomaticRevaluation": "ffbec913-dcc6-420b-b591-a27822f60648",
    "ProfitAndLossStatementAccountInventoryWriteOffs": "eff068c3-5cc6-4746-b73b-850d30534cbb",
    "CustomInventoryLocation": "fae8151d-252e-45e3-b1f4-e048075b8983",
    "DefaultInventoryLocation": "d63413bc-622e-4e39-86bc-15e95eb4e81c",
    "InventoryUnitCost": "d5d7bad5-0abd-4501-af7f-cb6289cabc30",
    "ProfitAndLossStatementAccountInventorySales": "ea44f579-9548-4954-baf0-48538aceff1e",
    "ProfitAndLossStatementAccountInventoryPurchases": "aa80b662-3642-4c08-b328-2fccf132ceb1",
    "CapitalAccount": "b9c4cd62-7569-44f0-bc62-9df3007a6a5c",
    "SubAccount": "f361339b-932a-4436-b56e-a337c1587c72",
    "SpecialAccount": "e495f4e8-5fad-48ac-8a66-f35049ac4ef3",
    "Employee": "dadb7f95-a5dd-45c0-945d-6ad4ee28776e",
    "ExpenseClaimsPayer": "563d7f9e-d64c-49ec-a938-e5531e72f4d8",
    "FixedAsset": "00082353-9fca-4ab4-91ae-20505479cbda",
    "IntangibleAsset": "94d5307e-4332-4545-ab1e-1528c9032b7d",
    "Investment": "a8f95068-fc73-43f7-aabb-fd868e506b51",
    "InvestmentMarketPrice": "32b97957-5cc5-48f0-b104-89ae340695d4",
    "ControlAccountForCapitalAccounts": "95b9d17d-f5f8-4722-a89d-456fa0906e13",
    "ControlAccountForBankAccounts": "c97264e3-eed6-4afd-8d2c-e1c1a00b4dc1",
    "ControlAccountForCustomers": "7a57978e-583d-4997-86c7-c557de0e7fdd",
    "ControlAccountForSuppliers": "78e7aefb-9dff-4ae5-981e-4165b14fd963",
    "ControlAccountForInventoryItems": "acc019f7-9bb9-4d8e-ba66-0956916b4247",
    "ControlAccountForEmployees": "fb804fba-df12-4628-ae54-df7a67d67f1b",
    "ControlAccountForFixedAssets": "014742b2-fc31-4ded-90b5-2e0d437a1589",
    "ControlAccountForFixedAssetsAccumulatedDepreciation": "c2e88cf8-4d05-4bec-8c42-fdef424faf1a",
    "ControlAccountForIntangibleAssets": "8ed99282-8ec3-4505-8846-0507bd0d0f70",
    "ControlAccountForIntangibleAssetsAccumulatedAmortization": "47995ee3-b2c9-4799-ace3-768f7514c644",
    "ControlAccountForInvestments": "fb2c120e-6b30-47cb-a61e-14f0189bec06",
    "ControlAccountForSpecialAccounts": "6af7e809-1de3-4a09-923f-27ad786ed837",
    "BalanceSheetAccountStartingBalance": "db53bb26-6d7d-4170-8373-7d09f9638960",
    "BankOrCashAccountStartingBalance": "0c1000da-6cc3-4448-8245-6f1eeccab8d6",
    "CapitalAccountStartingBalance": "c29b5c78-58d1-4678-9f38-ea0b8b21e4cf",
    "SpecialAccountStartingBalance": "371c8de0-293e-4b4d-8e16-e1e2548ff8e2",
    "EmployeeStartingBalance": "47074f31-7800-4cb9-8b69-ef9b01173e3e",
    "FixedAssetStartingBalance": "f0bd4b36-7bd4-49c1-8d68-720704fa4309",
    "IntangibleAssetStartingBalance": "7995ce1d-d8b5-4034-8b87-fb0291a44102",
    "InventoryItemStartingBalance": "bfb275ad-a639-4a6a-a112-b87e602df7e1",
    "InvestmentStartingBalance": "6494cd10-ec22-4b6a-a34f-12c690a00098",
    "SalesInvoiceStartingBalance": "f71f59af-dbab-492f-83a8-6c0157360be8",
    "PurchaseInvoiceStartingBalance": "e9333683-ea93-4949-99f3-28e60278b68d",
    "StartingExchangeRates": "d6bc0979-e474-4188-b881-d76b1a18c964",
    # Built-in accounts Manager adds once a holder of their kind exists.
    "BalanceSheetCapitalAccountsAccount": "054dfae1-c34a-475e-abde-49e0385ffc9a",
    "BalanceSheetSpecialAccountsAccount": "ef49facb-203b-4b45-aebd-99af4645700b",
    "BalanceSheetEmployeeClearingAccount": "650a36fe-801f-4031-8d5b-ab422d061fca",
    "BalanceSheetExpenseClaimsAccount": "f728124f-c6b6-4dad-82c5-22fc0d8d0571",
    "BalanceSheetFixedAssetsAtCostAccount": "4a0e8917-fee2-4033-9161-48dd513fdb73",
    "BalanceSheetFixedAssetsAccumulatedDepreciationAccount": "f813a6c8-1ead-46bd-8911-f12714be193c",
    "BalanceSheetIntangibleAssetsAtCostAccount": "31d369e3-32c7-4bd2-bb83-9c1c58010c1a",
    "BalanceSheetIntangibleAssetsAccumulatedAmortizationAccount": "aa12d048-bfbd-47dc-a5b8-03e35c417996",
    "BalanceSheetInvestmentsAccount": "352897d1-e7fe-462e-9965-458ed9e27b82",
    # Records that post through the accounts above, and the built-in accounts they post to.
    "DepreciationEntry": "75cdc055-6dec-4381-bc40-b670366e6abc",
    "AmortizationEntry": "d33519a3-e8e0-4556-9833-b744a58dd2f7",
    "Payslip": "1d103fa7-6fc1-4951-811e-972968b842cc",
    "PayslipEarningsItem": "ab02f6ab-c91c-4fc2-b979-66a6682c200e",
    "PayslipDeductionItem": "0444eb18-6fc5-4d1f-be8b-c114da01832c",
    "PayslipContributionItem": "73af4c68-c347-4088-8846-758f1e7bc5bb",
    "ExpenseClaim": "02572e0c-0167-4dbd-a392-08d8f67f3fe4",
    "LatePaymentFee": "4dada073-022f-464e-bdb3-ff38c83e577f",
    "WithholdingTaxReceipt": "8f7510d9-a92d-4b4c-9421-fd745e198b3c",
    "BillableTime": "6bfb652c-11cb-46fa-9e5a-c5950ccbae15",
    "BalanceSheetBillableTimeAccount": "9f3c0a1a-dd0b-4b5c-ba50-3e4939a0e90c",
    "BalanceSheetWithholdingTaxAccount": "8f75a810-abd0-4d89-a6a2-66c9003a60e2",
    "ProfitAndLossStatementAccountBillableTimeMovement": "03d41bd1-8dd4-4ce3-9b82-5ce17a40171a",
    "ProfitAndLossStatementAccountFixedAssetDepreciation": "fb6fdbfd-b39f-4674-8928-10c2bdd87e58",
    "ProfitAndLossStatementAccountFixedAssetLossOnDisposal": "428ea9ba-4679-4568-b05b-7fcf62504893",
    "ProfitAndLossStatementAccountIntangibleAssetsAmortization": "83d56444-fed8-4de8-8e58-e325a370ae44",
    "ProfitAndLossStatementAccountIntangibleAssetsGainsLossOnDisposal": "43e14984-202e-4e9e-b843-d417dddcd3d2",
    "ProfitAndLossStatementCapitalGainsOnInvestments": "de8d014f-e862-4ab5-88cc-335d464315a6",
    "ProfitAndLossStatementAccountLatePaymentFees": "841b2acb-8bb5-4742-864e-4226fa421f44",
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
    USD per EUR unless inverse. Every foreign record type that settles or posts: invoices and
    bills at their own rates, a receipt and a payment at a different rate, a credit and a debit
    note applied at a rate other than their invoice's, a transfer from the euro bank to the
    base bank, and a euro journal with no rate of its own. Figures are worked out in
    checkpoints.json under "fx"."""
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
        obj("SalesInvoice", "INVE2", {1: date(2026, 2, 20), 2: "INV-E2", 3: cust, 64: D("1.25"), 49: [
            {2: k("SALES"), 17: "Part 1", 18: D("1"), 19: D("10.01")},
            {2: k("SALES"), 17: "Part 2", 18: D("1"), 19: D("10.01")},
            {2: k("SALES"), 17: "Part 3", 18: D("1"), 19: D("10.01")},
        ]}),
        obj("CreditNote", "CNE1", {1: date(2026, 2, 25), 2: "CN-E1", 3: cust, 8: k("INVE2"), 30: D("1.28"), 22: [
            {2: k("SALES"), 17: "Returned part", 18: D("1"), 19: D("10")},
        ]}),
        obj("Payment", "PE1", {1: date(2026, 3, 1), 2: "P-E1", 3: PAID_BY_SUPPLIER, 5: supp, 7: ebank, 46: D("1.27"), 11: [
            {2: AP, 7: supp, 8: k("BILLE1"), 18: D("80")},
        ]}),
        obj("PurchaseInvoice", "BILLE2", {1: date(2026, 3, 2), 2: "BILL-E2", 3: supp, 61: D("1.26"), 23: [
            {2: k("PURCH"), 18: D("1"), 19: D("40")},
        ]}),
        obj("DebitNote", "DNE1", {1: date(2026, 3, 5), 2: "DN-E1", 3: supp, 6: k("BILLE2"), 22: D("1.3"), 16: [
            {2: k("PURCH"), 17: "Short delivery", 18: D("1"), 19: D("10")},
        ]}),
        obj("InterAccountTransfer", "IATE1", {1: date(2026, 3, 10), 6: "T-E1", 2: ebank, 8: D("20"), 3: k("MBANK"),
                                              9: D("26")}),
        obj("JournalEntry", "JEE1", {1: date(2026, 3, 15), 2: "JE-E1", 3: "Euro bank adjustment", 8: eur, 14: [
            {1: CASH_AT_BANK, 29: ebank, 13: D("10")},
            {1: k("SALES"), 14: D("10")},
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


# ── Quotes and orders ─────────────────────────────────────────────────────────
# Their lines share one layout: 1 item, 2 description, 3 quantity, 4 unit price,
# 5 discount percentage, 6 discount amount, 7 tax code.

ORDERS_CUTOVER = date(2026, 1, 31)


def _order_line(description: str, qty: str, price: str, extra: dict | None = None) -> dict:
    return {2: description, 3: D(qty), 4: D(price), **(extra or {})}


def quote_order_objects() -> list[Obj]:
    """Sales quotes and orders, purchase quotes and orders, and the invoices and bills they
    became. Q-1 is open and valid for 30 days, Q-X cancelled. Q-2 became SO-1, which became
    INV-O; Q-3 became INV-Q directly. PQ-1 became PO-1, which became BILL-O. PO-2 (a 10%
    line discount) and SO-2 (tax included) are open after ORDERS_CUTOVER."""
    vat, ca, sa, wid = k("VAT"), k("CA"), k("SA"), k("WID")
    return [
        *masters(),
        obj("SalesQuote", "Q1", {1: date(2026, 1, 5), 2: "Q-1", 3: ca, 9: "Consulting proposal", 26: 30, 24: [
            _order_line("Consulting hours", "2", "50", {7: vat})]}),
        obj("SalesQuote", "QX", {1: date(2026, 1, 6), 2: "Q-X", 3: ca, 25: True, 24: [
            _order_line("Withdrawn offer", "1", "80")]}),
        obj("SalesQuote", "Q2", {1: date(2026, 1, 7), 2: "Q-2", 3: ca, 24: [
            _order_line("Widgets", "2", "12.50", {1: wid})]}),
        obj("SalesOrder", "SO1", {1: date(2026, 1, 8), 2: "SO-1", 3: ca, 19: k("Q2"), 18: [
            _order_line("Widgets", "2", "12.50", {1: wid})]}),
        obj("SalesInvoice", "INVO", {1: date(2026, 1, 12), 2: "INV-O", 3: ca, 50: k("SO1"), 51: k("Q2"), 49: [
            {2: k("S1"), 17: "Widgets", 18: D("2"), 19: D("12.50")}]}),
        obj("SalesQuote", "Q3", {1: date(2026, 1, 9), 2: "Q-3", 3: ca, 24: [
            _order_line("Support", "1", "40")]}),
        obj("SalesInvoice", "INVQ", {1: date(2026, 1, 14), 2: "INV-Q", 3: ca, 51: k("Q3"), 49: [
            {2: k("S1"), 17: "Support", 18: D("1"), 19: D("40")}]}),
        obj("PurchaseQuote", "PQ1", {1: date(2026, 1, 9), 2: "PQ-1", 3: sa, 17: [
            _order_line("Widgets", "10", "4", {1: wid})]}),
        obj("PurchaseOrder", "PO1", {1: date(2026, 1, 10), 2: "PO-1", 3: sa, 18: k("PQ1"), 17: [
            _order_line("Widgets", "10", "4", {1: wid})]}),
        obj("PurchaseInvoice", "BILLO", {1: date(2026, 1, 15), 2: "BILL-O", 3: sa, 29: k("PO1"), 28: k("PQ1"), 23: [
            {2: k("OFF"), 17: "Widgets", 18: D("10"), 19: D("4")}]}),
        obj("PurchaseOrder", "PO2", {1: date(2026, 2, 1), 2: "PO-2", 3: sa, 12: True, 13: 0, 17: [
            _order_line("Widgets", "10", "4", {1: wid, 5: D("10")})]}),
        obj("SalesOrder", "SO2", {1: date(2026, 2, 3), 2: "SO-2", 3: ca, 9: True, 18: [
            _order_line("Support, tax included", "1", "55", {7: vat})]}),
    ]


def build_quotes_orders(path: Path) -> Path:
    return write_manager_file(path, quote_order_objects())


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
    Deliveries at the cost of sales each invoice books, the 5.00 unit cost from 01-14:
      INVE 2 (01-22): DN2 01-16, delivered before the invoice, 10.00, leaves 18 / 85.00
      INVP 5 (01-18): DN3 01-19 delivers 3 only, 15.00, leaves 15 / 70.00
      INVD 4 (01-15): DN1 01-20, delivered after the invoice, 20.00, leaves 11 / 50.00
      INVN 1 (01-21): no physical record, moves nothing
      INVX 1 (01-23), flagged: moves its own stock, 5.00, leaves 10 / 45.00
    Books: bills 122.00 to payables, invoices 13 @ 12.50 = 162.50 to receivables, cost of
    sales 13 x 5.00 (the unit cost from 01-14) = 65.00, so inventory on hand is 57.00. R1
    pays INVD in full on 01-25. At the 01-17 cutover the opening stock is 18 / 85.00.
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
    cutover in both directions. Hand-worked figures, receipts before deliveries on a day.

    Cost of sales, the item's unit cost on the invoice date (WID 3.20 from 02-01 and 3.50
    from 02-08, GAD 7.00 from 02-01), as Manager books it whatever the bills paid:
      02-05 SO1 WID 7 @ 3.20 = 22.40; GAD 2 @ 7.00 = 14.00
      02-08 SO2 WID 3 @ 3.50 = 10.50
      02-09 SO3 WID 1 @ 3.50 = 3.50
    Manager owns enough for each: WID 16 by 02-05 (BO1 10, BO2 6), 9 + BO4 2 = 11 by 02-08.

    Stock, each delivery leaving at its share of the cost its invoice line booked:

      02-03 GRO1 receives BO1's WID 10 of 10 @ 3.00 = 30.00 and GAD 3 of 4 @ 7.00 = 21.00
      02-04 GRA receives BO4's WID 2 @ 4.00 = 8.00, before BO4 is billed on 02-08
      02-05 BO2, flagged, brings in its own WID 6 @ 3.50 = 21.00: WID 18 / 59.00
      02-05 DNA delivers SO3's WID 1, before SO3 is invoiced on 02-09: 3.50, WID 17 / 55.50
      Opening at the 02-06 cutover: WID 17 / 55.50, GAD 3 / 21.00
      02-07 GRO2 receives BO1's last GAD 1, BO1 billed before the cutover: 28.00 - 21.00 = 7.00,
            GAD 4 / 28.00
      02-07 DNO1 delivers SO1's WID 5 of 7 and GAD 2 of 2, SO1 invoiced before the cutover:
            WID 22.40 x 5 / 7 = 16.00, 12 / 39.50; GAD 14.00, 2 / 14.00
      02-08 SO2, flagged, takes out its own WID 3: 10.50, WID 9 / 29.00
      02-09 BO3 bills GAD 5 @ 8.00 with no goods receipt: moves nothing
    On hand: WID 9 / 29.00, GAD 2 / 14.00, all at the default location."""
    return [
        *_stocked_masters(),
        obj("InventoryItem", "GAD", {1: "GAD-1", 11: "Gadget", 13: "each", 32: True, 3: SALE_PRICES["GAD"], 31: True}),
        obj("InventoryUnitCost", "UCW1", {1: date(2026, 2, 1), 2: k("WID"), 3: D("3.20")}),
        obj("InventoryUnitCost", "UCW2", {1: date(2026, 2, 8), 2: k("WID"), 3: D("3.50")}),
        obj("InventoryUnitCost", "UCG", {1: date(2026, 2, 1), 2: k("GAD"), 3: D("7")}),
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
    is held; 10 more, billed on 01-04, are received on 01-12. The bills own every widget
    sold, so only the physical stock runs short: the history ends holding stock, but at the
    01-09 cutover Manager's opening position is 5 - 9 - 6 = -10."""
    return [
        *_stocked_masters(),
        _bill("BILLS", date(2026, 1, 5), {"WID": (D("5"), D("4"))}, {64: True}),
        _sale("INVNEG", date(2026, 1, 6), {"WID": D("9")}),
        delivery_note("DNNEG", date(2026, 1, 8), "INVNEG", {"WID": D("9")}),
        _sale("INVFN", date(2026, 1, 9), {"WID": D("6")}, {69: True}),
        _bill("BILLR", date(2026, 1, 4), {"WID": (D("10"), D("4"))}),
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
    a delivery of 9 widgets against the 5 held, which takes stock below zero. BILLN bills the
    other 4 without receiving them, so the 9 sold are owned and only the physical stock is
    short."""
    objects = [
        *masters(),
        _bill("BILLS", date(2026, 1, 5), {"WID": (D("5"), D("4"))}, {64: True, **({13: k("LOC2")} if locations else {})}),
        *(obj("CustomInventoryLocation", f"LOC{n}", {1: f"Store {n}", 3: f"LOC{n}"})
          for n in range(2, locations + 2)),
        *(obj("InventoryTransfer", f"TRF{n}", {1: f"T-{n}", 2: date(2026, 1, 10), 4: k("LOC2"),
                                                6: [{9: k("WID"), 6: D("2")}]})
          for n in range(1, transfers + 1)),
    ]
    if negative:
        objects += [_bill("BILLN", date(2026, 1, 11), {"WID": (D("4"), D("4"))}),
                    _sale("INVNEG", date(2026, 1, 12), {"WID": D("9")}),
                    delivery_note("DNNEG", date(2026, 1, 15), "INVNEG", {"WID": D("9")})]
    return objects


def build_inventory_safety(path: Path, **kinds) -> Path:
    return write_manager_file(path, inventory_safety_objects(**kinds))


def write_off(label: str, day: date, lines: dict[str, D], header: dict | None = None) -> Obj:
    return obj("InventoryWriteOff", label, {1: day, 12: label, **(header or {}),
                                            3: [{1: k(item), 3: qty} for item, qty in lines.items()]})


def production(label: str, day: date, finished: tuple[str, D], consumed: dict[str, D],
               expenses: dict[str, D] | None = None, header: dict | None = None) -> Obj:
    """A production order making `finished` (item, quantity) from the `consumed` items, adding
    each of `expenses` ({account: amount}) to the cost of what it makes."""
    fields = {1: day, 17: label, 4: k(finished[0]), 18: finished[1],
              3: [{1: k(item), 3: qty} for item, qty in consumed.items()]}
    if expenses:
        fields |= {9: True, 10: [{1: k(account), 2: amount} for account, amount in expenses.items()]}
    return obj("ProductionOrder", label, fields | (header or {}))


def stock_records_objects() -> list[Obj]:
    """Every inventory record a multi-location Manager business keeps. Hand-worked figures,
    with widgets at a unit cost of 4.00 from 01-01:

    BILLS brings in its own 10 widgets @ 4.00 on 01-03, into Store 2: 10 / 40.00.
    WO1 writes off 2 widgets on 01-05 at Store 2, at 4.00: 8.00 to Inventory - write-offs,
    8 / 32.00 left. TRF1 moves 3 widgets to the default location on 01-06: no figure moves.
    PO1 makes 1 gadget from 3 widgets on 01-07, at 4.00 each (12.00), adding 6.00 of office
    expenses to its cost: the gadget is worth 18.00, 6.00 more stock on the inventory account,
    credited to office expenses. At the end: 5 widgets / 20.00, 1 gadget / 18.00, inventory
    on hand 40.00 - 8.00 + 6.00 = 38.00. AR1 is an automatic revaluation with nothing in it."""
    return [
        *_stocked_masters(),
        obj("InventoryItem", "GAD", {1: "GAD-1", 11: "Gadget", 13: "each", 32: True, 3: SALE_PRICES["GAD"], 31: True}),
        obj("CustomInventoryLocation", "LOC2", {1: "Store 2", 3: "LOC2"}),
        obj("ProfitAndLossStatementAccountInventoryWriteOffs", None, {1: "Inventory - write-offs"}),
        obj("InventoryUnitCost", "UCW", {1: date(2026, 1, 1), 2: k("WID"), 3: D("4")}),
        _bill("BILLS", date(2026, 1, 3), {"WID": (D("10"), D("4"))}, {64: True, 13: k("LOC2")}),
        write_off("WO1", date(2026, 1, 5), {"WID": D("2")}, {10: k("LOC2"), 2: "Damaged in storage"}),
        obj("InventoryTransfer", "TRF1", {1: "T-1", 2: date(2026, 1, 6), 3: k("LOC2"),
                                          6: [{9: k("WID"), 6: D("3")}]}),
        production("PO1", date(2026, 1, 7), ("GAD", D("1")), {"WID": D("3")}, {"OFF": D("6")}),
        obj("InventoryAutomaticRevaluation", "AR1", {}),
    ]


def build_stock_records(path: Path) -> Path:
    return write_manager_file(path, stock_records_objects())


def unpriced_write_off_objects() -> list[Obj]:
    """A write-off with no unit cost set. BILLS brings in its own 5 widgets @ 4.00 on 01-03;
    WO0 writes off 1 on 01-05, which Manager books at nothing: 4 widgets worth 20.00 are
    left, and the inventory account stays at 20.00."""
    return [
        *_stocked_masters(),
        _bill("BILLS", date(2026, 1, 3), {"WID": (D("5"), D("4"))}, {64: True}),
        write_off("WO0", date(2026, 1, 5), {"WID": D("1")}),
    ]


def over_owned_write_off_objects() -> list[Obj]:
    """WOQ writes off 3 widgets on 01-05 when the business owns the 2 BILLS brought in."""
    return [
        *_stocked_masters(),
        obj("InventoryUnitCost", "UCW", {1: date(2026, 1, 1), 2: k("WID"), 3: D("4")}),
        _bill("BILLS", date(2026, 1, 3), {"WID": (D("2"), D("4"))}, {64: True}),
        write_off("WOQ", date(2026, 1, 5), {"WID": D("3")}),
    ]


def unflagged_production_expense_objects() -> list[Obj]:
    """PON lists an expense item without the flag that adds it to the cost of what it makes."""
    return [
        *_stocked_masters(),
        obj("InventoryItem", "GAD", {1: "GAD-1", 11: "Gadget", 13: "each", 32: True, 3: SALE_PRICES["GAD"], 31: True}),
        obj("InventoryUnitCost", "UCW", {1: date(2026, 1, 1), 2: k("WID"), 3: D("4")}),
        _bill("BILLS", date(2026, 1, 3), {"WID": (D("5"), D("4"))}, {64: True}),
        production("PON", date(2026, 1, 7), ("GAD", D("1")), {"WID": D("3")}, header={
            10: [{1: k("OFF"), 2: D("6")}]}),
    ]


DEPENDENCY_CUTOVER = date(2026, 1, 15)


def cutover_dependency_objects() -> list[Obj]:
    """A bill and an invoice each settled in full before the 01-15 cutover, whose goods move
    only after it. BILLS brings in its own 5 widgets @ 4.00 on 01-03. BILLC bills 10 @ 4.00
    on 01-05, PAYC pays it on 01-06 and GRC receives the 10 on 01-20. INVC sells 2 on 01-07,
    at the 4.00 unit cost set from 01-01: cost of sales 8.00. RECC pays it on 01-08 and DNC
    delivers the 2 on 01-21. Opening stock 5 / 20.00; on hand at the end 13 / 52.00."""
    return [
        *_stocked_masters(),
        obj("InventoryUnitCost", "UCC", {1: date(2026, 1, 1), 2: k("WID"), 3: D("4")}),
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
    01-02, received by GRU on 01-07. INVB sells 3 on 01-05, when 9 are owned for 49.00, at
    the 5.4433 unit cost set from 01-01: cost of sales 3 x 5.4433 = 16.33. DNB1 delivers 1 on
    01-06 (16.33 / 3 = 5.44) and DNB2 the other 2 on 01-09 (16.33 - 5.44 = 10.89). On hand at
    the end 6 / 32.67."""
    return [
        *_stocked_masters(),
        obj("InventoryUnitCost", "UCB", {1: date(2026, 1, 1), 2: k("WID"), 3: D("5.4433")}),
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
    arrive with GR2 on 01-12. INVO sells 5 on 01-07 and DNO delivers them on 01-08. No unit
    cost is set, so Manager books INVO at no cost, though it owns 20 widgets worth 100.00
    and 10 @ 4.00 are physically held when they leave. On hand at the end 15 / 100.00,
    which is the inventory account's balance: 40.00 + 60.00 - 0.00."""
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


def over_owned_sale_objects() -> list[Obj]:
    """Five widgets are on hand by 01-03, but the bill for three of them is dated 01-10, and
    a unit cost is set. INVQ sells three on 01-05: more than Manager's books own then (two),
    though fewer than are on hand. Manager clears the shortfall through negative inventory,
    which Celerp does not rebuild."""
    return [
        *_stocked_masters(),
        obj("InventoryUnitCost", "UCQ", {1: date(2026, 1, 1), 2: k("WID"), 3: D("4")}),
        _bill("BILLQ", date(2026, 1, 2), {"WID": (D("2"), D("4"))}),
        _bill("BILLL", date(2026, 1, 10), {"WID": (D("3"), D("4"))}),
        goods_receipt("GRQ", date(2026, 1, 2), "BILLQ", {"WID": D("2")}),
        goods_receipt("GRL", date(2026, 1, 3), "BILLL", {"WID": D("3")}),
        _sale("INVQ", date(2026, 1, 5), {"WID": D("3")}),
    ]


# ── Starting balances and the accounts that hold them ─────────────────────────
# Manager keeps a balance per capital account, special account, employee, expense claims
# payer, fixed and intangible asset and investment, each under a control account, and
# lets a business start from balances instead of history.

MIN_DATE = {1: 1, 2: 15}            # .NET DateTime.MinValue, which Manager writes into some unused fields
CAPITAL = T["BalanceSheetCapitalAccountsAccount"]
SPECIAL = T["BalanceSheetSpecialAccountsAccount"]
EMPLOYEES = T["BalanceSheetEmployeeClearingAccount"]
EXPENSE_CLAIMS = T["BalanceSheetExpenseClaimsAccount"]
FIXED_AT_COST = T["BalanceSheetFixedAssetsAtCostAccount"]
DEPRECIATION = T["BalanceSheetFixedAssetsAccumulatedDepreciationAccount"]
INTANGIBLE_AT_COST = T["BalanceSheetIntangibleAssetsAtCostAccount"]
AMORTIZATION = T["BalanceSheetIntangibleAssetsAccumulatedAmortizationAccount"]
INVESTMENTS = T["BalanceSheetInvestmentsAccount"]
STARTING_CUTOVER = date(2026, 1, 15)


def holder_objects() -> list[Obj]:
    """One holder of each kind with the built-in account Manager adds once that kind is used,
    a capital sub-account, and a custom control account for capital accounts and for bank
    accounts."""
    return [
        obj("BalanceSheetCapitalAccountsAccount", None, {1: "Capital accounts"}),
        obj("BalanceSheetSpecialAccountsAccount", None, {1: "Special accounts"}),
        obj("BalanceSheetEmployeeClearingAccount", None, {1: "Employee clearing account"}),
        obj("BalanceSheetExpenseClaimsAccount", None, {1: "Expense claims"}),
        obj("BalanceSheetFixedAssetsAtCostAccount", None, {1: "Fixed assets, at cost"}),
        obj("BalanceSheetFixedAssetsAccumulatedDepreciationAccount", None,
            {1: "Fixed assets, accumulated depreciation"}),
        obj("BalanceSheetIntangibleAssetsAtCostAccount", None, {1: "Intangible assets, at cost"}),
        obj("BalanceSheetIntangibleAssetsAccumulatedAmortizationAccount", None,
            {1: "Intangible assets, accumulated amortization"}),
        obj("BalanceSheetInvestmentsAccount", None, {1: "Investments"}),
        obj("ControlAccountForBankAccounts", "BCTRL", {1: "Bank accounts", 17: "1000", 3: T["Assets"], 16: 1}),
        obj("ControlAccountForCapitalAccounts", "PCTRL", {1: "Partner capital", 17: "3100", 3: k("OWNER_FUNDS"),
                                                          16: 1}),
        obj("BalanceSheetAccount", "EQUIP", {1: "Equipment", 17: "1500", 3: T["Assets"]}),
        obj("BalanceSheetAccount", "LOAN", {1: "Bank loan", 17: "2500", 3: T["Liabilities"]}),
        obj("CapitalAccount", "CAP", {1: "Owner capital", 11: "3001", 4: MIN_DATE}),
        obj("CapitalAccount", "CAP2", {1: "Second partner", 11: "3101", 10: k("PCTRL"), 4: MIN_DATE}),
        obj("SubAccount", "DRAW", {1: "Drawings"}),
        obj("SpecialAccount", "SPEC", {1: "Director loan", 11: "2601"}),
        obj("Employee", "EMP", {1: "Staff One", 14: "E1"}),
        obj("ExpenseClaimsPayer", "ECP", {1: "Owner"}),
        obj("FixedAsset", "VAN", {1: "Van", 10: "FA1", 17: D("20"), 5: MIN_DATE}),
        obj("IntangibleAsset", "PAT", {1: "Patent", 10: "IA1", 15: D("10"), 22: MIN_DATE}),
        obj("Investment", "FUND", {1: "INV1", 2: "Index fund"}),
    ]


def control_objects() -> list[Obj]:
    """A custom control account of every other kind, none of them used: each arrives as an
    empty account, except the customer, supplier and inventory controls, which fold into
    receivables, payables and inventory."""
    assets, liabilities = T["Assets"], T["Liabilities"]
    return [
        obj("ControlAccountForEmployees", "EMPCTRL", {1: "Staff clearing", 17: "2700", 3: liabilities}),
        obj("ControlAccountForSpecialAccounts", "SPECCTRL", {1: "Related parties", 17: "2600", 3: liabilities}),
        obj("ControlAccountForFixedAssets", "FACTRL", {1: "Vehicles at cost", 17: "1600", 3: assets}),
        obj("ControlAccountForFixedAssetsAccumulatedDepreciation", "FADCTRL",
            {1: "Vehicles depreciation", 17: "1610", 3: assets}),
        obj("ControlAccountForIntangibleAssets", "IACTRL", {1: "Licences at cost", 17: "1700", 3: assets}),
        obj("ControlAccountForIntangibleAssetsAccumulatedAmortization", "IAACTRL",
            {1: "Licences amortization", 17: "1710", 3: assets}),
        obj("ControlAccountForInvestments", "INVCTRL", {1: "Listed funds", 17: "1800", 3: assets}),
        obj("ControlAccountForCustomers", "CUSCTRL", {1: "Export debtors", 17: "1160", 3: assets}),
        obj("ControlAccountForSuppliers", "SUPCTRL", {1: "Import creditors", 17: "2160", 3: liabilities}),
        obj("ControlAccountForInventoryItems", "STKCTRL", {1: "Stock in transit", 17: "1360", 3: assets}),
    ]


def starting_balance_objects() -> list[Obj]:
    """A business that starts from balances, then trades with its holders. Hand-worked
    figures, debit positive:

    Starting balances: equipment 1000, bank loan -400, operating bank 500, owner capital
    -900, director loan -150, Staff One -60, van 800 at cost and -200 depreciated, patent
    300 at cost and -30 amortized, 5 widgets @ 12 = 60, 250 units of the index fund @ 1 = 250, 70 of the 100
    invoice INV-0 already received and all 40 of the bill BILL-0 already paid. Debits
    2950, credits 1810: retained earnings -1140.

    Then: drawings 50 and wages 60 paid from the bank (HJ1, HJ2), the second partner pays
    in 100 (HJ3), the van is fitted out for 100 (HJ4), the director lends 40 more (HR1),
    the owner is repaid 20 of expenses (HP1) and the customer pays the last 30 of INV-0
    (HR2). Operating bank 440, owner capital - drawings 50, second partner -100, director
    loan -190, Staff One 0, Owner 20, van 900 at cost, sales -100, office expenses 40,
    receivable 0, payable 0."""
    opb, ca = k("OPB"), k("CA")
    objects = [o if o.key != opb else Obj(opb, o.content_type, {**o.fields, 12: k("BCTRL")})
               for o in _stocked_masters()]
    return [
        *objects,
        *holder_objects(),
        *control_objects(),
        obj("SalesInvoice", "INVSB", {1: date(2025, 12, 20), 2: "INV-0", 3: ca, 49: [
            {2: k("S1"), 17: "Opening work", 18: D("1"), 19: D("100")},
        ]}),
        obj("PurchaseInvoice", "BILLSB", {1: date(2025, 12, 21), 2: "BILL-0", 3: k("SA"), 23: [
            {2: k("OFF"), 17: "Opening supplies", 18: D("1"), 19: D("40")},
        ]}),
        obj("PurchaseInvoiceStartingBalance", "SBBILL", {1: k("BILLSB"), 2: D("40")}),
        obj("BalanceSheetAccountStartingBalance", "SBEQ", {1: k("EQUIP"), 3: D("1000")}),
        obj("BalanceSheetAccountStartingBalance", "SBLN", {1: k("LOAN"), 2: 1, 3: D("400")}),
        obj("BankOrCashAccountStartingBalance", "SBBK", {1: opb, 2: D("500")}),
        obj("CapitalAccountStartingBalance", "SBCAP", {1: k("CAP"), 2: 1, 3: D("900")}),
        obj("SpecialAccountStartingBalance", "SBSP", {1: k("SPEC"), 2: 1, 3: D("150")}),
        obj("EmployeeStartingBalance", "SBEMP", {1: k("EMP"), 2: 1, 3: D("60")}),
        obj("FixedAssetStartingBalance", "SBVAN", {1: k("VAN"), 2: D("800"), 3: D("200")}),
        obj("IntangibleAssetStartingBalance", "SBPAT", {1: k("PAT"), 2: D("300"), 3: D("30")}),
        obj("InventoryItemStartingBalance", "SBWID", {1: k("WID"), 2: True, 3: [{2: D("5")}], 8: D("12")}),
        obj("InvestmentStartingBalance", "SBFUND", {1: k("FUND"), 2: D("250"), 3: D("1")}),
        obj("SalesInvoiceStartingBalance", "SBINV", {1: k("INVSB"), 2: D("70")}),
        obj("JournalEntry", "HJ1", {1: date(2026, 1, 10), 2: "HJ-1", 3: "Owner drawings", 14: [
            {1: CAPITAL, 11: k("CAP"), 17: k("DRAW"), 13: D("50")},
            {1: CASH_AT_BANK, 29: opb, 14: D("50")},
        ]}),
        obj("JournalEntry", "HJ2", {1: date(2026, 1, 12), 2: "HJ-2", 3: "Wages paid", 14: [
            {1: EMPLOYEES, 4: k("EMP"), 13: D("60")},
            {1: CASH_AT_BANK, 29: opb, 14: D("60")},
        ]}),
        obj("JournalEntry", "HJ3", {1: date(2026, 1, 14), 2: "HJ-3", 3: "Partner contribution", 14: [
            {1: CASH_AT_BANK, 29: opb, 13: D("100")},
            {1: k("PCTRL"), 11: k("CAP2"), 14: D("100")},
        ]}),
        obj("JournalEntry", "HJ4", {1: date(2026, 1, 16), 2: "HJ-4", 3: "Van fit-out", 14: [
            {1: FIXED_AT_COST, 9: k("VAN"), 13: D("100")},
            {1: CASH_AT_BANK, 29: opb, 14: D("100")},
        ]}),
        obj("Receipt", "HR1", {1: date(2026, 1, 18), 2: "HR-1", 3: PAID_BY_OTHER, 6: "Director", 7: opb, 11: [
            {2: SPECIAL, 12: k("SPEC"), 18: D("40")},
        ]}),
        obj("Payment", "HP1", {1: date(2026, 1, 20), 2: "HP-1", 3: PAID_BY_OTHER, 6: "Owner", 7: opb, 11: [
            {2: EXPENSE_CLAIMS, 22: k("ECP"), 18: D("20")},
        ]}),
        obj("Receipt", "HR2", {1: date(2026, 1, 22), 2: "HR-2", 3: PAID_BY_CUSTOMER, 4: ca, 7: opb, 11: [
            {2: AR, 3: ca, 4: k("INVSB"), 18: D("30")},
        ]}),
    ]


def build_starting_balances(path: Path, extra: tuple[Obj, ...] = ()) -> Path:
    return write_manager_file(path, [*starting_balance_objects(), *extra])


# ── Records that post through the accounts Manager keeps under a control account ──

HISTORY_CUTOVER = date(2026, 2, 22)


def history_objects() -> list[Obj]:
    """The starting balance business, then a quarter of the records Manager posts through its
    holders and built-in accounts. Hand-worked figures, debit positive:

    DEP1 depreciates the van 50 to the built-in depreciation account and AMO1 amortizes the
    patent 10 to its own expense account. PS1 pays Staff One 2 x 500 salary less 100 income
    tax, with a 50 employer contribution: wages 1050, PAYE -150, Staff One -900. EC1 claims
    2 x 15 of office expenses for the owner: Owner -10. LPF1 charges a late fee of 5 on
    INV-0 and WHT1 receives a 5 withholding tax certificate from the same customer.
    Billable time: BT1 2h30 @ 40 = 100 written off on 15 February, BT2 1h @ 60 invoiced on
    INV-BT (60), BT3 0h45 @ 80 = 60 unbilled, so billable time 60 and its movement -60. The
    index fund's 250 units are priced at 1.2 on 20 February (+50), 30 more is paid in on 25
    February at no change in units (-30) and the price falls to 1.1 on 1 March (-25): fund
    275, investment gains 5. The van is sold for 600 on 4 March and disposed of on 5 March:
    at cost 300 and depreciation -250 clear with a loss on disposal of 50.

    End: operating bank 1010, Staff One -900, Owner -10, van 0 and 0, patent 300 and -40,
    fund 275, receivable 60, sales -160, office expenses 70."""
    opb, ca = k("OPB"), k("CA")
    van = {1: "Van", 10: "FA1", 17: D("20"), 5: MIN_DATE, 6: True, 7: date(2026, 3, 5), 23: {}}
    patent = {1: "Patent", 10: "IA1", 15: D("10"), 22: MIN_DATE, 18: True, 19: k("AMEXP")}
    replaced = {k("VAN"): van, k("PAT"): patent}
    return [
        *(Obj(o.key, o.content_type, replaced.get(o.key, o.fields)) for o in starting_balance_objects()),
        obj("ProfitAndLossStatementAccount", "WAGES", {1: "Wages", 11: "6200", 3: EXPENSES}),
        obj("ProfitAndLossStatementAccount", "AMEXP", {1: "Patent amortization", 11: "6300", 3: EXPENSES}),
        obj("BalanceSheetAccount", "PAYE", {1: "PAYE payable", 17: "2200", 3: T["Liabilities"]}),
        obj("PayslipEarningsItem", "PEW", {1: "Salary", 3: k("WAGES")}),
        obj("PayslipDeductionItem", "PDT", {1: "Income tax", 3: k("PAYE")}),
        obj("PayslipContributionItem", "PCS", {1: "Social security", 3: k("WAGES"), 4: k("PAYE")}),
        obj("DepreciationEntry", "DEP1", {1: date(2026, 2, 1), 2: "February", 3: [{1: k("VAN"), 2: D("50")}]}),
        obj("AmortizationEntry", "AMO1", {1: date(2026, 2, 2), 2: "February", 3: [{1: k("PAT"), 2: D("10")}]}),
        obj("Payslip", "PS1", {1: date(2026, 2, 5), 2: k("EMP"), 3: [{3: D("500"), 4: k("PEW"), 7: D("2")}],
                               4: [{2: D("100"), 3: k("PDT")}], 5: [{2: D("50"), 3: k("PCS")}]}),
        obj("ExpenseClaim", "EC1", {1: date(2026, 2, 6), 3: "Stationery", 9: k("ECP"), 18: [
            {2: k("OFF"), 17: "Paper", 18: D("2"), 19: D("15")}]}),
        obj("LatePaymentFee", "LPF1", {1: date(2026, 2, 8), 2: ca, 3: k("INVSB"), 4: D("5")}),
        obj("WithholdingTaxReceipt", "WHT1", {1: ca, 2: date(2026, 2, 9), 3: D("5"), 4: "Certificate 7"}),
        obj("BillableTime", "BT1", {1: date(2026, 2, 10), 3: "Review", 6: 2, 7: 30, 8: D("40"), 9: ca, 14: 2,
                                    15: date(2026, 2, 15)}),
        obj("BillableTime", "BT2", {1: date(2026, 2, 11), 3: "Advice", 6: 1, 8: D("60"), 9: ca, 13: k("INVBT"),
                                    14: 1}),
        obj("BillableTime", "BT3", {1: date(2026, 2, 13), 3: "Drafting", 7: 45, 8: D("80"), 9: ca}),
        obj("SalesInvoice", "INVBT", {1: date(2026, 2, 12), 2: "INV-BT", 3: ca, 49: [
            {2: k("S1"), 17: "Advice", 18: D("1"), 19: D("60")}]}),
        obj("InvestmentMarketPrice", "MP1", {1: date(2026, 2, 20), 2: k("FUND"), 6: D("1.2")}),
        obj("JournalEntry", "HJ5", {1: date(2026, 2, 25), 2: "HJ-5", 3: "Fund top-up", 14: [
            {1: INVESTMENTS, 26: k("FUND"), 13: D("30")},
            {1: CASH_AT_BANK, 29: opb, 14: D("30")},
        ]}),
        obj("InvestmentMarketPrice", "MP2", {1: date(2026, 3, 1), 2: k("FUND"), 6: D("1.1")}),
        obj("JournalEntry", "HJ6", {1: date(2026, 3, 4), 2: "HJ-6", 3: "Van sold", 14: [
            {1: CASH_AT_BANK, 29: opb, 13: D("600")},
            {1: FIXED_AT_COST, 9: k("VAN"), 14: D("600")},
        ]}),
    ]


def renamed_builtin_objects() -> list[Obj]:
    """The activity built-in accounts as Manager stores them once a user renames them: the
    names are the user's own and every other figure stays as history_objects() works out."""
    return [
        obj("BalanceSheetBillableTimeAccount", None, {1: "Unbilled time"}),
        obj("BalanceSheetWithholdingTaxAccount", None, {1: "Tax withheld by customers"}),
        obj("ProfitAndLossStatementAccountBillableTimeMovement", None, {1: "Unbilled time movement"}),
        obj("ProfitAndLossStatementAccountFixedAssetDepreciation", None, {1: "Depreciation"}),
        obj("ProfitAndLossStatementAccountFixedAssetLossOnDisposal", None, {1: "Loss on asset sales"}),
        obj("ProfitAndLossStatementAccountIntangibleAssetsAmortization", None, {1: "Amortization"}),
        obj("ProfitAndLossStatementAccountIntangibleAssetsGainsLossOnDisposal", None,
            {1: "Gain or loss on intangible asset sales"}),
        obj("ProfitAndLossStatementAccountLatePaymentFees", None, {1: "Late fees charged"}),
        obj("ProfitAndLossStatementCapitalGainsOnInvestments", None, {1: "Investment results"}),
    ]


def build_history(path: Path, objects: list[Obj] | None = None) -> Path:
    """Without objects, the committed history file: history_objects() with the built-in
    accounts renamed."""
    return write_manager_file(path, [*history_objects(), *renamed_builtin_objects()] if objects is None else objects)
