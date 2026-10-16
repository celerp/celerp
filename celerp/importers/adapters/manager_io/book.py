# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Decode a Manager business file into plain source records and classify each one.

Every object row is classified by its content type (`types.CONTENT_TYPES`).
Objects of a carried type are decoded field by field into the dataclasses
below; any decode failure, any source feature whose accounting effect Celerp
cannot yet reproduce, and any reference to a record that does not exist puts
that one object in a blocking coverage row with a short, content-free note.
Every object lands in exactly one coverage row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from celerp.importers.adapters.manager_io import fields
from celerp.importers.adapters.manager_io.protobuf import DecodeError, Message, decode
from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
from celerp.importers.adapters.manager_io.types import CONTENT_TYPES, GUIDS, NOTES, TARGETS, TYPE_NAMES
from celerp.importers.schema import CIFCoverageEntry, CoverageClass
from celerp.services.money import checked_exchange_rate, currency_dp, round_money, split_to_base

MAX_NOTE = 300

# Chart roots and built-in accounts are keyed by their content type GUID (a singleton's key is its type).
INCOME = "95713fac-30d3-42e4-b536-dd7bc4f7a80e"
EXPENSES = "fd003045-876e-439e-b923-1904453f5c30"
ASSETS, LIABILITIES, EQUITY = GUIDS["Assets"], GUIDS["Liabilities"], GUIDS["Equity"]
CASH_AT_BANK = GUIDS["BalanceSheetCashAtBankAccount"]
AR = GUIDS["BalanceSheetAccountsReceivableAccount"]
AP = GUIDS["BalanceSheetAccountsPayableAccount"]
TAX = GUIDS["BalanceSheetTaxPayableAccount"]
INVENTORY = GUIDS["BalanceSheetInventoryOnHandAccount"]
RETAINED = GUIDS["BalanceSheetRetainedEarningsAccount"]
FX_GAIN = GUIDS["ProfitAndLossStatementAccountCurrencyGainsLosses"]
INVENTORY_SALES = GUIDS["ProfitAndLossStatementAccountInventorySales"]
INVENTORY_PURCHASES = GUIDS["ProfitAndLossStatementAccountInventoryPurchases"]
DEFAULT_LOCATION = GUIDS["DefaultInventoryLocation"]

# Built-in account: (account type, control, name used when the file has no object for it).
BUILTINS: dict[str, tuple[str, str | None, str]] = {
    AR: ("asset", "receivable", "Accounts receivable"),
    AP: ("liability", "payable", "Accounts payable"),
    TAX: ("liability", "tax", "Tax payable"),
    INVENTORY: ("asset", "inventory", "Inventory on hand"),
    RETAINED: ("equity", "retained_earnings", "Retained earnings"),
    FX_GAIN: ("expense", "exchange_difference", "Foreign exchange gains and losses"),
}
# Built-in accounts a business has once it sells inventory items: sales income, and the
# cost of the items sold.
INVENTORY_BUILTINS: dict[str, tuple[str, str | None, str]] = {
    INVENTORY_SALES: ("revenue", None, "Inventory - sales"),
    INVENTORY_PURCHASES: ("cogs", None, "Inventory - cost"),
}
ROOT_TYPES = {ASSETS: "asset", LIABILITIES: "liability", EQUITY: "equity", INCOME: "revenue", EXPENSES: "expense"}

# Built-in control accounts Manager keeps a balance per holder under: (account type, name used
# when the file has no object for it). A special account takes its type from its heading.
CAPITAL = GUIDS["BalanceSheetCapitalAccountsAccount"]
SPECIAL = GUIDS["BalanceSheetSpecialAccountsAccount"]
EMPLOYEES = GUIDS["BalanceSheetEmployeeClearingAccount"]
EXPENSE_CLAIMS = GUIDS["BalanceSheetExpenseClaimsAccount"]
FA_AT_COST = GUIDS["BalanceSheetFixedAssetsAtCostAccount"]
FA_ACCUM = GUIDS["BalanceSheetFixedAssetsAccumulatedDepreciationAccount"]
IA_AT_COST = GUIDS["BalanceSheetIntangibleAssetsAtCostAccount"]
IA_ACCUM = GUIDS["BalanceSheetIntangibleAssetsAccumulatedAmortizationAccount"]
INVESTMENTS = GUIDS["BalanceSheetInvestmentsAccount"]
HOLDER_BUILTINS: dict[str, tuple[str, str]] = {
    CAPITAL: ("equity", "Capital accounts"),
    SPECIAL: ("asset", "Special accounts"),
    EMPLOYEES: ("liability", "Employee clearing account"),
    EXPENSE_CLAIMS: ("liability", "Expense claims"),
    FA_AT_COST: ("asset", "Fixed assets - at cost"),
    FA_ACCUM: ("asset", "Fixed assets - accumulated depreciation"),
    IA_AT_COST: ("asset", "Intangible assets - at cost"),
    IA_ACCUM: ("asset", "Intangible assets - accumulated amortization"),
    INVESTMENTS: ("asset", "Investments"),
}
# Built-in accounts Manager adds once a record type that posts to them is used: (account type,
# name used when the file has no object for it).
BILLABLE_TIME = GUIDS["BalanceSheetBillableTimeAccount"]
BILLABLE_TIME_MOVEMENT = GUIDS["ProfitAndLossStatementAccountBillableTimeMovement"]
WITHHOLDING_TAX = GUIDS["BalanceSheetWithholdingTaxAccount"]
LATE_FEES = GUIDS["ProfitAndLossStatementAccountLatePaymentFees"]
INVESTMENT_GAINS = GUIDS["ProfitAndLossStatementCapitalGainsOnInvestments"]
FA_DEPRECIATION = GUIDS["ProfitAndLossStatementAccountFixedAssetDepreciation"]
FA_DISPOSAL = GUIDS["ProfitAndLossStatementAccountFixedAssetLossOnDisposal"]
IA_AMORTIZATION = GUIDS["ProfitAndLossStatementAccountIntangibleAssetsAmortization"]
IA_DISPOSAL = GUIDS["ProfitAndLossStatementAccountIntangibleAssetsGainsLossOnDisposal"]
ACTIVITY_BUILTINS: dict[str, tuple[str, str]] = {
    BILLABLE_TIME: ("asset", "Billable time"),
    WITHHOLDING_TAX: ("asset", "Withholding tax"),
    BILLABLE_TIME_MOVEMENT: ("revenue", "Billable time - movement"),
    LATE_FEES: ("revenue", "Late payment fees"),
    INVESTMENT_GAINS: ("revenue", "Investment gains (losses)"),
    FA_DEPRECIATION: ("expense", "Fixed assets - depreciation"),
    FA_DISPOSAL: ("expense", "Fixed assets - loss on disposal"),
    IA_AMORTIZATION: ("expense", "Intangible assets - amortization"),
    IA_DISPOSAL: ("expense", "Intangible assets - gain (loss) on disposal"),
}
# Custom control accounts, each reported in place of the built-in one of its kind.
CONTROL_KINDS = {
    "ControlAccountForBankAccounts": CASH_AT_BANK, "ControlAccountForCapitalAccounts": CAPITAL,
    "ControlAccountForEmployees": EMPLOYEES, "ControlAccountForSpecialAccounts": SPECIAL,
    "ControlAccountForFixedAssets": FA_AT_COST, "ControlAccountForFixedAssetsAccumulatedDepreciation": FA_ACCUM,
    "ControlAccountForIntangibleAssets": IA_AT_COST,
    "ControlAccountForIntangibleAssetsAccumulatedAmortization": IA_ACCUM,
    "ControlAccountForInvestments": INVESTMENTS,
}
_CONTROL_INACTIVE = {"ControlAccountForEmployees": 18, "ControlAccountForSpecialAccounts": 22}
# Custom control accounts for customers, suppliers and inventory items. Celerp keeps every
# customer balance on receivables, every supplier balance on payables and all stock on
# inventory on hand, so these are not accounts of their own.
PARTY_CONTROLS = {"Customer": "ControlAccountForCustomers", "Supplier": "ControlAccountForSuppliers"}
MERGED_CONTROLS = frozenset({*PARTY_CONTROLS.values(), "ControlAccountForInventoryItems"})
CONTROL_NOTE = ("A custom control account for customers or suppliers is not carried. Celerp keeps every customer "
                "balance on accounts receivable and every supplier balance on accounts payable.")
# The key of the one journal starting balances post as.
STARTING = "starting-balances"


def source_key(key: str) -> str:
    """The object a record or account comes from. A second journal of a record, and a second
    account of a holder, is keyed by the object's key and a suffix after a colon."""
    return key.split(":")[0]

DOC_TYPES = {
    "SalesInvoice": "invoice", "CreditNote": "credit_note", "PurchaseInvoice": "bill", "DebitNote": "debit_note",
}
# Quotes and orders: none posts. Celerp has no sales order, so one arrives as the quotation
# that converts to an invoice, and no purchase quote, so one arrives as a purchase order.
ORDER_TYPES = {
    "SalesQuote": "quotation", "SalesOrder": "quotation", "PurchaseQuote": "purchase_order",
    "PurchaseOrder": "purchase_order",
}
SALES_TYPES = frozenset({"SalesInvoice", "CreditNote", "SalesQuote", "SalesOrder"})
# The document a note settles: a credit note reduces a sales invoice, a debit note a purchase invoice.
NOTE_OF = {"CreditNote": "SalesInvoice", "DebitNote": "PurchaseInvoice"}
# Documents Celerp has no document type for: each posts as a journal on the document it notes.
JOURNAL_DOCUMENTS = frozenset({"DebitNote"})
# The document each physical stock record moves goods for.
MOVEMENT_OF = {"DeliveryNote": "SalesInvoice", "GoodsReceipt": "PurchaseInvoice"}
INBOUND = frozenset({"GoodsReceipt", "PurchaseInvoice"})
LOCATION_NOTE = "Stock held at a location other than the default one; Celerp keeps each item at one location."
# Inventory records Celerp cannot carry yet: each blocks under its own reason.
INVENTORY_BLOCKERS = {
    "CustomInventoryLocation": ("multiple locations", "A second inventory location; Celerp keeps each item at one "
                                "location."),
    "InventoryTransfer": ("inter-location transfer", "Stock moved between locations; Celerp keeps each item at one "
                          "location."),
}


class Blocked(Exception):
    """One object carries a source feature Celerp cannot reproduce faithfully."""

    def __init__(self, reason: str, note: str):
        super().__init__(note)
        self.reason = reason
        self.note = note


def _money(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal(0)


# ── Source records ─────────────────────────────────────────────────────────────

@dataclass
class Currency:
    key: str
    code: str
    name: str | None
    precision: int
    inactive: bool = False


@dataclass
class Account:
    key: str
    source_type: str
    name: str
    code: str | None = None
    group: str | None = None
    account_type: str | None = None
    control: str | None = None
    currency: str | None = None                # currency object key; None is the base currency
    inactive: bool = False
    parent: str | None = None                  # the control account it is reported under
    kind: str | None = None                    # the built-in control account its kind reports under


@dataclass
class Group:
    key: str
    source_type: str
    parent: str | None
    pl_type: int = 0                           # P&L group: 0 income, 1 expense, 2 subgroup of its parent


@dataclass
class TaxCode:
    key: str
    name: str
    rate: Decimal
    account: str
    inactive: bool = False


@dataclass
class Contact:
    key: str
    source_type: str                           # Customer or Supplier
    name: str
    code: str | None
    currency: str | None
    email: str | None
    address: str | None
    inactive: bool
    phone: str | None = None
    control: str | None = None                 # a custom control account it is reported under


@dataclass
class Item:
    key: str
    code: str | None
    name: str
    unit: str | None
    sales_price: Decimal | None
    purchase_price: Decimal | None
    inactive: bool
    average_cost: bool = False                 # weighted average costing; otherwise first in, first out


# A line's account under a control account: (its type, its key, a capital sub-account or None).
Holder = tuple[str, str, str | None]


@dataclass
class Line:
    """A priced line. `net` excludes tax and is after discount; figures are in the record currency.
    A line discount is either a percentage (`discount_percent`) or an exact amount (`discount`)."""
    account: str | None
    item: str | None
    description: str | None
    quantity: Decimal
    unit_price: Decimal
    discount: Decimal
    tax_code: str | None
    discount_percent: Decimal | None = None
    net: Decimal = Decimal(0)
    tax: Decimal = Decimal(0)
    contact: str | None = None                 # party line of a settlement
    document: str | None = None                # settled document of a party line
    cost: Decimal = Decimal(0)                 # cost of sales of an inventory item sold, base currency
    base_net: Decimal = Decimal(0)             # net and tax in the base currency, at the record's rate
    base_tax: Decimal = Decimal(0)
    holder: Holder | None = None               # the account under a control account the line posts to


@dataclass
class Document:
    key: str
    source_type: str
    date: date
    due: date | None
    ref: str | None
    contact: str
    currency: str | None                       # currency object key; None is the base currency
    include_tax: bool
    description: str | None
    lines: list[Line]
    applies_to: str | None = None              # the invoice a note settles
    moves_stock: bool = False                  # an invoice that moves its own stock, with no physical record
    location: str | None = None                # where such an invoice moves its stock
    rate: Decimal | None = None                # base units per document unit; 1 for a base currency document
    billing_address: str | None = None
    valid_until: date | None = None            # the last day a quote can be accepted
    cancelled: bool = False                    # a quote or order cancelled in Manager
    names: tuple[str, ...] = ()                # the quotes and orders the record says it came from, nearest first
    converted_from: str | None = None          # the carried quote or order it came from
    converted_to: str | None = None            # the record a quote or order became

    @property
    def doc_type(self) -> str:
        return DOC_TYPES.get(self.source_type) or ORDER_TYPES[self.source_type]

    @property
    def order_status(self) -> str:
        """A quote or order's status: cancelled is void, a purchase quote still a draft, any
        other issued, converted ones included."""
        if self.cancelled:
            return "void"
        if self.source_type == "PurchaseQuote" and self.converted_to is None:
            return "draft"
        return "awaiting_payment"

    @property
    def total(self) -> Decimal:
        return sum((ln.net + ln.tax for ln in self.lines), Decimal(0))

    @property
    def base_total(self) -> Decimal:
        return sum((ln.base_net + ln.base_tax for ln in self.lines), Decimal(0))

    @property
    def tax_total(self) -> Decimal:
        return sum((ln.tax for ln in self.lines), Decimal(0))


def line_account(doc: Document, line: Line) -> str | None:
    """The account a document line posts its net to: an inventory item bought goes to
    inventory on hand, one sold to inventory sales. A quote or order posts nothing."""
    if doc.source_type in ORDER_TYPES:
        return None
    if not line.item:
        return line.account
    return INVENTORY if doc.source_type == "PurchaseInvoice" else INVENTORY_SALES


@dataclass
class MoveLine:
    item: str
    quantity: Decimal                          # signed: positive into stock, negative out
    line: int = 0                              # the document line it moves goods for
    value: Decimal = Decimal(0)                # signed base value, set when stock is valued


@dataclass
class Movement:
    """Goods physically moved into or out of stock: a goods receipt, a delivery note, or an
    invoice that moves its own stock."""
    key: str
    source_type: str
    date: date
    ref: str | None
    document: str | None                       # the invoice or bill the goods belong to
    location: str | None
    lines: list[MoveLine]
    source_lines: tuple[tuple[str, Decimal], ...] = ()   # (item, signed quantity) as the record states them


@dataclass
class Settlement:
    key: str
    source_type: str                           # Receipt or Payment
    date: date
    ref: str | None
    bank: str
    currency: str | None
    description: str | None
    party_lines: list[Line]                    # receivable or payable lines that settle a document
    other_lines: list[Line]                    # everything else, money on account included: kept as a journal
    include_tax: bool = True
    rate: Decimal | None = None                # base units per settlement unit; 1 in the base currency
    payer: str | None = None                   # the name typed in the paid by or payee box
    form_contacts: tuple[str, ...] = ()        # the customer and supplier the form shows

    @property
    def kind(self) -> str:
        return "receipt" if self.source_type == "Receipt" else "payment"

    @property
    def contact(self) -> str | None:
        return self.party_lines[0].contact if self.party_lines else None


@dataclass
class Transfer:
    key: str
    date: date
    ref: str | None
    description: str | None
    from_bank: str
    to_bank: str
    amount: Decimal                            # leaves the source account, in its currency
    to_amount: Decimal                         # arrives in the destination account, in its currency
    base: Decimal = Decimal(0)                 # what moves in the base currency
    rate: Decimal | None = None                # the rate table's rate, for a transfer within one foreign currency


@dataclass
class JournalLine:
    account: str
    contact: str | None
    debit: Decimal
    credit: Decimal
    description: str | None
    base_debit: Decimal = Decimal(0)           # debit and credit in the base currency, at the journal's rate
    base_credit: Decimal = Decimal(0)
    holder: Holder | None = None


@dataclass
class StartingBalance:
    """A balance a business started from instead of history. Resolved into the lines it posts
    to the opening journal, debit positive, and the stock or invoice payment it starts with."""
    key: str
    source_type: str
    target: str                                # the account, item or invoice it is the starting balance of
    amounts: tuple[Decimal, ...]               # as the record states them, debit positive
    quantity: Decimal = Decimal(0)             # an inventory item's quantity
    lines: list[tuple[str, Decimal, str | None]] = field(default_factory=list)   # (account, amount, contact)
    stock: tuple[Decimal, Decimal] | None = None                                 # (quantity, base value)
    paid: Decimal = Decimal(0)                 # what an invoice or bill had received or paid before


@dataclass
class Journal:
    key: str
    date: date
    ref: str | None
    narration: str | None
    currency: str | None
    lines: list[JournalLine]
    rate: Decimal | None = None                # base units per journal unit; 1 in the base currency


@dataclass
class Asset:
    """A fixed or intangible asset's posting terms."""
    expense: str                               # the account depreciation or amortization posts to
    disposal_account: str                      # the account the gain or loss on disposal posts to
    disposed: date | None = None               # the disposal date, for an asset marked disposed


@dataclass
class MarketPrice:
    key: str
    date: date
    investment: str
    price: Decimal


@dataclass
class Rate:
    """One row of Manager's exchange rate table: base units per foreign unit from a date."""
    key: str
    effective: date
    currency: str                              # currency object key
    rate: Decimal


@dataclass
class AttachmentRef:
    key: str
    name: str
    size: int
    target: str | None
    sha256: bytes | None


@dataclass
class Verdict:
    source_type: str                           # coverage row label
    coverage_class: CoverageClass
    target: str | None = None
    note: str | None = None


@dataclass
class Book:
    """Everything decoded from one Manager business file."""
    schema_version: int | None = None
    company_key: str | None = None
    company_name: str | None = None
    address: str | None = None
    base_key: str | None = None
    base_code: str | None = None
    base_name: str | None = None
    precision: int = 2
    currencies: dict[str, Currency] = field(default_factory=dict)
    rates: list[Rate] = field(default_factory=list)
    groups: dict[str, Group] = field(default_factory=dict)
    accounts: dict[str, Account] = field(default_factory=dict)
    tax_codes: dict[str, TaxCode] = field(default_factory=dict)
    contacts: dict[str, Contact] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    orders: dict[str, Document] = field(default_factory=dict)    # quotes and orders, which never post
    settlements: dict[str, Settlement] = field(default_factory=dict)
    transfers: dict[str, Transfer] = field(default_factory=dict)
    journals: dict[str, Journal] = field(default_factory=dict)
    movements: dict[str, Movement] = field(default_factory=dict)
    unit_costs: dict[str, list[tuple[date, Decimal]]] = field(default_factory=dict)   # item -> dated costs
    moves: list[Movement] = field(default_factory=list)          # valued movements, in the order stock moved
    attachments: dict[str, AttachmentRef] = field(default_factory=dict)
    sub_accounts: dict[str, str] = field(default_factory=dict)   # capital sub-account key -> name
    merged_controls: dict[str, str] = field(default_factory=dict)  # control key -> type, for those not carried
    controls: set[str] = field(default_factory=set)              # accounts that only report those under them
    starting: dict[str, StartingBalance] = field(default_factory=dict)
    assets: dict[str, Asset] = field(default_factory=dict)
    prices: dict[str, MarketPrice] = field(default_factory=dict)
    payslip_items: dict[str, tuple[str, tuple[str | None, ...]]] = field(default_factory=dict)  # (type, accounts)
    activity: dict[str, Message] = field(default_factory=dict)  # records posted as journals once all are read
    starting_date: date | None = None                            # the day before the first dated record
    names: dict[str, str] = field(default_factory=dict)          # object key -> Manager type name
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    losses: dict[tuple[str, str], str] = field(default_factory=dict)   # (key, row label) -> note
    object_counts: dict[str, int] = field(default_factory=dict)
    history: dict[str, int] = field(default_factory=dict)
    lock_date: date | None = None                                # the date periods are locked through

    # ── classification ──

    def accept(self, type_name: str, key: str, label: str | None = None, klass: CoverageClass | None = None,
               target: str | None = None, note: str | None = None) -> None:
        self.verdicts[key] = Verdict(
            label or type_name, klass or CONTENT_TYPES[GUIDS[type_name]],
            target if target is not None else TARGETS.get(type_name), note if note is not None else NOTES.get(type_name),
        )

    def block(self, type_name: str, key: str, reason: str, note: str) -> None:
        self.verdicts[key] = Verdict(f"{type_name} ({reason})", CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER,
                                     None, note[:MAX_NOTE])

    def set_aside(self, type_name: str, key: str, reason: str, note: str) -> None:
        """A quote or order Celerp cannot carry faithfully: listed as not carried. It moves no
        money, so it never stops the migration."""
        self.orders.pop(key, None)
        self.verdicts[key] = Verdict(f"{type_name} ({reason})", CoverageClass.UNSUPPORTED_NONFINANCIAL, None,
                                     note[:MAX_NOTE])

    def is_blocked(self, key: str) -> bool:
        """Whether the object is blocked. An account Celerp adds itself, a built-in the file has
        no object for, has no verdict and is never blocked."""
        verdict = self.verdicts.get(key)
        return verdict is not None and verdict.coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER

    @property
    def blocking_rows(self) -> list[CIFCoverageEntry]:
        stop = (CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER, CoverageClass.UNCLASSIFIED)
        return [row for row in self.coverage() if row.coverage_class in stop]

    def coverage(self) -> list[CIFCoverageEntry]:
        rows: dict[str, CIFCoverageEntry] = {}
        notes: dict[str, list[str]] = {}
        for verdict in self.verdicts.values():
            row = rows.get(verdict.source_type)
            if row is None:
                rows[verdict.source_type] = CIFCoverageEntry(
                    source_type=verdict.source_type, count=1, coverage_class=verdict.coverage_class,
                    target=verdict.target,
                )
            else:
                row.count += 1
            if verdict.note and verdict.note not in notes.setdefault(verdict.source_type, []):
                notes[verdict.source_type].append(verdict.note)
        for label, row in rows.items():
            if notes.get(label):
                row.note = " ".join(notes[label])[:MAX_NOTE]
        # A value a carried record holds that Celerp leaves behind, listed once per record.
        for (key, label), note in self.losses.items():
            if self.is_blocked(key):
                continue
            row = rows.setdefault(label, CIFCoverageEntry(source_type=label, count=0,
                                                          coverage_class=CoverageClass.MAPPED_WITH_LOSS, note=note))
            row.count += 1
        return sorted(rows.values(), key=lambda r: r.source_type)

    # ── lookups ──

    def currency_code(self, key: str | None) -> str:
        return self.base_code if key is None else self.currencies[key].code

    def is_foreign(self, key: str | None) -> bool:
        return key is not None and self.currencies[key].code != self.base_code

    def dated_records(self) -> list[tuple[date, str]]:
        dated = [(r.date, k) for group in (self.documents, self.settlements, self.transfers, self.journals)
                 for k, r in group.items()]
        return sorted(dated)

    def live_starting(self) -> list[StartingBalance]:
        return [sb for key, sb in self.starting.items() if not self.is_blocked(key)]

    def starting_stock(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Quantity and base value per item the business started with."""
        held: dict[str, tuple[Decimal, Decimal]] = {}
        for sb in self.live_starting():
            if sb.stock:
                qty, value = held.get(sb.target, (Decimal(0), Decimal(0)))
                held[sb.target] = (qty + sb.stock[0], value + sb.stock[1])
        return held

    def starting_paid(self) -> dict[str, Decimal]:
        """What each invoice or bill had received or paid before the books began."""
        paid: dict[str, Decimal] = {}
        for sb in self.live_starting():
            if sb.paid:
                paid[sb.target] = paid.get(sb.target, Decimal(0)) + sb.paid
        return paid


# ── Field extraction, one function per carried type ────────────────────────────

def _require_date(msg: Message, number: int) -> date:
    value = msg.date(number)
    if value is None:
        raise Blocked("missing date", "The record has no date.")
    return value


def _unsupported(msg: Message, numbers: tuple[int, ...], what: str) -> None:
    for number in numbers:
        if msg.values(number):
            raise Blocked("unsupported feature", f"Uses {what}, which Celerp cannot reproduce yet.")


def _ref(value) -> str | None:
    return None if value is None else str(value)


# Line fields that link to source features Celerp does not carry.
_SALES_LINE_LINKS = (9, 10, 13, 14, 15)
_PURCHASE_LINE_LINKS = (5, 6, 11)
_RECEIPT_LINE_LINKS = (5, 6, 19, 23, 27, 28, 38)
_PAYMENT_LINE_LINKS = (5, 6, 19, 23, 27, 28, 39)
_JOURNAL_LINE_LINKS = (5, 6, 15, 20, 21, 22, 23, 24, 30)
# Line fields naming the account under a control account a line posts to, and the capital
# sub-account field.
_JOURNAL_HOLDERS = ({11: "CapitalAccount", 4: "Employee", 8: "SpecialAccount", 9: "FixedAsset",
                     10: "IntangibleAsset", 18: "ExpenseClaimsPayer", 26: "Investment"}, 17)
_SETTLEMENT_HOLDERS = ({10: "CapitalAccount", 9: "Employee", 12: "SpecialAccount", 13: "FixedAsset",
                        14: "IntangibleAsset", 22: "ExpenseClaimsPayer", 26: "Investment"}, 11)


def _holder(m: Message, layout: tuple[dict[int, str], int]) -> Holder | None:
    holders, sub_field = layout
    named = [(type_name, str(m.guid(n))) for n, type_name in holders.items() if m.guid(n) is not None]
    sub = _ref(m.guid(sub_field))
    if len(named) > 1:
        raise Blocked("unsupported feature", "A line names more than one account under a control account.")
    if sub and (not named or named[0][0] != "CapitalAccount"):
        raise Blocked("unknown reference", "A line names a sub-account without its capital account.")
    return (*named[0], sub) if named else None


def _doc_line(m: Message, links: tuple[int, ...]) -> Line:
    _unsupported(m, links, "a line link")
    return Line(
        account=_ref(m.guid(2)), item=_ref(m.guid(1)), description=m.str(17),
        quantity=m.decimal(18) if m.values(18) else Decimal(1), unit_price=_money(m.decimal(19)),
        discount=Decimal(0), tax_code=_ref(m.guid(21)),
    )


def _order_line(m: Message) -> Line:
    return Line(account=None, item=_ref(m.guid(1)), description=m.str(2),
                quantity=m.decimal(3) if m.values(3) else Decimal(1), unit_price=_money(m.decimal(4)),
                discount=Decimal(0), tax_code=_ref(m.guid(7)))


def _line_discounts(msg: Message, lines: list[Message], parsed: list[Line], flag: int, kind: int,
                    percent: int = 23, amount: int = 24) -> None:
    if not msg.bool(flag):
        return
    exact = msg.int(kind, 0) == 1
    for raw, line in zip(lines, parsed):
        if exact:
            line.discount = _money(raw.decimal(amount))
        else:
            line.discount_percent = _money(raw.decimal(percent)) or None


def _due(msg: Message, issue: date, kind: int, days: int, on: int) -> date | None:
    if msg.int(kind, 0) == 1:
        return msg.date(on)
    count = msg.int(days)
    return issue + timedelta(days=count) if count else None


def _rate(m: Message, number: int, inverse: int) -> Decimal | None:
    """The record's own exchange rate as base units per foreign unit, or None when it stores
    none. Manager can store the rate inverted; 1/x restores the direction."""
    value = m.decimal(number)
    if value is None or value == 0:
        return None
    try:
        return checked_exchange_rate(Decimal(1) / value if m.bool(inverse) else value)
    except ValueError as exc:
        raise Blocked("invalid exchange rate", f"The record's exchange rate {exc}.") from exc


def _document(source_type: str, key: str, m: Message) -> Document:
    if source_type == "SalesInvoice":
        _unsupported(m, (29, 28, 35), "invoice rounding, withholding tax or an early payment discount")
        raw = m.messages(49)
        lines = [_doc_line(x, _SALES_LINE_LINKS) for x in raw]
        _line_discounts(m, raw, lines, 31, 32)
        issue = _require_date(m, 1)
        return Document(key, source_type, issue, _due(m, issue, 54, 22, 6), m.str(2), _ref(m.guid(3)), None,
                        m.bool(8), m.str(12), lines, moves_stock=m.bool(69), location=_ref(m.guid(30)),
                        rate=_rate(m, 64, 65), billing_address=m.str(4), names=_names(m, 50, 51))
    if source_type == "PurchaseInvoice":
        _unsupported(m, (24, 65, 68), "withholding tax, freight or landed costs")
        raw = m.messages(23)
        lines = [_doc_line(x, _PURCHASE_LINE_LINKS) for x in raw]
        _line_discounts(m, raw, lines, 14, 15)
        issue = _require_date(m, 1)
        return Document(key, source_type, issue, _due(m, issue, 31, 19, 5), m.str(2), _ref(m.guid(3)), None,
                        m.bool(7), m.str(9), lines, moves_stock=m.bool(64), location=_ref(m.guid(13)),
                        rate=_rate(m, 61, 62), names=_names(m, 29, 28))
    if source_type == "CreditNote":
        _unsupported(m, (13,), "withholding tax")
        raw = m.messages(22)
        lines = [_doc_line(x, _SALES_LINE_LINKS) for x in raw]
        _line_discounts(m, raw, lines, 15, 16)
        return Document(key, source_type, _require_date(m, 1), None, m.str(2), _ref(m.guid(3)), None,
                        m.bool(6), m.str(9), lines, _ref(m.guid(8)), rate=_rate(m, 30, 31),
                        billing_address=m.str(4))
    raw = m.messages(16)                                            # DebitNote
    lines = [_doc_line(x, _PURCHASE_LINE_LINKS) for x in raw]
    _line_discounts(m, raw, lines, 10, 11)
    return Document(key, source_type, _require_date(m, 1), None, m.str(2), _ref(m.guid(3)), None,
                    m.bool(5), m.str(7), lines, _ref(m.guid(6)), rate=_rate(m, 22, 23))


def _names(m: Message, *numbers: int) -> tuple[str, ...]:
    return tuple(ref for ref in (_ref(m.guid(n)) for n in numbers) if ref)


# Per quote and order type: (lines, include tax, description, cancelled, discount flag and
# kind, the quote or order it came from, billing address, unsupported fields).
_ORDER_LAYOUT = {
    "SalesQuote": (24, 8, 9, 25, 17, 18, None, 4, (13, 16, 28, 29, 30, 31)),
    "SalesOrder": (18, 9, 12, 21, 13, 14, 19, 11, (23, 24, 25, 26)),
    "PurchaseQuote": (17, 9, 11, 19, 12, 13, None, None, (25, 26, 27, 28)),
    "PurchaseOrder": (17, 9, 11, 20, 12, 13, 18, None, (22, 23, 24, 25)),
}


def _order(source_type: str, key: str, m: Message) -> Document:
    lines_at, tax_at, text_at, cancelled_at, flag, kind, came_from, address_at, unsupported = _ORDER_LAYOUT[source_type]
    _unsupported(m, unsupported, "rounding or withholding tax")
    raw = m.messages(lines_at)
    lines = [_order_line(x) for x in raw]
    _line_discounts(m, raw, lines, flag, kind, 5, 6)
    issue = _require_date(m, 1)
    days = m.int(26, 0) if source_type == "SalesQuote" else 0
    return Document(key, source_type, issue, None, m.str(2), _ref(m.guid(3)), None, m.bool(tax_at), m.str(text_at),
                    lines, billing_address=m.str(address_at) if address_at else None,
                    valid_until=issue + timedelta(days=days) if days else None, cancelled=m.bool(cancelled_at),
                    names=_names(m, came_from) if came_from else ())


def _movement(source_type: str, key: str, m: Message) -> Movement:
    """A goods receipt or delivery note: fields are laid out alike but for the document link."""
    sign = 1 if source_type in INBOUND else -1
    lines = []
    for raw in m.messages(15):
        item = _ref(raw.guid(1))
        if item is None:
            raise Blocked("unknown reference", "A line names no inventory item.")
        quantity = raw.decimal(3) if raw.values(3) else Decimal(1)
        if quantity <= 0:
            raise Blocked("unsupported feature", "A zero or negative quantity.")
        lines.append(MoveLine(item, sign * quantity))
    document = _ref(m.guid(19 if source_type == "DeliveryNote" else 17))
    return Movement(key, source_type, _require_date(m, 3), m.str(1), document, _ref(m.guid(11)), lines,
                    tuple((line.item, line.quantity) for line in lines))


def _settlement(source_type: str, key: str, m: Message) -> Settlement:
    receipt = source_type == "Receipt"
    if receipt:
        bank, qty_col, price_col, exclusive, rate = m.guid(7), 32, 33, 31, _rate(m, 45, 46)
        _unsupported(m, (34, 35, 38, 39), "a discount or a fixed total")
        links, party_account, contact_field, doc_field = _RECEIPT_LINE_LINKS, AR, 3, 4
        other_party = (AP, 7, 8)
    else:
        bank, qty_col, price_col, exclusive, rate = m.guid(7), 33, 34, 32, _rate(m, 46, 47)
        _unsupported(m, (35, 36, 39, 40), "a discount or a fixed total")
        links, party_account, contact_field, doc_field = _PAYMENT_LINE_LINKS, AP, 7, 8
        other_party = (AR, 3, 4)
    if bank is None:
        raise Blocked("unknown reference", "The record names no bank or cash account.")
    use_price = m.bool(qty_col) and m.bool(price_col)
    party: list[Line] = []
    other: list[Line] = []
    for raw in m.messages(11):
        _unsupported(raw, links, "a line link")
        if raw.values(1):
            raise Blocked("inventory item line", "An inventory item sold or bought on a receipt or payment.")
        account = _ref(raw.guid(2))
        if use_price:
            amount = _money(raw.decimal(17)) * _money(raw.decimal(25))
        else:
            amount = _money(raw.decimal(18))
        line = Line(account=account, item=None, description=raw.str(15), quantity=Decimal(1), unit_price=amount,
                    discount=Decimal(0), tax_code=_ref(raw.guid(20)), net=amount,
                    holder=_holder(raw, _SETTLEMENT_HOLDERS))
        if account == party_account:
            line.contact, line.document = _ref(raw.guid(contact_field)), _ref(raw.guid(doc_field))
            if raw.values(20):
                raise Blocked("unsupported feature", "A tax code on a customer or supplier balance line.")
            (party if line.document else other).append(line)
            continue
        if account == other_party[0]:
            if raw.values(other_party[2]):
                raise Blocked("unsupported feature", "Settles a document of the other ledger.")
            line.contact = _ref(raw.guid(other_party[1]))
        other.append(line)
    contacts = {ln.contact for ln in party}
    if len(contacts) > 1:
        raise Blocked("several contacts", "One settlement for several customers or suppliers.")
    return Settlement(key, source_type, _require_date(m, 1), m.str(2), str(bank), None, m.str(10),
                      party, other, not m.bool(exclusive), rate, m.str(6),
                      tuple(str(c) for c in (m.guid(4), m.guid(5)) if c is not None))


def _journal(key: str, m: Message) -> Journal:
    lines = []
    for raw in m.messages(14):
        _unsupported(raw, _JOURNAL_LINE_LINKS, "an invoice, item, tax or foreign amount link")
        account = _ref(raw.guid(1))
        contact = None
        if account == CASH_AT_BANK:
            account = _ref(raw.guid(29))
        elif account == AR:
            contact = _ref(raw.guid(2))
        elif account == AP:
            contact = _ref(raw.guid(3))
        if account is None:
            raise Blocked("unknown reference", "A journal line names no account.")
        debit, credit = _money(raw.decimal(13)), _money(raw.decimal(14))
        if debit or credit:
            lines.append(JournalLine(account, contact, debit, credit, raw.str(12),
                                     holder=_holder(raw, _JOURNAL_HOLDERS)))
    debits = sum((ln.debit for ln in lines), Decimal(0))
    credits = sum((ln.credit for ln in lines), Decimal(0))
    if len(lines) < 2 or debits != credits or any(ln.debit and ln.credit for ln in lines):
        raise Blocked("unbalanced", "The journal entry does not balance.")
    return Journal(key, _require_date(m, 1), m.str(2), m.str(3), _ref(m.guid(8)), lines)


# Records Manager posts through its holders and built-in accounts, each carried as a journal of
# its own. They name other records, so each is built once every object is read.
ACTIVITY = frozenset({"DepreciationEntry", "AmortizationEntry", "Payslip", "ExpenseClaim", "LatePaymentFee",
                      "WithholdingTaxReceipt", "BillableTime"})
# The asset type each write-off record writes off.
WRITE_OFFS = {"DepreciationEntry": "FixedAsset", "AmortizationEntry": "IntangibleAsset"}
# Payslip items and the fields naming the accounts each posts to.
PAYSLIP_ITEMS = {"PayslipEarningsItem": (3,), "PayslipDeductionItem": (3,), "PayslipContributionItem": (3, 4)}


def _posting(account: str | None, amount: Decimal, contact: str | None = None,
             description: str | None = None) -> JournalLine:
    """A journal line from a debit positive amount."""
    return JournalLine(account, contact, max(amount, Decimal(0)), max(-amount, Decimal(0)), description)


def _named(book: Book, key: str | None, type_name: str, what: str) -> str:
    """The account under a control account a record names, which must be of the given type."""
    account = book.accounts.get(key) if key else None
    _check(account is not None and account.source_type == type_name and not book.is_blocked(key), what)
    return key


def _customer(book: Book, key: str | None) -> str:
    contact = book.contacts.get(key) if key else None
    _check(contact is not None and contact.source_type == "Customer", "a customer")
    return key


def _payslip_item(book: Book, key: str | None, type_name: str) -> tuple[str, ...]:
    item = book.payslip_items.get(str(key)) if key else None
    _check(item is not None and item[0] == type_name and not book.is_blocked(str(key)), "a payslip item")
    if None in item[1]:
        raise Blocked("unknown reference", "A payslip item names no account to post to.")
    return item[1]


def _activity_journals(book: Book, name: str, key: str, m: Message) -> list[Journal]:
    """The journals an activity record posts, in the base currency. Billable time posts a second
    one, reversing it, when it is invoiced or written off."""
    code = book.base_code or ""
    if name in WRITE_OFFS:
        asset_type = WRITE_OFFS[name]
        lines = []
        for raw in m.messages(3):
            asset = _named(book, _ref(raw.guid(1)), asset_type, "an asset")
            amount = _money(raw.decimal(2))
            lines += [_posting(book.assets[asset].expense, amount),
                      _posting(asset + WRITTEN_OFF[asset_type][0], -amount)]
        return [Journal(key, _require_date(m, 1), m.str(4), m.str(2), None, lines)]
    if name == "Payslip":
        employee = _named(book, _ref(m.guid(2)), "Employee", "an employee")
        lines, net = [], Decimal(0)
        for raw in m.messages(3):
            (account,) = _payslip_item(book, raw.guid(4), "PayslipEarningsItem")
            units = raw.decimal(7) if raw.values(7) else Decimal(1)
            amount = round_money(_money(raw.decimal(3)) * units, code)
            lines.append(_posting(account, amount, description=raw.str(6)))
            net += amount
        for raw in m.messages(4):
            (account,) = _payslip_item(book, raw.guid(3), "PayslipDeductionItem")
            amount = _money(raw.decimal(2))
            lines.append(_posting(account, -amount, description=raw.str(4)))
            net -= amount
        for raw in m.messages(5):
            expense, liability = _payslip_item(book, raw.guid(3), "PayslipContributionItem")
            amount = _money(raw.decimal(2))
            lines += [_posting(expense, amount, description=raw.str(5)),
                      _posting(liability, -amount, description=raw.str(5))]
        lines.append(_posting(employee, -net))
        return [Journal(key, _require_date(m, 1), m.str(12), m.str(11), None, lines)]
    if name == "ExpenseClaim":
        payer = _named(book, _ref(m.guid(9)), "ExpenseClaimsPayer", "an expense claims payer")
        lines = [_posting(_ref(raw.guid(2)), round_money((raw.decimal(18) if raw.values(18) else Decimal(1))
                                                         * _money(raw.decimal(19)), code), description=raw.str(17))
                 for raw in m.messages(18)]
        lines.append(_posting(payer, -sum((ln.debit - ln.credit for ln in lines), Decimal(0))))
        return [Journal(key, _require_date(m, 1), m.str(5), m.str(3), None, lines)]
    if name == "LatePaymentFee":
        amount = _money(m.decimal(4))
        lines = [_posting(AR, amount, _customer(book, _ref(m.guid(2)))), _posting(LATE_FEES, -amount)]
        return [Journal(key, _require_date(m, 1), None, None, None, lines)]
    if name == "WithholdingTaxReceipt":
        amount, description = _money(m.decimal(3)), m.str(4)
        lines = [_posting(WITHHOLDING_TAX, amount, description=description),
                 _posting(AR, -amount, _customer(book, _ref(m.guid(1))))]
        return [Journal(key, _require_date(m, 2), None, description, None, lines)]
    # BillableTime: the hours at the hourly rate, held until invoiced (status 1) or written off (2).
    amount = round_money(_money(m.decimal(8)) * (Decimal(m.int(6, 0)) + Decimal(m.int(7, 0)) / 60), code)
    description, status = m.str(3), m.int(14, 0)

    def held(sign: int) -> list[JournalLine]:
        return [_posting(BILLABLE_TIME, sign * amount), _posting(BILLABLE_TIME_MOVEMENT, -sign * amount)]

    out = [Journal(key, _require_date(m, 1), None, description, None, held(1))]
    if status == 1:
        invoice = book.documents.get(_ref(m.guid(13)) or "")
        _check(invoice is not None and invoice.source_type == "SalesInvoice", "the invoice it is billed on")
        out.append(Journal(f"{key}:reversal", invoice.date, None, description, None, held(-1)))
    elif status == 2:
        out.append(Journal(f"{key}:reversal", _require_date(m, 15), None, description, None, held(-1)))
    elif status:
        raise Blocked("unsupported feature", "A billable time status Celerp does not recognise.")
    return out


def _activity(book: Book) -> None:
    """Build every activity record's journals, adding the built-in accounts they post to."""
    for key, m in book.activity.items():
        name = book.names[key]
        try:
            journals = _activity_journals(book, name, key, m)
        except Blocked as blocked:
            book.block(name, key, blocked.reason, blocked.note)
            continue
        for journal in journals:
            journal.lines = [ln for ln in journal.lines if ln.debit or ln.credit]
            for line in journal.lines:
                _add_builtin(book, line.account)
            book.journals[journal.key] = journal
            book.names[journal.key] = name


def _add_builtin(book: Book, key: str | None) -> None:
    if key in ACTIVITY_BUILTINS and key not in book.accounts:
        account_type, label = ACTIVITY_BUILTINS[key]
        book.accounts[key] = Account(key, TYPE_NAMES[key], label, account_type=account_type)


def _decode_object(book: Book, name: str, key: str, m: Message) -> None:
    """Store one decoded object. Raises Blocked or DecodeError for this object alone."""
    if name == "BusinessDetails":
        book.company_key, book.company_name, book.address = key, m.str(1), m.str(2)
    elif name == "BaseCurrency":
        book.base_key, book.base_name, book.base_code = key, m.str(2), m.str(3)
        book.precision = m.int(5, None) if m.values(5) else currency_dp(book.base_code or "")
    elif name == "ForeignCurrency":
        code = m.str(2)
        if not code or len(code) != 3:
            raise Blocked("unsupported feature", "The currency has no three-letter code.")
        book.currencies[key] = Currency(key, code, m.str(1), m.int(4, None) if m.values(4) else currency_dp(code),
                                        m.bool(6))
    elif name == "ExchangeRate":
        effective, currency, value = m.date(1), _ref(m.guid(2)), m.decimal(6)
        if effective is None or currency is None:
            raise Blocked("unknown reference", "The exchange rate names no date or currency.")
        try:
            rate = checked_exchange_rate(value)
        except ValueError as exc:
            raise Blocked("invalid exchange rate", f"The exchange rate {exc}.") from exc
        book.rates.append(Rate(key, effective, currency, rate))
    elif name in ("Assets", "Liabilities", "Equity"):
        book.groups[key] = Group(key, name, None)
    elif name == "BalanceSheetGroup":
        book.groups[key] = Group(key, name, _ref(m.guid(3)))
    elif name == "ProfitAndLossStatementGroup":
        book.groups[key] = Group(key, name, _ref(m.guid(3)), m.int(6, 0))
    elif name == "BalanceSheetAccount":
        book.accounts[key] = Account(key, name, m.str(1) or "", m.str(17), _ref(m.guid(3)), inactive=m.bool(19))
    elif name == "ProfitAndLossStatementAccount":
        book.accounts[key] = Account(key, name, m.str(1) or "", m.str(11), _ref(m.guid(3)), inactive=m.bool(12))
    elif name == "BankOrCashAccount":
        book.accounts[key] = Account(key, name, m.str(1) or "", m.str(13), None, "asset", "bank",
                                     _ref(m.guid(3)), m.bool(10), _ref(m.guid(12)), CASH_AT_BANK)
    elif key in BUILTINS or key in INVENTORY_BUILTINS:
        account_type, control, label = BUILTINS.get(key) or INVENTORY_BUILTINS[key]
        book.accounts[key] = Account(key, name, m.str(1) or label, None, None, account_type, control)
    elif key in ACTIVITY_BUILTINS:
        account_type, label = ACTIVITY_BUILTINS[key]
        code = m.str(12) if name.startswith("BalanceSheet") else None
        book.accounts[key] = Account(key, name, m.str(1) or label, code, None, account_type)
    elif key in HOLDER_BUILTINS:
        account_type, label = HOLDER_BUILTINS[key]
        code = m.str(2) if key == INVESTMENTS else m.str(12)
        group = _ref(m.guid(3)) if key == SPECIAL else None
        book.accounts[key] = Account(key, name, m.str(1) or label, code, group, None if group else account_type)
    elif name in CONTROL_KINDS:
        book.accounts[key] = Account(key, name, m.str(1) or "", m.str(17), _ref(m.guid(3)),
                                     inactive=m.bool(_CONTROL_INACTIVE.get(name, 18)), kind=CONTROL_KINDS[name])
    elif name in MERGED_CONTROLS:
        book.merged_controls[key] = name
    elif name in HOLDERS:
        _decode_holder(book, name, key, m)
    elif name == "SubAccount":
        book.sub_accounts[key] = m.str(1) or ""
    elif name in STARTING_TYPES:
        book.starting[key] = _starting_balance(name, key, m)
    elif name == "TaxCode":
        if m.int(5, 0) == 1 or m.int(6, 0) == 1 or m.bool(11):
            raise Blocked("unsupported feature", "A total-rate, multiple-rate or reverse-charge tax code.")
        rate = _money(m.decimal(4)) if m.int(5, 0) == 2 else Decimal(0)
        book.tax_codes[key] = TaxCode(key, m.str(1) or "", rate, _ref(m.guid(7)) or TAX, m.bool(10))
    elif name == "Customer":
        book.contacts[key] = Contact(key, name, m.str(1) or "", m.str(13), _ref(m.guid(14)), m.str(3), m.str(2),
                                     m.bool(15), m.str(4), _ref(m.guid(19)))
        _custom_fields(book, name, key, m.messages(12), m.str(4))
    elif name == "Supplier":
        book.contacts[key] = Contact(key, name, m.str(1) or "", m.str(10), _ref(m.guid(11)), m.str(2), m.str(7),
                                     m.bool(12), m.str(3), _ref(m.guid(16)))
        _custom_fields(book, name, key, m.messages(9), m.str(3))
    elif name == "InventoryItem":
        code, item_name = m.str(1), m.str(11)
        method = m.int(38, 0)
        if method not in VALUATION_METHODS:
            raise Blocked("unsupported feature", "Uses the manual inventory valuation method, which Celerp cannot "
                                                 "reproduce yet. Choose first in, first out or average cost for the "
                                                 "item in Manager and upload the file again.")
        book.items[key] = Item(key, code, item_name or code or "", m.str(13),
                               m.decimal(3) if m.bool(32) else None, m.decimal(2) if m.bool(31) else None, m.bool(10),
                               VALUATION_METHODS[method])
    elif name in DOC_TYPES:
        book.documents[key] = _document(name, key, m)
    elif name in ORDER_TYPES:
        book.orders[key] = _order(name, key, m)
    elif name in MOVEMENT_OF:
        book.movements[key] = _movement(name, key, m)
    elif name == "InventoryUnitCost":
        item, cost = _ref(m.guid(2)), m.decimal(3)
        if item is None or cost is None:
            raise Blocked("unknown reference", "The unit cost names no inventory item or cost.")
        book.unit_costs.setdefault(item, []).append((_require_date(m, 1), cost))
    elif name in ("Receipt", "Payment"):
        book.settlements[key] = _settlement(name, key, m)
    elif name == "InterAccountTransfer":
        source, dest = _ref(m.guid(2)), _ref(m.guid(3))
        credit, debit = _money(m.decimal(8)), _money(m.decimal(9))
        if source is None or dest is None:
            raise Blocked("unknown reference", "The transfer names no source or destination account.")
        if credit <= 0 or debit <= 0:
            raise Blocked("unsupported feature", "A zero or negative transfer amount.")
        book.transfers[key] = Transfer(key, _require_date(m, 1), m.str(6), m.str(5), source, dest, credit, debit)
    elif name == "JournalEntry":
        book.journals[key] = _journal(key, m)
    elif name in ACTIVITY:
        book.activity[key] = m
    elif name in PAYSLIP_ITEMS:
        book.payslip_items[key] = (name, tuple(_ref(m.guid(n)) for n in PAYSLIP_ITEMS[name]))
    elif name == "InvestmentMarketPrice":
        investment, price = _ref(m.guid(2)), m.decimal(6)
        if investment is None or price is None:
            raise Blocked("unknown reference", "The market price names no investment or price.")
        book.prices[key] = MarketPrice(key, _require_date(m, 1), investment, price)
    elif name == "Attachment":
        book.attachments[key] = AttachmentRef(key, m.str(2) or "", m.int(4, 0), _ref(m.guid(6)), m.bytes(12))
    elif name == "LockDate":
        _lock_date(book, key, m)


# Manager's inventory valuation methods by stored value: first in, first out (the default,
# stored as nothing) and weighted average cost. Manual costing is not reproduced.
VALUATION_METHODS = {0: False, 1: True}


def _custom_fields(book: Book, name: str, key: str, values: list[Message], phone: str | None) -> None:
    """Custom field values have no Celerp equivalent. Manager's upgrade also copies a contact's
    old telephone into one; that value is carried as the phone, so only the others are lost."""
    if any(v.str(2) and v.str(2) != phone for v in values):
        book.losses[(key, f"{name} (custom fields)")] = ("Custom field values are not carried. Celerp has no "
                                                        "custom fields for customers and suppliers.")


LOCK_OFF_NOTE = "Locking is switched off in Manager, so no lock date is installed."


def _lock_date(book: Book, key: str, m: Message) -> None:
    """Manager's one LockDate object: field 1 the date through which periods are locked,
    field 2 whether locking is switched on. A date left behind with locking off locks
    nothing in Manager, so none is installed. A second LockDate object, or locking on with
    no date, cannot say which lock the user set, so the file is refused rather than a lock
    dropped or guessed."""
    locked, through = m.bool(2), m.date(1)
    if book.object_counts.get("LockDate", 0) > 1:
        raise Blocked("unreadable", "The file holds more than one lock date.")
    if not locked:
        book.verdicts[key] = Verdict("LockDate", CoverageClass.IGNORED_NON_BUSINESS, None, LOCK_OFF_NOTE)
        return
    if through is None:
        raise Blocked("unreadable", "Locking is switched on but no lock date is set.")
    book.lock_date = through


# Accounts Manager keeps a balance for under a control account, per type: (field naming a
# custom control account, the built-in one otherwise, code field, currency field, inactive
# field). Each becomes an account under its control account.
HOLDERS: dict[str, tuple[int | None, str, int | None, int | None, int | None]] = {
    "CapitalAccount": (10, CAPITAL, 11, None, 8),
    "SpecialAccount": (7, SPECIAL, 11, 5, 10),
    "Employee": (15, EMPLOYEES, 14, 7, 9),
    "ExpenseClaimsPayer": (None, EXPENSE_CLAIMS, None, None, 6),
    "FixedAsset": (14, FA_AT_COST, 10, None, None),
    "IntangibleAsset": (12, IA_AT_COST, 10, None, None),
    "Investment": (7, INVESTMENTS, 1, None, 3),
}
# A fixed or intangible asset's posting terms: (field marking a custom depreciation or
# amortization account, the built-in one otherwise, field naming a custom disposal account,
# the built-in one otherwise). The custom account itself is in the field after its flag. Field
# 6 marks the asset disposed of and field 7 holds the disposal date.
ASSET_TERMS = {
    "FixedAsset": (20, FA_DEPRECIATION, 19, FA_DISPOSAL),
    "IntangibleAsset": (18, IA_AMORTIZATION, 17, IA_DISPOSAL),
}
# A fixed or intangible asset's second account, for what has been written off it: (key
# suffix, name suffix, field naming a custom control account, the built-in one otherwise).
WRITTEN_OFF = {
    "FixedAsset": (":depreciation", " - accumulated depreciation", 18, FA_ACCUM),
    "IntangibleAsset": (":amortization", " - accumulated amortization", 16, IA_ACCUM),
}


def _decode_holder(book: Book, name: str, key: str, m: Message) -> None:
    parent_at, builtin, code_at, currency_at, inactive_at = HOLDERS[name]
    if name in ASSET_TERMS:
        flag_at, expense, disposal_at, disposal = ASSET_TERMS[name]
        book.assets[key] = Asset((_ref(m.guid(flag_at + 1)) if m.bool(flag_at) else None) or expense,
                                 _ref(m.guid(disposal_at)) or disposal,
                                 _require_date(m, 7) if m.bool(6) else None)
    if name == "SpecialAccount" and m.values(6):
        raise Blocked("unsupported feature", "A special account with a tax code, which Celerp cannot reproduce yet.")
    label = m.str(2) if name == "Investment" else m.str(1)
    account = Account(key, name, label or "", m.str(code_at) if code_at else None,
                      currency=_ref(m.guid(currency_at)) if currency_at else None,
                      inactive=m.bool(inactive_at) if inactive_at else False,
                      parent=(_ref(m.guid(parent_at)) if parent_at else None) or builtin, kind=builtin)
    book.accounts[key] = account
    if name in WRITTEN_OFF:
        suffix, title, parent_at, builtin = WRITTEN_OFF[name]
        book.accounts[key + suffix] = Account(key + suffix, name, account.name + title, inactive=account.inactive,
                                              parent=_ref(m.guid(parent_at)) or builtin, kind=builtin)


# Starting balance types and the type of record each is the starting balance of.
STARTING_TYPES = {
    "BalanceSheetAccountStartingBalance": "BalanceSheetAccount", "BankOrCashAccountStartingBalance": "BankOrCashAccount",
    "CapitalAccountStartingBalance": "CapitalAccount", "SpecialAccountStartingBalance": "SpecialAccount",
    "EmployeeStartingBalance": "Employee", "FixedAssetStartingBalance": "FixedAsset",
    "IntangibleAssetStartingBalance": "IntangibleAsset", "InventoryItemStartingBalance": "InventoryItem",
    "InvestmentStartingBalance": "Investment", "SalesInvoiceStartingBalance": "SalesInvoice",
    "PurchaseInvoiceStartingBalance": "PurchaseInvoice",
}
# Starting balances stated as an amount and whether it is a credit.
_SIDED_STARTING = frozenset({"BalanceSheetAccountStartingBalance", "CapitalAccountStartingBalance",
                             "SpecialAccountStartingBalance", "EmployeeStartingBalance"})
FOREIGN_START_NOTE = ("A starting balance in a foreign currency. Manager keeps no exchange rate for it, so its base "
                      "currency amount is not known.")


def _starting_balance(name: str, key: str, m: Message) -> StartingBalance:
    target = _ref(m.guid(1))
    if target is None:
        raise Blocked("unknown reference", "The starting balance names no account, item or invoice.")
    if name in _SIDED_STARTING:
        amount = _money(m.decimal(3))
        return StartingBalance(key, name, target, (-amount if m.int(2, 0) == 1 else amount,))
    if STARTING_TYPES[name] in WRITTEN_OFF:
        return StartingBalance(key, name, target, (_money(m.decimal(2)), -_money(m.decimal(3))))
    if name == "InvestmentStartingBalance":                         # units and the price paid for each
        units = _money(m.decimal(2))
        return StartingBalance(key, name, target, (units * _money(m.decimal(3)),), units)
    if name != "InventoryItemStartingBalance":
        return StartingBalance(key, name, target, (_money(m.decimal(2)),))
    _unsupported(m, (4, 5, 6, 7), "a starting stock value other than an average cost")
    lines = m.messages(3)
    if any(_ref(line.guid(1)) not in (None, DEFAULT_LOCATION) for line in lines):
        raise Blocked("multiple locations", LOCATION_NOTE)
    quantity = sum((_money(line.decimal(2)) for line in lines), Decimal(0))
    if not m.bool(2) or quantity <= 0:
        raise Blocked("unsupported feature", "A starting stock with no quantity, which Celerp cannot reproduce yet.")
    return StartingBalance(key, name, target, (_money(m.decimal(8)),), quantity)


# ── Resolution: references, account types, figures, currency ──────────────────

def _account_type(book: Book, account: Account) -> str | None:
    if account.account_type:
        return account.account_type
    if account.parent:                                              # the type of the control account it is under
        parent = book.accounts.get(account.parent)
        return _account_type(book, parent) if parent is not None and parent.parent is None else None
    node = account.group
    for _ in range(32):                                             # bounded: a cyclic group chain fails
        if node in ROOT_TYPES:
            return ROOT_TYPES[node]
        group = book.groups.get(node) if node else None
        if group is None:
            return None
        if group.source_type == "ProfitAndLossStatementGroup" and group.pl_type in (0, 1):
            return "revenue" if group.pl_type == 0 else "expense"
        if group.source_type in ("Assets", "Liabilities", "Equity"):
            return ROOT_TYPES[group.key]
        node = group.parent
    return None


def _check(condition: bool, what: str) -> None:
    if not condition:
        raise Blocked("unknown reference", f"Refers to {what} that is not in the file.")


def _controls(book: Book) -> set[str]:
    """Accounts that only report the accounts under them, so no line posts to them directly."""
    return {*HOLDER_BUILTINS, *(a.parent for a in book.accounts.values() if a.parent),
            *(a.key for a in book.accounts.values() if a.source_type in CONTROL_KINDS)}


def _place(book: Book, account: str | None, holder: Holder | None) -> str:
    """The account a line posts to: the one it names, or the account under that control
    account it names. A capital sub-account becomes an account of its own under the capital
    account's control account."""
    if holder is None:
        _check(account in book.accounts, "an account")
        if account in book.controls:
            raise Blocked("control account", "A line posts to a control account without naming the account under "
                                             "it.")
        return account
    type_name, key, sub = holder
    target = book.accounts.get(key)
    _check(target is not None and target.source_type == type_name and not book.is_blocked(key),
           "an account under a control account")
    if sub:
        _check(sub in book.sub_accounts, "a sub-account")
        _check(target.parent == account, "a capital account under the control account the line posts to")
        sub_key = f"{key}:{sub}"
        book.accounts.setdefault(sub_key, Account(
            sub_key, "SubAccount", f"{target.name} - {book.sub_accounts[sub]}", account_type=target.account_type,
            currency=target.currency, parent=target.parent, kind=target.kind))
        target = book.accounts[sub_key]
    for candidate in (target.key, *(target.key + suffix for suffix, *_ in WRITTEN_OFF.values())):
        if candidate in book.accounts and book.accounts[candidate].parent == account:
            return candidate
    raise Blocked("unknown reference", "Names an account under a control account other than the one the line "
                                       "posts to.")


def _price_lines(book: Book, currency: str | None, lines: list[Line], include_tax: bool) -> None:
    """Net, discount and tax per line, rounded half up to the record currency's precision."""
    code = book.currency_code(currency)
    for line in lines:
        gross = round_money(line.quantity * line.unit_price, code) if line.net == 0 else line.net
        if line.discount_percent is not None:
            line.discount = round_money(gross * line.discount_percent / 100, code)
        amount = gross - line.discount
        rate = book.tax_codes[line.tax_code].rate if line.tax_code else Decimal(0)
        if include_tax:
            line.tax = round_money(amount * rate / (100 + rate), code)
            line.net = amount - line.tax
        else:
            line.tax = round_money(amount * rate / 100, code)
            line.net = amount


def _resolve_document(book: Book, doc: Document) -> None:
    contact = book.contacts.get(doc.contact) if doc.contact else None
    wanted = "Customer" if doc.source_type in SALES_TYPES else "Supplier"
    _check(contact is not None and contact.source_type == wanted, f"a {wanted.lower()}")
    doc.currency = contact.currency
    for line in doc.lines:
        if line.item:
            _check(line.item in book.items, "an inventory item")
            if doc.source_type not in ("PurchaseInvoice", "SalesInvoice"):
                raise Blocked("inventory item line", "Inventory items on this record type are not carried yet.")
        else:
            _place(book, line.account, None)
        if line.tax_code:
            _check(line.tax_code in book.tax_codes, "a tax code")
    if doc.source_type in JOURNAL_DOCUMENTS and not doc.applies_to:
        raise Blocked("not applied", "A debit note is carried only as applied to the bill it notes.")
    if doc.applies_to:
        target = book.documents.get(doc.applies_to)
        _check(target is not None and target.source_type == NOTE_OF[doc.source_type], "the invoice it settles")
    _price_lines(book, doc.currency, doc.lines, doc.include_tax)
    if doc.moves_stock and doc.location not in (None, DEFAULT_LOCATION):
        raise Blocked("multiple locations", LOCATION_NOTE)


def _resolve_order(book: Book, order: Document) -> None:
    contact = book.contacts.get(order.contact) if order.contact else None
    wanted = "Customer" if order.source_type in SALES_TYPES else "Supplier"
    _check(contact is not None and contact.source_type == wanted, f"a {wanted.lower()}")
    if book.is_blocked(contact.key):
        raise Blocked("blocked contact", f"Its {wanted.lower()} cannot be migrated.")
    order.currency = contact.currency
    for line in order.lines:
        if line.item:
            _check(line.item in book.items and not book.is_blocked(line.item), "an inventory item")
        if line.tax_code:
            _check(line.tax_code in book.tax_codes and not book.is_blocked(line.tax_code), "a tax code")
    _price_lines(book, order.currency, order.lines, order.include_tax)
    order.rate = _record_rate(book, order.currency, None, order.date)


def _link_orders(book: Book) -> None:
    """Each carried record names the carried quote or order it came from, nearest first, and
    each quote or order names what it became: the order made from it, else its earliest
    invoice or bill. A cancelled quote or order is not linked."""
    def live(key: str) -> bool:
        return key in book.orders and not book.orders[key].cancelled

    later = sorted(book.documents.values(), key=lambda d: (d.date, d.key))
    for record in [*sorted(book.orders.values(), key=lambda d: (d.date, d.key)), *later]:
        if book.is_blocked(record.key) or (record.key in book.orders and record.cancelled):
            continue
        sources = [key for key in record.names if live(key)]
        record.converted_from = sources[0] if sources else None
        for key in sources:
            if book.orders[key].converted_to is None:
                book.orders[key].converted_to = record.key


def _unit_cost(book: Book, item: str, day: date) -> Decimal | None:
    """The item's latest unit cost on or before the day, or None when none is set."""
    costs = [cost for on, cost in sorted(book.unit_costs.get(item, [])) if on <= day]
    return costs[-1] if costs else None


def _cost_sales(book: Book) -> None:
    """Work out each sales invoice's cost of sales as Manager books it, from what it owns.

    Manager's books own an item from the bill that buys it, not from its goods receipt, and
    an invoice relieves them on its own date, not when the goods leave. Each item line of a
    sales invoice costs the item's unit cost on the invoice date when one is set. Otherwise
    it costs what is owned then, by the item's valuation method: the oldest bills still
    owned first (first in, first out), or the average of everything owned (average cost).
    What is owned is the stock the business started with and every bill bought on or before
    that day, less every earlier sale.
    Documents Celerp cannot carry still count, since Manager's books hold them. An invoice
    selling more of an item than is owned is blocked, even with goods on hand or a unit cost
    set: Manager clears negative inventory with later cost corrections that are not rebuilt.
    Every later sale of that item is blocked too, as its cost depends on those corrections.
    An item that is not carried leaves its documents blocked already and is not costed."""
    code = book.base_code or ""
    trading = sorted((doc for doc in book.documents.values() if doc.source_type in ("PurchaseInvoice", "SalesInvoice")),
                     key=lambda d: (d.date, d.source_type != "PurchaseInvoice", d.key))
    # item -> [quantity, value] per bill still owned, the stock the business started with first
    owned: dict[str, list[list[Decimal]]] = {item: [[qty, value]] for item, (qty, value) in book.starting_stock().items()}
    oversold: set[str] = set()
    for doc in trading:
        for line in (ln for ln in doc.lines if ln.item in book.items):
            layers = owned.setdefault(line.item, [])
            if doc.source_type == "PurchaseInvoice":
                layers.append([line.quantity, line.base_net])
                continue
            qty = sum((q for q, _ in layers), Decimal(0))
            if line.quantity > qty or line.item in oversold:
                oversold.add(line.item)
                if not book.is_blocked(doc.key):
                    book.block(doc.source_type, doc.key, "negative quantity owned", "Sells more of an item than "
                               "the business owned on the invoice date, or sells it after such an invoice. Manager "
                               "settles that through negative inventory clearing. Celerp does not carry that yet.")
            unit = _unit_cost(book, line.item, doc.date)
            if not book.items[line.item].average_cost:
                relieved = _relieve(layers, line.quantity)
                line.cost = round_money(line.quantity * unit if unit is not None else relieved, code)
                continue
            value = sum((v for _, v in layers), Decimal(0))
            if unit is not None:
                line.cost = round_money(line.quantity * unit, code)
            elif qty > 0:
                line.cost = round_money(value * min(line.quantity, qty) / qty, code)
            layers[:] = [[qty - line.quantity, value - line.cost]]


def _relieve(layers: list[list[Decimal]], quantity: Decimal) -> Decimal:
    """Take the quantity from the oldest layers first; return the value taken. A sale beyond
    what is owned takes what there is."""
    taken = Decimal(0)
    while quantity > 0 and layers:
        layer = layers[0]
        part = min(quantity, layer[0])
        value = layer[1] * part / layer[0] if layer[0] else layer[1]
        layer[0], layer[1], quantity, taken = layer[0] - part, layer[1] - value, quantity - part, taken + value
        if layer[0] <= 0:
            layers.pop(0)
    return taken


def _resolve_settlement(book: Book, s: Settlement) -> None:
    bank = book.accounts.get(s.bank)
    _check(bank is not None and bank.control == "bank", "a bank or cash account")
    s.currency = bank.currency
    wanted, wanted_doc = ("Customer", "SalesInvoice") if s.source_type == "Receipt" else ("Supplier", "PurchaseInvoice")
    for line in s.party_lines:
        contact = book.contacts.get(line.contact) if line.contact else None
        _check(contact is not None and contact.source_type == wanted, f"a {wanted.lower()}")
        if contact.currency != s.currency:
            raise Blocked("currency mismatch", "The settlement currency differs from the contact's currency.")
        doc = book.documents.get(line.document)
        _check(doc is not None and doc.source_type == wanted_doc and doc.contact == line.contact,
               "the document it settles")
        _check(line.holder is None, "an account under a control account")
    for line in s.other_lines:
        line.account = _place(book, line.account, line.holder)
        if line.account in (AR, AP):
            _check(line.contact in book.contacts, "a customer or supplier")
        if line.tax_code:
            _check(line.tax_code in book.tax_codes, "a tax code")
    if any(line.net <= 0 for line in s.party_lines):
        raise Blocked("unsupported feature", "A zero or negative customer or supplier amount.")
    _price_lines(book, s.currency, s.other_lines, s.include_tax)


def _resolve_journal(book: Book, j: Journal) -> None:
    _check(j.currency is None or j.currency in book.currencies, "a currency")
    for line in j.lines:
        line.account = _place(book, line.account, line.holder)
        if line.account in (AR, AP):
            _check(line.contact in book.contacts, "a customer or supplier")


def _table_rate(book: Book, currency: str, day: date) -> Decimal | None:
    """The rate table's latest rate for the currency on or before the day."""
    rates = [r for r in book.rates if r.currency == currency and r.effective <= day and not book.is_blocked(r.key)]
    return max(rates, key=lambda r: (r.effective, r.key)).rate if rates else None


def _record_rate(book: Book, currency: str | None, own: Decimal | None, day: date) -> Decimal:
    """Base units per record unit: 1 in the base currency, else the record's own rate, else the
    rate table's latest rate on or before the record's date."""
    if not book.is_foreign(currency):
        return Decimal(1)
    rate = own or _table_rate(book, currency, day)
    if rate is None:
        raise Blocked("no exchange rate", "A foreign currency record with no exchange rate of its own and none "
                                          "in the exchange rate table on or before its date.")
    return rate


def _same_currency(book: Book, record: str | None, holders: list[str | None]) -> None:
    """Every foreign currency account or contact a record touches is in the record's currency:
    one record converts at one rate."""
    code = book.currency_code(record)
    if any(book.is_foreign(c) and book.currency_code(c) != code for c in holders):
        raise Blocked("currency mismatch", "Touches an account, customer or supplier in a currency other than "
                                           "the record's own.")


def _to_base(book: Book, lines: list[Line], rate: Decimal) -> None:
    """Each line's net and tax in the base currency, split from the converted running total so
    the lines add up to the converted record total."""
    parts = split_to_base([x for ln in lines for x in (ln.net, ln.tax)], rate, book.base_code or "")
    for index, line in enumerate(lines):
        line.base_net, line.base_tax = parts[2 * index], parts[2 * index + 1]


def _rate_document(book: Book, doc: Document) -> None:
    doc.rate = _record_rate(book, doc.currency, doc.rate, doc.date)
    _to_base(book, doc.lines, doc.rate)            # before the check: a blocked bill still counts as owned stock
    _same_currency(book, doc.currency, [book.accounts[ln.account].currency for ln in doc.lines
                                        if ln.account in book.accounts])


def _rate_settlement(book: Book, s: Settlement) -> None:
    holders = [*(book.accounts[ln.account].currency for ln in s.other_lines),
               *(book.contacts[ln.contact].currency for ln in s.other_lines if ln.contact in book.contacts)]
    _same_currency(book, s.currency, holders)
    s.rate = _record_rate(book, s.currency, s.rate, s.date)
    _to_base(book, [*s.party_lines, *s.other_lines], s.rate)


def _rate_journal(book: Book, j: Journal) -> None:
    holders = [*(book.accounts[ln.account].currency for ln in j.lines),
               *(book.contacts[ln.contact].currency for ln in j.lines if ln.contact in book.contacts)]
    _same_currency(book, j.currency, holders)
    j.rate = _record_rate(book, j.currency, None, j.date)
    code = book.base_code or ""
    for line, debit, credit in zip(j.lines, split_to_base([ln.debit for ln in j.lines], j.rate, code),
                                   split_to_base([ln.credit for ln in j.lines], j.rate, code)):
        line.base_debit, line.base_credit = debit, credit


def _rate_transfer(book: Book, t: Transfer) -> None:
    """What a transfer moves in the base currency. A base currency side states it exactly; a
    transfer within one foreign currency converts at the rate table's rate on its date. Two
    different foreign currencies would need a rate between them, which Manager does not keep."""
    source, dest = book.accounts[t.from_bank].currency, book.accounts[t.to_bank].currency
    if not book.is_foreign(source) or not book.is_foreign(dest):
        if not book.is_foreign(source) and not book.is_foreign(dest) and t.amount != t.to_amount:
            raise Blocked("unequal amounts", "The two sides of the transfer differ.")
        t.base = t.to_amount if not book.is_foreign(dest) else t.amount
        return
    if book.currency_code(source) != book.currency_code(dest):
        raise Blocked("currency mismatch", "A transfer between two different foreign currencies.")
    if t.amount != t.to_amount:
        raise Blocked("unequal amounts", "The two sides of the transfer differ.")
    t.rate = _record_rate(book, source, None, t.date)
    t.base = round_money(t.amount * t.rate, book.base_code or "")


def _parent_check(book: Book, account: Account) -> None:
    """An account under a control account is under the built-in one of its kind or a custom
    control account of that kind."""
    parent = book.accounts.get(account.parent)
    _check(parent is not None and not book.is_blocked(parent.key)
           and (parent.key == account.kind or (parent.source_type in CONTROL_KINDS and parent.kind == account.kind)),
           "the control account it is reported under")


def _resolve_starting(book: Book, sb: StartingBalance) -> None:
    """The lines a starting balance posts to the opening journal, debit positive."""
    want = STARTING_TYPES[sb.source_type]
    if want == "InventoryItem":
        _check(sb.target in book.items and not book.is_blocked(sb.target), "an inventory item")
        value = round_money(sb.quantity * sb.amounts[0], book.base_code or "")
        sb.stock, sb.lines = (sb.quantity, value), [(INVENTORY, value, None)]
        return
    if want in DOC_TYPES:
        doc = book.documents.get(sb.target)
        _check(doc is not None and doc.source_type == want, "the invoice or bill it is the starting balance of")
        if book.is_blocked(doc.key):
            raise Blocked("blocked document", "Its invoice or bill cannot be migrated.")
        if book.is_foreign(doc.currency):
            raise Blocked("foreign currency starting balance", FOREIGN_START_NOTE)
        sb.paid = sb.amounts[0]
        if not 0 < sb.paid <= doc.total:
            raise Blocked("unsupported feature", "What the invoice or bill had received or paid before the books "
                                                 "began is not more than nothing and at most its total.")
        sb.lines = [(AR, -sb.paid, doc.contact) if want == "SalesInvoice" else (AP, sb.paid, doc.contact)]
        return
    account = book.accounts.get(sb.target)
    _check(account is not None and account.source_type == want and not book.is_blocked(sb.target),
           "the account it is the starting balance of")
    if book.is_foreign(account.currency):
        raise Blocked("foreign currency starting balance", FOREIGN_START_NOTE)
    if want == "Investment":
        sb.amounts = (round_money(sb.amounts[0], book.base_code or ""),)
    keys = [sb.target, *(sb.target + WRITTEN_OFF[want][0] for _ in sb.amounts[1:])]
    sb.lines = [(key, amount, None) for key, amount in zip(keys, sb.amounts) if amount]


def _resolve(book: Book) -> None:
    names = book.names
    for account in list(book.accounts.values()):
        if account.parent in HOLDER_BUILTINS and account.parent not in book.accounts:
            account_type, label = HOLDER_BUILTINS[account.parent]
            book.accounts[account.parent] = Account(account.parent, TYPE_NAMES[account.parent], label,
                                                    account_type=account_type)
    for account in book.accounts.values():
        if account.parent is not None:
            try:
                _parent_check(book, account)
            except Blocked as blocked:
                book.block(account.source_type, source_key(account.key), blocked.reason, blocked.note)
                continue
        if account.account_type is None:
            account.account_type = _account_type(book, account)
            if account.account_type is None:
                book.block(account.source_type, account.key, "unresolved group",
                           "The account's group does not lead to a statement section.")
        if account.currency is not None and account.currency not in book.currencies:
            book.block(account.source_type, account.key, "unknown reference", "Refers to a currency not in the file.")
    book.controls = _controls(book)
    for contact in book.contacts.values():
        if contact.currency is not None and contact.currency not in book.currencies:
            book.block(contact.source_type, contact.key, "unknown reference", "Refers to a currency not in the file.")
        elif contact.control is not None:
            if book.merged_controls.get(contact.control) == PARTY_CONTROLS[contact.source_type]:
                book.losses[(contact.key, f"{contact.source_type} (custom control account)")] = CONTROL_NOTE
            else:
                book.block(contact.source_type, contact.key, "unknown reference",
                           "Refers to a control account not in the file.")
    for tax in book.tax_codes.values():
        if tax.account not in book.accounts:
            book.block("TaxCode", tax.key, "unknown reference", "Refers to an account not in the file.")
    for rate in book.rates:
        if rate.currency not in book.currencies:
            book.block("ExchangeRate", rate.key, "unknown reference", "Refers to a currency not in the file.")

    _activity(book)
    steps: list[tuple[dict, Callable[[Book, object], None], Callable[[Book, object], None]]] = [
        (book.documents, _resolve_document, _rate_document), (book.settlements, _resolve_settlement, _rate_settlement),
        (book.journals, _resolve_journal, _rate_journal),
    ]
    for records, resolve, rate in steps:
        for key, record in list(records.items()):
            try:
                resolve(book, record)
                rate(book, record)
            except Blocked as blocked:                          # a second journal of a record blocks the record
                book.block(names[key], source_key(key), blocked.reason, blocked.note)
    for key, order in list(book.orders.items()):
        try:
            _resolve_order(book, order)
        except Blocked as blocked:
            book.set_aside(order.source_type, key, blocked.reason, blocked.note)
    _link_orders(book)
    for key, t in book.transfers.items():
        try:
            for bank in (t.from_bank, t.to_bank):
                _check(bank in book.accounts and book.accounts[bank].control == "bank", "a bank or cash account")
            _rate_transfer(book, t)
        except Blocked as blocked:
            book.block("InterAccountTransfer", key, blocked.reason, blocked.note)
    for key, sb in book.starting.items():
        try:
            _resolve_starting(book, sb)
        except Blocked as blocked:
            book.block(sb.source_type, key, blocked.reason, blocked.note)
    _cost_sales(book)
    for key, movement in book.movements.items():
        try:
            _link(book, movement)
        except Blocked as blocked:
            book.block(movement.source_type, key, blocked.reason, blocked.note)
    _value_stock(book)
    # A settlement that pays a blocked document, or a note on a blocked invoice, cannot stand alone.
    _payers(book)
    for key, s in book.settlements.items():
        if not book.is_blocked(key) and any(ln.document and book.is_blocked(ln.document) for ln in s.party_lines):
            book.block(s.source_type, key, "blocked document", "Settles a document that cannot be migrated.")
    for key, doc in book.documents.items():
        if not book.is_blocked(key) and doc.applies_to and book.is_blocked(doc.applies_to):
            book.block(doc.source_type, key, "blocked document", "Settles a document that cannot be migrated.")
    for key, sb in book.starting.items():
        if not book.is_blocked(key) and sb.paid and book.is_blocked(sb.target):
            book.block(sb.source_type, key, "blocked document", "Its invoice or bill cannot be migrated.")
    _late_fee_receipts(book)
    _disposals(book)
    _revaluations(book)
    # Starting balances stand the day before the first record that moves money or stock.
    dated = [day for day, _ in book.dated_records()] + [m.date for m in book.movements.values()]
    book.starting_date = min(dated) - timedelta(days=1) if dated else None
    if not book.base_code or len(book.base_code) != 3:
        key = book.base_key or GUIDS["BaseCurrency"]
        book.block("BaseCurrency", key, "missing", "The business has no base currency code. Set it in Manager under "
                                                   "Settings, Base Currency, then upload the file again.")


PAYER_NOTE = ("The name typed for a payer who is neither a customer nor a supplier is not carried. Celerp records "
              "the payment against its accounts, without a payer name.")


def _payers(book: Book) -> None:
    """A payer name is lost only when it is not the name of a customer or supplier the
    settlement names: Manager's upgrade fills it in with that contact's name."""
    for key, s in book.settlements.items():
        named = {*s.form_contacts, *(ln.contact for ln in (*s.party_lines, *s.other_lines) if ln.contact)}
        names = {book.contacts[c].name for c in named if c in book.contacts}
        if s.payer and s.payer not in names:
            book.losses[(key, f"{s.source_type} (payer name)")] = PAYER_NOTE


LATE_FEE_NOTE = ("Pays an invoice more than its own total, toward the late payment fee Manager added to it. Celerp "
                 "keeps the fee on the customer's balance, not on the invoice, so the receipt would overpay the "
                 "invoice.")


def _late_fee_receipts(book: Book) -> None:
    """A late payment fee stays on the customer's balance, so its invoice keeps its own total.
    A receipt or credit note taking what is paid on such an invoice past that total, in date
    order after what was paid before the books began, blocks."""
    charged = {_ref(m.guid(3)) for key, m in book.activity.items()
               if book.names[key] == "LatePaymentFee" and not book.is_blocked(key)} & set(book.documents)
    events = sorted([*((s.date, s.key, ln.document, ln.net) for s in book.settlements.values()
                       for ln in s.party_lines if ln.document in charged),
                     *((d.date, d.key, d.applies_to, d.total) for d in book.documents.values()
                       if d.applies_to in charged)])
    paid = book.starting_paid()
    for _, key, invoice, amount in events:
        paid[invoice] = paid.get(invoice, Decimal(0)) + amount
        if paid[invoice] > book.documents[invoice].total and not book.is_blocked(key):
            book.block(book.names[key], key, "late payment fee", LATE_FEE_NOTE)


class _Balances:
    """Base balances, debit positive, from the starting balances and every record not blocked."""

    def __init__(self, book: Book):
        from celerp.importers.adapters.manager_io.ledger import record_postings  # ledger builds on this module

        self.postings = record_postings(book, {k for _, k in book.dated_records()
                                               if not book.is_blocked(source_key(k))})
        self.opening = [(account, amount) for sb in book.live_starting() for account, amount, _ in sb.lines]

    def through(self, account: str, day: date) -> Decimal:
        return (sum((amount for key, amount in self.opening if key == account), Decimal(0))
                + sum((p.amount for p in self.postings if p.account == account and p.date <= day), Decimal(0)))


def _install(book: Book, key: str, name: str, day: date, narration: str, lines: list[JournalLine]) -> None:
    """A journal Celerp posts from the balances the records leave, in the base currency."""
    journal = Journal(key, day, None, narration, None, [ln for ln in lines if ln.debit or ln.credit])
    for line in journal.lines:
        _add_builtin(book, line.account)
    _rate_journal(book, journal)
    book.journals[key], book.names[key] = journal, name


DISPOSAL_NAMES = {"FixedAsset": "FixedAssetDisposal", "IntangibleAsset": "IntangibleAssetDisposal"}


def _disposals(book: Book) -> None:
    """An asset marked disposed of posts a journal on its disposal date, after that day's
    records, clearing what it cost and what was written off it against its disposal account.
    A record posting to the asset after that date blocks."""
    disposed = {key: asset for key, asset in book.assets.items() if asset.disposed and not book.is_blocked(key)}
    if not disposed:
        return
    balances = _Balances(book)
    for key, asset in disposed.items():
        name = book.names[key]
        written_off = key + WRITTEN_OFF[name][0]
        for record in sorted({p.record for p in balances.postings
                              if p.account in (key, written_off) and p.date > asset.disposed}):
            book.block(book.names[record], source_key(record), "disposed asset",
                       "Posts to an asset after the date it was disposed of.")
        if asset.disposal_account not in book.accounts and asset.disposal_account not in ACTIVITY_BUILTINS:
            book.block(name, key, "unknown reference", "Refers to a disposal account that is not in the file.")
            continue
        cost, off = balances.through(key, asset.disposed), balances.through(written_off, asset.disposed)
        _install(book, f"{key}:disposal", DISPOSAL_NAMES[name], asset.disposed,
                 f"Disposal of {book.accounts[key].name}.",
                 [_posting(key, -cost), _posting(written_off, -off), _posting(asset.disposal_account, cost + off)])


NO_UNITS_NOTE = "The investment holds no units Celerp can read, so its market value is not known."


def _revaluations(book: Book) -> None:
    """Each market price brings its investment to the units held times the price, against
    investment gains and losses, on the price's date and again on each later date a record
    posts to the investment."""
    prices: dict[str, list[MarketPrice]] = {}
    for price in book.prices.values():
        try:
            prices.setdefault(_named(book, price.investment, "Investment", "an investment"), []).append(price)
        except Blocked as blocked:
            book.block("InvestmentMarketPrice", price.key, blocked.reason, blocked.note)
    if not prices:
        return
    balances = _Balances(book)
    for investment, dated in prices.items():
        units = sum((sb.quantity for sb in book.live_starting() if sb.target == investment), Decimal(0))
        if not units:
            for price in dated:
                book.block("InvestmentMarketPrice", price.key, "unsupported feature", NO_UNITS_NOTE)
            continue
        first = min(p.date for p in dated)
        days = {p.date for p in dated} | {p.date for p in balances.postings
                                          if p.account == investment and p.date >= first}
        revalued = Decimal(0)
        for day in sorted(days):
            price = max((p for p in dated if p.date <= day), key=lambda p: (p.date, p.key)).price
            change = round_money(units * price, book.base_code or "") - balances.through(investment, day) - revalued
            if change:
                revalued += change
                _install(book, f"{investment}:revaluation:{day.isoformat()}", "InvestmentRevaluation", day,
                         f"Revaluation of {book.accounts[investment].name} at market price.",
                         [_posting(investment, change), _posting(INVESTMENT_GAINS, -change)])


def _link(book: Book, movement: Movement) -> None:
    """Check a goods receipt or delivery note against the document it moves goods for, and
    bind each item it moves to that document's line. Lines of one item are merged."""
    if movement.document is None:
        raise Blocked("not linked", "Carried only as linked to the invoice or bill it moves goods for.")
    doc = book.documents.get(movement.document)
    _check(doc is not None and doc.source_type == MOVEMENT_OF[movement.source_type],
           "the invoice or bill it moves goods for")
    if movement.location not in (None, DEFAULT_LOCATION):
        raise Blocked("multiple locations", LOCATION_NOTE)
    if book.is_blocked(doc.key):
        raise Blocked("blocked document", "Moves goods for a document that cannot be migrated.")
    if doc.moves_stock:
        raise Blocked("unsupported feature", "Its invoice or bill already moves its own stock.")
    merged: dict[str, Decimal] = {}
    for line in movement.lines:
        _check(line.item in book.items, "an inventory item")
        merged[line.item] = merged.get(line.item, Decimal(0)) + line.quantity
    lines = []
    for item, quantity in merged.items():
        matches = [index for index, line in enumerate(doc.lines) if line.item == item]
        if not matches:
            raise Blocked("not on the document", "Moves an item its invoice or bill does not list.")
        if len(matches) > 1:
            raise Blocked("unsupported feature", "Moves an item listed on more than one line of its invoice or bill.")
        lines.append(MoveLine(item, quantity, matches[0]))
    movement.lines = lines


def _own_movement(doc: Document) -> Movement:
    """The stock an invoice or bill flagged to move its own stock moves, line by line."""
    sign = 1 if doc.source_type in INBOUND else -1
    return Movement(doc.key, doc.source_type, doc.date, doc.ref, doc.key, doc.location,
                    [MoveLine(line.item, sign * line.quantity, index) for index, line in enumerate(doc.lines)
                     if line.item])


def _value_stock(book: Book) -> None:
    """Value every movement in the order stock moved, receipts first on a day, from the stock
    the business started with.

    Goods received carry their share of the bill line's net and goods delivered their share
    of the cost of sales the invoice line booked, the last movement of a line taking what is
    left of it. A movement that takes more than its document lists, moves a line of zero or
    negative quantity, or takes more stock than is held, is blocked and moves nothing."""
    code = book.base_code or ""
    movements = [m for k, m in book.movements.items() if not book.is_blocked(k)]
    movements += [_own_movement(d) for k, d in book.documents.items()
                  if d.moves_stock and not book.is_blocked(k) and any(line.item for line in d.lines)]
    held = book.starting_stock()
    taken: dict[tuple[str, int], tuple[Decimal, Decimal]] = {}
    for movement in sorted(movements, key=lambda m: (m.date, m.source_type not in INBOUND, m.key)):
        doc = book.documents[movement.document]
        now_held, now_taken = dict(held), dict(taken)
        try:
            for line in movement.lines:
                source = doc.lines[line.line]
                qty, value = now_taken.get((doc.key, line.line), (Decimal(0), Decimal(0)))
                if qty + abs(line.quantity) > source.quantity:
                    raise Blocked("more than invoiced", "Moves more goods than its invoice or bill lists.")
                if source.quantity <= 0:
                    raise Blocked("unsupported feature", "A zero or negative quantity.")
                on_hand, worth = now_held.get(line.item, (Decimal(0), Decimal(0)))
                if on_hand + line.quantity < 0:
                    raise Blocked("negative stock", "Moves more stock out than is on hand at the time.")
                booked = source.base_net if line.quantity > 0 else -source.cost
                share = round_money(booked * (qty + abs(line.quantity)) / source.quantity, code)
                line.value = share - (value if line.quantity > 0 else -value)
                now_taken[(doc.key, line.line)] = (qty + abs(line.quantity), value + abs(line.value))
                now_held[line.item] = (on_hand + line.quantity, worth + line.value)
        except Blocked as blocked:
            book.block(book.names[movement.key], movement.key, blocked.reason, blocked.note)
            continue
        held, taken = now_held, now_taken
        book.moves.append(movement)


def _receipt_verdicts(book: Book) -> None:
    """A settlement with lines that settle no document keeps them as a journal."""
    for key, s in book.settlements.items():
        if book.is_blocked(key) or not s.other_lines:
            continue
        book.accept(s.source_type, key, label=f"{s.source_type} (journal fallback)",
                    klass=CoverageClass.MAPPED_WITH_LOSS, target="journal",
                    note="Lines that settle no invoice or bill, money on account included, are carried as a "
                         "journal entry naming the customer or supplier.")


def _document_verdicts(book: Book) -> None:
    """A document whose line form Celerp cannot keep is carried with exact totals, reported per record."""
    for key, doc in [*book.documents.items(), *book.orders.items()]:
        if book.is_blocked(key):
            continue
        lost = []
        if any(ln.discount and (ln.discount_percent is None or doc.include_tax) for ln in doc.lines):
            lost.append(("line discount amount", "Celerp line discounts are percentages. A discount entered as "
                         "an amount, or on amounts including tax, is kept in the line total only."))
        if doc.include_tax and any(ln.tax for ln in doc.lines):
            lost.append(("amounts including tax", "Amounts entered including tax are shown excluding tax. "
                         "Editing the document later recomputes tax from those amounts, which can differ by a cent."))
        if lost:
            book.accept(doc.source_type, key, label=f"{doc.source_type} ({', '.join(w for w, _ in lost)})",
                        klass=CoverageClass.MAPPED_WITH_LOSS,
                        note=" ".join(filter(None, (NOTES.get(doc.source_type), *(n for _, n in lost)))))


def _master_verdicts(book: Book) -> None:
    """A master whose attribute Celerp cannot hold is carried without it, reported per record.
    An inactive item with no stock is not listed: it becomes an archived item."""
    from celerp.importers.adapters.manager_io.ledger import holding_stock  # ledger builds on this module

    held = holding_stock(book)
    lost: list[tuple[str, str, str, str]] = [
        *((c.source_type, c.key, "inactive", "Celerp has no inactive state for customers and suppliers; "
           "this one is imported as active.") for c in book.contacts.values() if c.inactive),
        *(("TaxCode", t.key, "inactive", "Celerp has no inactive state for tax codes; this one is imported "
           "as active.") for t in book.tax_codes.values() if t.inactive),
        *(("ForeignCurrency", c.key, "inactive", "Celerp has no inactive state for currencies; this one is "
           "imported as active.") for c in book.currencies.values() if c.inactive),
        *(("InventoryItem", i.key, "inactive with stock", "It still holds stock, so it is imported as available. "
           "Archive it in Celerp once the stock is gone.") for i in book.items.values() if i.inactive and i.key in held),
        *(("InventoryItem", i.key, "purchase price not moved", "Celerp has no default purchase price for items. "
           "Inventory cost comes from the purchases themselves.") for i in book.items.values()
          if i.purchase_price is not None),
    ]
    per_record: dict[str, list[tuple[str, str, str]]] = {}
    for type_name, key, what, note in lost:
        per_record.setdefault(key, []).append((type_name, what, note))
    for key, losses in per_record.items():
        if not book.is_blocked(key):
            type_name = losses[0][0]
            book.accept(type_name, key, label=f"{type_name} ({', '.join(w for _, w, _ in losses)})",
                        klass=CoverageClass.MAPPED_WITH_LOSS, note=" ".join(n for _, _, n in losses))


# Types held once per business, keyed by the type itself. Any other carried type keyed by
# itself is the defaults Manager fills a new record of that type with.
SINGLETONS = frozenset({"BusinessDetails", "BaseCurrency", "LockDate", "Assets", "Liabilities", "Equity",
                        "DefaultInventoryLocation"})
FORM_DEFAULTS_NOTE = "The values Manager fills a new form with. They are not a record and are not carried."


def read_book(reader: ManagerReader) -> Book:
    """Decode and classify every object in an open Manager file."""
    book = Book(schema_version=reader.schema_version)
    for row in reader.objects():
        key, ctype = row.key.lower(), row.content_type.lower()
        name = TYPE_NAMES.get(ctype)
        book.object_counts[name or ctype] = book.object_counts.get(name or ctype, 0) + 1
        if name is None:
            book.verdicts[key] = Verdict(f"Unknown type {ctype}", CoverageClass.UNCLASSIFIED, None,
                                         "An object type Celerp does not recognise.")
            continue
        if key == ctype and name in fields.SCHEMAS and name not in SINGLETONS and key not in BUILTINS \
                and key not in INVENTORY_BUILTINS and key not in HOLDER_BUILTINS and key not in ACTIVITY_BUILTINS:
            book.verdicts[key] = Verdict(f"{name} form defaults", CoverageClass.IGNORED_NON_BUSINESS, None,
                                         FORM_DEFAULTS_NOTE)
            continue
        book.names[key] = name
        book.accept(name, key)
        if name in INVENTORY_BLOCKERS:
            book.block(name, key, *INVENTORY_BLOCKERS[name])
            continue
        if CONTENT_TYPES[ctype] not in (CoverageClass.MAPPED, CoverageClass.MAPPED_WITH_LOSS):
            continue
        try:
            if row.content is None:
                raise DecodeError(f"Object payload larger than {reader.max_object_bytes} bytes.")
            message = decode(row.content)
            unknown, lost = fields.check(name, message)
            if unknown:
                raise Blocked("unknown field", f"Field {unknown} is not one Celerp reads or has classified, so "
                                               "what it records would be lost in the migration.")
            _decode_object(book, name, key, message)
            book.losses |= {(key, f"{name} ({f.lost})"): f.note for f in lost}
        except DecodeError as exc:
            (book.set_aside if name in ORDER_TYPES else book.block)(name, key, "unreadable", f"Could not be read: {exc}")
        except Blocked as blocked:
            (book.set_aside if name in ORDER_TYPES else book.block)(name, key, blocked.reason, blocked.note)
    for key, (account_type, control, label) in BUILTINS.items():
        book.accounts.setdefault(key, Account(key, TYPE_NAMES[key], label, None, None, account_type, control))
    if any(doc.source_type == "SalesInvoice" and any(line.item for line in doc.lines) for doc in book.documents.values()):
        for key, (account_type, control, label) in INVENTORY_BUILTINS.items():
            book.accounts.setdefault(key, Account(key, TYPE_NAMES[key], label, None, None, account_type, control))
    _resolve(book)
    _receipt_verdicts(book)
    _document_verdicts(book)
    _master_verdicts(book)
    book.history = {"changes": reader.row_count("Changes"), "emails": reader.row_count("Emails")}
    return book
