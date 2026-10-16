# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The fields each carried Manager record may hold, and what Celerp does with each.

Every field Celerp reads, or checks and refuses when set, is READ. A field Celerp knows
and deliberately leaves behind is either LOST, when it holds something the user entered
and the scan's coverage lists it as mapped with loss, or IGNORED, when it is bookkeeping
the record already says elsewhere; the reason is written beside each. A field an earlier
Manager version wrote is IGNORED too: Manager's upgrade copies what it held into current
fields and leaves the old one in place, and current Manager no longer reads it. Any other
field found populated stops the scan for that record: what it records could otherwise be
lost without a word. Nested messages (lines, GUIDs, decimals, dates) are checked the same
way, field by field, to any depth."""

from __future__ import annotations

from dataclasses import dataclass

from celerp.importers.adapters.manager_io.protobuf import Message


@dataclass(frozen=True)
class Field:
    note: str | None = None                         # None: read; otherwise why it is left behind
    nested: dict[int, "Field"] | None = None        # the fields of a nested message
    lost: str | None = None                         # the coverage label of a loss the scan discloses


Schema = dict[int, Field]
READ = Field()


def ignored(note: str) -> Field:
    return Field(note)


def lost(label: str, note: str, nested: Schema | None = None) -> Field:
    return Field(note, nested, label)


# protobuf-net's .NET value types.
GUID: Schema = {1: READ, 2: READ}
DECIMAL: Schema = {1: READ, 2: READ, 3: READ}
DATE: Schema = {1: READ, 2: READ, 3: ignored("The DateTime kind. Manager dates are calendar days, read the same "
                                              "whatever the kind.")}
REF, NUMBER, DAY = Field(nested=GUID), Field(nested=DECIMAL), Field(nested=DATE)


def _fields(read: tuple[int, ...] = (), refs: tuple[int, ...] = (), numbers: tuple[int, ...] = (),
            days: tuple[int, ...] = (), **nested: Field) -> Schema:
    out: Schema = {n: READ for n in read}
    out |= {n: REF for n in refs} | {n: NUMBER for n in numbers} | {n: DAY for n in days}
    return out | {int(n.lstrip("f")): f for n, f in nested.items()}


def _lines(schema: Schema) -> Field:
    return Field(nested=schema)


# Line layouts. Link fields Celerp refuses when set are read, since setting one stops the scan.
_SALES_LINE = _fields(read=(9, 10, 13, 14, 15, 17), refs=(1, 2, 21), numbers=(18, 19, 23, 24))
_PURCHASE_LINE = _fields(read=(5, 6, 11, 17), refs=(1, 2, 21), numbers=(18, 19, 23, 24))
_SETTLEMENT_LINE = (1, 5, 6, 9, 10, 11, 12, 13, 14, 15, 19, 22, 23, 26, 27, 28)
_RECEIPT_LINE = _fields(read=_SETTLEMENT_LINE + (38,), refs=(2, 3, 4, 7, 8, 20), numbers=(17, 18, 25))
_PAYMENT_LINE = _fields(read=_SETTLEMENT_LINE + (39,), refs=(2, 3, 4, 7, 8, 20), numbers=(17, 18, 25))
_MOVEMENT_LINE = _fields(refs=(1,), numbers=(3,),
                         f2=ignored("The item's name as the line shows it. The line names the item itself, "
                                    "and the item carries its own name."))
# Quote and order lines: no account, since none of them posts.
_ORDER_LINE = _fields(read=(2,), refs=(1, 7), numbers=(3, 4, 5, 6))
_JOURNAL_LINE = _fields(read=(4, 5, 6, 8, 9, 10, 11, 12, 15, 17, 18, 20, 21, 22, 23, 24, 26, 30),
                        refs=(1, 2, 3, 29), numbers=(13, 14))

_GROUP = _fields(read=(1,))                         # a chart heading: its name
_BUILTIN = _fields(read=(1,),                       # a built-in account: the name it is shown as
                   f3=ignored("The heading the built-in account is listed under. Its role fixes its account "
                              "type, whatever heading holds it."),
                   f10=ignored("The account's position under its heading, for the order statements list it in."))
_POSITION = ignored("The account's position under its heading, for the order statements list it in.")
_CASH_FLOW = ignored("The cash flow statement section the account is listed in. Celerp places an account on its cash "
                     "flow statement by the account's type.")
# The built-in account a kind of holder (capital account, employee, fixed asset...) reports under.
_HOLDER_BUILTIN = _fields(read=(1, 12), f3=_BUILTIN[3], f11=_POSITION)
_DIVISION = lost("division", "The division the record is reported under is not carried. Celerp has no divisions.")
_ACCOUNT_CUSTOM = lost("custom fields", "Custom field values are not carried. Celerp accounts have no custom fields.")
_ASSET_DESCRIPTION = lost("description", "The asset's description is not carried. The account keeps its name "
                                          "and code.")
_ASSET_RATE = lost("depreciation rate", "The depreciation or amortization rate is not carried. Celerp carries the "
                                        "entries Manager booked, not a schedule.", DECIMAL)
_ASSET_EXPENSE = lost("custom expense account", "The account the asset's depreciation or amortization is charged "
                                                "to is not carried. The entries Manager booked keep their accounts.")


def _asset(refs: tuple[int, ...], rate: int, expense: tuple[int, int], custom: tuple[int, int],
           division: int) -> Schema:
    """A fixed or intangible asset: name, disposed flag, code, controls and disposal account and
    date are read; the rest is left behind."""
    out = _fields(read=(1, 6, 10), refs=refs, days=(7,)) | {2: _ASSET_DESCRIPTION, rate: _ASSET_RATE}
    return out | {division: _DIVISION} | {n: _ASSET_EXPENSE for n in expense} | {n: _ACCOUNT_CUSTOM for n in custom}


def _control(inactive: int, cash_flow: tuple[int, ...]) -> Schema:
    """A custom control account: its name, heading, code and inactive flag."""
    return _fields(read=(1, 17, inactive), refs=(3,), f16=_POSITION) | {n: _CASH_FLOW for n in cash_flow}


_SIDED = _fields(read=(2,), refs=(1,), numbers=(3,))  # a starting balance with a debit or credit side
_LINE_DESCRIPTION = ignored("Whether the form shows the line description column. Each line's description is "
                            "carried as it is.")
_CUSTOM_FIELDS = Field(nested={1: REF, 2: READ})     # (custom field, value); values other than the telephone are lost
_MOVEMENT = _fields(read=(1,), refs=(11,), days=(3,), f15=_lines(_MOVEMENT_LINE),
                    f4=ignored("The supplier or customer on the note. The bill or invoice it moves goods "
                               "for names the same contact, and that is what is carried."))
_PRINTED_FORM = ignored("How Manager prints the form: its theme, title, footers, images, line numbers and which "
                        "columns and totals it shows. Celerp prints every document in its own layout.")


def _printed(*numbers: int) -> dict[str, Field]:
    return {f"f{n}": _PRINTED_FORM for n in numbers}


_PAID_BY = dict(
    f3=ignored("Whether a customer, a supplier or someone else paid, as the form shows it. Each line names "
               "the contact and document it settles, and that is what is carried."),
    f4=ignored("The customer the form shows. Each customer line names the customer it settles."),
    f5=ignored("The supplier the form shows. Each supplier line names the supplier it settles."),
    f6=READ,                                        # the payer's name: lost unless it names a contact settled
    f9=lost("bank clearance date", "The date the money cleared the bank is not carried. Celerp records the "
                                   "payment on its own date.", DATE),
)
_CLEARED = ignored("Whether the money clears the bank on the payment date or later. Celerp has no clearance "
                   "status; a clearance date that is set is listed separately.")

SCHEMAS: dict[str, Schema] = {
    "BusinessDetails": _fields(read=(1, 2)),
    "BaseCurrency": _fields(read=(2, 3, 5)),
    "ForeignCurrency": _fields(read=(1, 2, 4, 6)),
    "Assets": _GROUP, "Liabilities": _GROUP, "Equity": _GROUP,
    "BalanceSheetGroup": _fields(read=(1,), refs=(3,)),
    "ProfitAndLossStatementGroup": _fields(read=(1, 6), refs=(3,),
                                           f5=ignored("The heading's position, for the order statements list it "
                                                      "in.")),
    "BalanceSheetAccount": _fields(read=(1, 17, 19), refs=(3,)),
    "ProfitAndLossStatementAccount": _fields(read=(1, 11, 12), refs=(3,)),
    "BankOrCashAccount": _fields(read=(1, 10, 12, 13), refs=(3,)),
    "BalanceSheetAccountsPayableAccount": _BUILTIN,
    "BalanceSheetAccountsReceivableAccount": _BUILTIN,
    "BalanceSheetInventoryOnHandAccount": _BUILTIN,
    "BalanceSheetRetainedEarningsAccount": _BUILTIN,
    "BalanceSheetTaxPayableAccount": _BUILTIN,
    "ProfitAndLossStatementAccountCurrencyGainsLosses": _BUILTIN,
    "ProfitAndLossStatementAccountInventoryPurchases": _BUILTIN,
    "ProfitAndLossStatementAccountInventorySales": _BUILTIN,
    **{name: _HOLDER_BUILTIN for name in (
        "BalanceSheetCapitalAccountsAccount", "BalanceSheetEmployeeClearingAccount", "BalanceSheetExpenseClaimsAccount",
        "BalanceSheetFixedAssetsAtCostAccount", "BalanceSheetFixedAssetsAccumulatedDepreciationAccount",
        "BalanceSheetIntangibleAssetsAtCostAccount", "BalanceSheetIntangibleAssetsAccumulatedAmortizationAccount")},
    # Special accounts can sit under any heading, which sets their type.
    "BalanceSheetSpecialAccountsAccount": _fields(read=(1, 12), refs=(3,), f11=_POSITION,
                                                  **{f"f{n}": _CASH_FLOW for n in (13, 14, 15, 16)}),
    "BalanceSheetInvestmentsAccount": _fields(read=(1, 2), f3=_BUILTIN[3], f4=_CASH_FLOW, f5=_POSITION),
    "TaxCode": _fields(read=(1, 5, 6, 10, 11), refs=(7,), numbers=(4,)),
    # Accounts Manager keeps a balance for under a control account, and custom control accounts.
    "CapitalAccount": _fields(read=(1, 8, 11), refs=(10,), f12=_DIVISION, f9=_ACCOUNT_CUSTOM, f13=_ACCOUNT_CUSTOM),
    "SubAccount": _fields(read=(1,)),
    "SpecialAccount": _fields(read=(1, 10, 11), refs=(5, 6, 7), f12=_DIVISION, f13=_ACCOUNT_CUSTOM,
                              f14=_ACCOUNT_CUSTOM),
    "Employee": _fields(read=(1, 9, 14), refs=(7, 15), f16=_DIVISION, f8=_ACCOUNT_CUSTOM, f17=_ACCOUNT_CUSTOM,
                        f2=lost("address", "The employee's address is not carried. Celerp keeps the employee as an "
                                           "account."),
                        f3=lost("email", "The employee's email is not carried. Celerp keeps the employee as an "
                                         "account.")),
    "ExpenseClaimsPayer": _fields(read=(1, 6), f7=_DIVISION),
    "FixedAsset": _asset((14, 18, 19), 17, (20, 21), (9, 23), 22),
    "IntangibleAsset": _asset((12, 16, 17), 15, (18, 19), (9, 21), 20),
    "Investment": _fields(read=(1, 2, 3), refs=(7,), f8=_ACCOUNT_CUSTOM, f9=_ACCOUNT_CUSTOM),
    **{name: _control(18, ()) for name in (
        "ControlAccountForBankAccounts", "ControlAccountForCapitalAccounts", "ControlAccountForFixedAssets",
        "ControlAccountForFixedAssetsAccumulatedDepreciation", "ControlAccountForIntangibleAssets",
        "ControlAccountForIntangibleAssetsAccumulatedAmortization", "ControlAccountForInvestments",
        "ControlAccountForInventoryItems")},
    "ControlAccountForEmployees": _control(18, (19,)),
    "ControlAccountForCustomers": _control(19, (18,)),
    "ControlAccountForSuppliers": _control(19, (18,)),
    "ControlAccountForSpecialAccounts": _control(22, (18, 19, 20, 21)),
    # Starting balances.
    **{name: _SIDED for name in ("BalanceSheetAccountStartingBalance", "CapitalAccountStartingBalance",
                                 "SpecialAccountStartingBalance", "EmployeeStartingBalance")},
    "BankOrCashAccountStartingBalance": _fields(refs=(1,), numbers=(2,)),
    "FixedAssetStartingBalance": _fields(refs=(1,), numbers=(2, 3)),
    "IntangibleAssetStartingBalance": _fields(refs=(1,), numbers=(2, 3)),
    "InventoryItemStartingBalance": _fields(read=(2, 4, 5, 6, 7), refs=(1,), numbers=(8,),
                                            f3=_lines(_fields(refs=(1,), numbers=(2,)))),
    "InvestmentStartingBalance": _fields(refs=(1,), numbers=(2,),
                                         f3=lost("market price", "The investment's market price is not carried. "
                                                                 "Celerp carries what it cost.", DECIMAL)),
    "SalesInvoiceStartingBalance": _fields(refs=(1,), numbers=(2,)),
    "PurchaseInvoiceStartingBalance": _fields(refs=(1,), numbers=(2,)),
    "Customer": _fields(read=(1, 2, 3, 4, 13, 15, 19), refs=(14,), f12=_CUSTOM_FIELDS),
    "Supplier": _fields(read=(1, 2, 3, 7, 10, 12, 16), refs=(11,), f9=_CUSTOM_FIELDS),
    "InventoryItem": _fields(read=(1, 10, 11, 13, 31, 32, 38), numbers=(2, 3)),
    "DefaultInventoryLocation": _fields(f1=ignored("The location's name. Every item is held at the one "
                                                   "default location, so no location is carried.")),
    "InventoryUnitCost": _fields(refs=(2,), numbers=(3,), days=(1,)),
    "SalesInvoice": _fields(read=(2, 4, 8, 12, 22, 28, 29, 31, 32, 35, 54, 69), refs=(3, 30, 50, 51), days=(1, 6),
                            f49=_lines(_SALES_LINE), f64=NUMBER, f65=READ, f48=_LINE_DESCRIPTION),
    "PurchaseInvoice": _fields(read=(2, 7, 9, 14, 15, 19, 24, 31, 64, 65, 68), refs=(3, 13, 28, 29), days=(1, 5),
                               f23=_lines(_PURCHASE_LINE), f61=NUMBER, f62=READ, f22=_LINE_DESCRIPTION),
    "CreditNote": _fields(read=(2, 4, 6, 9, 13, 15, 16, 31), refs=(3, 8), days=(1,), numbers=(30,),
                          f22=_lines(_SALES_LINE), f21=_LINE_DESCRIPTION),
    "DebitNote": _fields(read=(2, 5, 7, 10, 11, 23), refs=(3, 6), days=(1,), numbers=(22,),
                         f16=_lines(_PURCHASE_LINE)),
    "SalesQuote": _fields(read=(2, 4, 8, 9, 13, 16, 17, 18, 25, 26, 28, 29, 30, 31), refs=(3,), days=(1,),
                          f24=_lines(_ORDER_LINE), **_printed(14, 19, 20, 21, 22, 23, 27, 32, 33, 34, 36)),
    "SalesOrder": _fields(read=(2, 9, 11, 12, 13, 14, 21, 23, 24, 25, 26), refs=(3, 19), days=(1,),
                          f18=_lines(_ORDER_LINE), **_printed(15, 16, 17, 22, 27, 28, 29, 31, 32, 33)),
    "PurchaseQuote": _fields(read=(2, 9, 11, 12, 13, 19, 25, 26, 27, 28), refs=(3,), days=(1,),
                             f17=_lines(_ORDER_LINE), **_printed(14, 15, 16, 18, 20, 21, 22, 23, 24, 29, 30, 31)),
    "PurchaseOrder": _fields(read=(2, 9, 11, 12, 13, 20, 22, 23, 24, 25), refs=(3, 18), days=(1,),
                             f17=_lines(_ORDER_LINE), **_printed(14, 15, 16, 21, 26, 27, 28, 30, 31)),
    "GoodsReceipt": _MOVEMENT | {17: REF},
    "DeliveryNote": _MOVEMENT | {19: REF},
    "Receipt": _fields(read=(2, 10, 31, 32, 33, 34, 35, 38, 39), refs=(7,), days=(1,), f11=_lines(_RECEIPT_LINE),
                       f45=NUMBER, f46=READ, f36=_CLEARED, **_PAID_BY),
    "Payment": _fields(read=(2, 10, 32, 33, 34, 35, 36, 39, 40, 47), refs=(7,), days=(1,), numbers=(46,),
                       f11=_lines(_PAYMENT_LINE), f37=_CLEARED, **_PAID_BY),
    "InterAccountTransfer": _fields(read=(5, 6), refs=(2, 3), numbers=(8, 9), days=(1,)),
    "JournalEntry": _fields(read=(2, 3), refs=(8,), days=(1,), f14=_lines(_JOURNAL_LINE)),
    "Attachment": _fields(read=(2, 4, 12), refs=(6,),
                          f1=lost("attach time", "When the file was attached is not carried. The file is "
                                  "carried as it is, onto the record it belongs to.", DATE)),
    "LockDate": _fields(read=(2,), days=(1,)),
    "ExchangeRate": _fields(refs=(2,), numbers=(6,), days=(1,)),
}

# The fields each type kept from earlier Manager versions. A field the schema above reads
# (a contact's old telephone, the only place an upgraded file holds it as such) stays read.
_OBSOLETE: dict[str, tuple[int, ...]] = {
    "BusinessDetails": (3, 4, 6),
    "BaseCurrency": (1,),
    "ForeignCurrency": (5, 7, 8),
    "BalanceSheetGroup": (2,),
    "ProfitAndLossStatementGroup": (2, 4),
    "BalanceSheetAccount": (2, 4, 5, 6, 7, 9, 10, 11, 13, 15, 20),
    "ProfitAndLossStatementAccount": (2, 9),
    "BankOrCashAccount": (2, 5, 6, 7, 8, 11, 14, 15, 25, 26, 27, 28, 39),
    "TaxCode": (3, 8, 9, 12),
    "CapitalAccount": (2, 3, 4, 5, 6, 7),
    "SpecialAccount": (2, 3, 4, 8, 9, 15, 16),
    "Employee": (4, 5, 6, 10, 11, 12, 13, 18, 19),
    "ExpenseClaimsPayer": (4, 5),
    "FixedAsset": (3, 4, 5, 8, 11, 12, 13, 15, 16, 25),
    "IntangibleAsset": (4, 8, 11, 13, 14, 22),
    "Investment": (4, 5, 6, 10),
    "Customer": (4, 5, 6, 7, 8, 9, 10, 11, 17, 18, 23, 24, 31, 32),
    "Supplier": (3, 4, 5, 6, 8, 14, 15, 17, 20, 21),
    "InventoryItem": (5, 6, 7, 8, 14, 21, 22, 23, 24, 25, 26, 27),
    "SalesInvoice": (5, 7, 9, 10, 14, 17, 18, 19, 21, 23, 47, 55, 58, 66, 67),
    "PurchaseInvoice": (4, 6, 11, 18, 21),
    "CreditNote": (5, 32, 33),
    "DebitNote": (4,),
    "SalesQuote": (5, 7, 11, 12, 15),
    "SalesOrder": (4, 5, 6, 7, 8, 20),
    "PurchaseQuote": (4,),
    "PurchaseOrder": (4, 5, 6, 7, 8, 19),
    "GoodsReceipt": (7,),
    "DeliveryNote": (7, 9),
    "Receipt": (8, 14, 21, 37),
    "Payment": (8, 14, 21, 38),
    "InterAccountTransfer": (4, 10, 13, 14, 20, 21),
    "JournalEntry": (4, 5, 6, 12, 24),
    "Attachment": (3, 5, 7, 8, 9, 10, 11),
    "ExchangeRate": (3, 4, 5),
}
_EARLIER_VERSION = ignored("Written by an earlier Manager version. Manager's upgrade copied what it held into the "
                           "current fields, and current Manager no longer reads it.")
for _name, _numbers in _OBSOLETE.items():
    SCHEMAS[_name] = {n: _EARLIER_VERSION for n in _numbers} | SCHEMAS[_name]


def check(name: str, m: Message) -> tuple[str | None, list[Field]]:
    """The dotted path of the first populated field *name* holds that its schema does not
    list, or None when every populated field is listed; and every populated LOST field."""
    found: list[Field] = []
    return _walk(SCHEMAS[name], m, "", found), found


def _walk(schema: Schema, m: Message, prefix: str, found: list[Field]) -> str | None:
    for number in sorted(m.fields):
        path = f"{prefix}{number}"
        entry = schema.get(number)
        if entry is None:
            return path
        if entry.lost:
            found.append(entry)
        if entry.nested is None:
            continue
        for nested in m.messages(number):
            unknown = _walk(entry.nested, nested, f"{path}.", found)
            if unknown:
                return unknown
    return None
