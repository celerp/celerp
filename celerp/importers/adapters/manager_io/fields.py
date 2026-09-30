# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The fields each carried Manager record may hold, and what Celerp does with each.

Every field Celerp reads, or checks and refuses when set, is READ. A field Celerp knows
and deliberately leaves behind is IGNORED, with the reason written beside it. Any other
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


Schema = dict[int, Field]
READ = Field()


def ignored(note: str) -> Field:
    return Field(note)


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
_JOURNAL_LINE = _fields(read=(4, 5, 6, 8, 9, 10, 11, 12, 15, 17, 18, 20, 21, 22, 23, 24, 26, 30),
                        refs=(1, 2, 3, 29), numbers=(13, 14))

_GROUP = _fields(read=(1,))                         # a chart heading: its name
_BUILTIN = _fields(read=(1,))                       # a built-in account: the name it is shown as
_MOVEMENT = _fields(read=(1,), refs=(11,), days=(3,), f15=_lines(_MOVEMENT_LINE),
                    f4=ignored("The supplier or customer on the note. The bill or invoice it moves goods "
                               "for names the same contact, and that is what is carried."))
_RATE = ignored("The exchange rate of a record in a foreign currency. Such a record is refused at the scan, "
                "so no rate is ever carried.")
_PAID_BY = dict(
    f3=ignored("Whether a customer, a supplier or someone else paid, as the form shows it. Each line names "
               "the contact and document it settles, and that is what is carried."),
    f4=ignored("The customer the form shows. Each customer line names the customer it settles."),
    f5=ignored("The supplier the form shows. Each supplier line names the supplier it settles."),
    f6=ignored("The name typed for a payer who is neither a customer nor a supplier. Celerp records the "
               "payment against its accounts, without a payer name."),
)

SCHEMAS: dict[str, Schema] = {
    "BusinessDetails": _fields(read=(1, 2)),
    "BaseCurrency": _fields(read=(2, 3, 5)),
    "ForeignCurrency": _fields(read=(1, 2, 4, 6)),
    "Assets": _GROUP, "Liabilities": _GROUP, "Equity": _GROUP,
    "BalanceSheetGroup": _fields(read=(1,), refs=(3,)),
    "ProfitAndLossStatementGroup": _fields(read=(1, 6), refs=(3,)),
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
    "TaxCode": _fields(read=(1, 5, 6, 10, 11), refs=(7,), numbers=(4,)),
    "Customer": _fields(read=(1, 2, 3, 13, 15, 19), refs=(14,)),
    "Supplier": _fields(read=(1, 2, 7, 10, 12, 16), refs=(11,)),
    "InventoryItem": _fields(read=(1, 10, 11, 13, 31, 32), numbers=(2, 3)),
    "DefaultInventoryLocation": _fields(f1=ignored("The location's name. Every item is held at the one "
                                                   "default location, so no location is carried.")),
    "InventoryUnitCost": _fields(refs=(2,), numbers=(3,), days=(1,)),
    "SalesInvoice": _fields(read=(2, 8, 12, 22, 28, 29, 31, 32, 35, 54, 69), refs=(3, 30), days=(1, 6),
                            f49=_lines(_SALES_LINE), f64=_RATE),
    "PurchaseInvoice": _fields(read=(2, 7, 9, 14, 15, 19, 24, 31, 64, 65, 68), refs=(3, 13), days=(1, 5),
                               f23=_lines(_PURCHASE_LINE), f61=_RATE,
                               f62=ignored("Whether the exchange rate is entered inverted. It goes with the "
                                           "rate, which is never carried.")),
    "CreditNote": _fields(read=(2, 6, 9, 13, 15, 16), refs=(3, 8), days=(1,), f22=_lines(_SALES_LINE)),
    "DebitNote": _fields(read=(2, 5, 7, 10, 11), refs=(3, 6), days=(1,), f16=_lines(_PURCHASE_LINE)),
    "GoodsReceipt": _MOVEMENT | {17: REF},
    "DeliveryNote": _MOVEMENT | {19: REF},
    "Receipt": _fields(read=(2, 10, 31, 32, 33, 34, 35, 38, 39), refs=(7,), days=(1,), f11=_lines(_RECEIPT_LINE),
                       f45=_RATE, **_PAID_BY),
    "Payment": _fields(read=(2, 10, 32, 33, 34, 35, 36, 39, 40), refs=(7,), days=(1,), f11=_lines(_PAYMENT_LINE),
                       **_PAID_BY),
    "InterAccountTransfer": _fields(read=(5, 6), refs=(2, 3), numbers=(8, 9), days=(1,)),
    "JournalEntry": _fields(read=(2, 3), refs=(8,), days=(1,), f14=_lines(_JOURNAL_LINE)),
    "Attachment": _fields(read=(2, 4, 12), refs=(6,),
                          f1=Field("When the file was attached. The file is carried as it is, onto the "
                                   "record it belongs to, without the attach time.", DATE)),
    "LockDate": _fields(read=(2,), days=(1,)),
}


def unknown_field(name: str, m: Message) -> str | None:
    """The dotted path of the first populated field *name* holds that its schema does not
    list, or None when every populated field is listed."""
    return _unknown(SCHEMAS[name], m, "")


def _unknown(schema: Schema, m: Message, prefix: str) -> str | None:
    for number in sorted(m.fields):
        path = f"{prefix}{number}"
        entry = schema.get(number)
        if entry is None:
            return path
        if entry.nested is None:
            continue
        for nested in m.messages(number):
            found = _unknown(entry.nested, nested, f"{path}.")
            if found:
                return found
    return None
