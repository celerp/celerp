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

from celerp.importers.adapters.manager_io.protobuf import DecodeError, Message, decode
from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
from celerp.importers.adapters.manager_io.types import CONTENT_TYPES, GUIDS, NOTES, TARGETS, TYPE_NAMES
from celerp.importers.schema import CIFCoverageEntry, CoverageClass
from celerp.services.money import currency_dp, round_money

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

# Built-in account: (account type, control, name used when the file has no object for it).
BUILTINS: dict[str, tuple[str, str | None, str]] = {
    AR: ("asset", "receivable", "Accounts receivable"),
    AP: ("liability", "payable", "Accounts payable"),
    TAX: ("liability", "tax", "Tax payable"),
    INVENTORY: ("asset", "inventory", "Inventory on hand"),
    RETAINED: ("equity", "retained_earnings", "Retained earnings"),
    FX_GAIN: ("expense", None, "Foreign exchange gains and losses"),
}
ROOT_TYPES = {ASSETS: "asset", LIABILITIES: "liability", EQUITY: "equity", INCOME: "revenue", EXPENSES: "expense"}

# Foreign-currency record types whose FX treatment a synthetic fixture proves. Any other type in a
# foreign currency blocks Full history.
FX_PROVEN = frozenset({"SalesInvoice", "PurchaseInvoice", "Receipt"})

DOC_TYPES = {
    "SalesInvoice": "invoice", "CreditNote": "credit_note", "PurchaseInvoice": "bill", "DebitNote": "debit_note",
}
# The document a note settles: a credit note reduces a sales invoice, a debit note a purchase invoice.
NOTE_OF = {"CreditNote": "SalesInvoice", "DebitNote": "PurchaseInvoice"}


class Blocked(Exception):
    """One object carries a source feature Celerp cannot reproduce faithfully."""

    def __init__(self, reason: str, note: str):
        super().__init__(note)
        self.reason = reason
        self.note = note


def _money(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal(0)


def _rate(value: Decimal | None, inverse: bool) -> Decimal | None:
    """Base units per one foreign unit. Manager can store the inverse; 1/x restores the direction."""
    if value is None or value == 0:
        return None
    return Decimal(1) / value if inverse else value


# ── Source records ─────────────────────────────────────────────────────────────

@dataclass
class Currency:
    key: str
    code: str
    name: str | None
    precision: int
    inactive: bool = False


@dataclass
class Rate:
    key: str
    effective: date
    currency: str                              # currency object key
    rate: Decimal                              # base units per one foreign unit


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


@dataclass
class Item:
    key: str
    code: str | None
    name: str
    unit: str | None
    sales_price: Decimal | None
    purchase_price: Decimal | None
    inactive: bool


@dataclass
class Line:
    """A priced line. `net` excludes tax and is after discount; figures are in the record currency."""
    account: str | None
    item: str | None
    description: str | None
    quantity: Decimal
    unit_price: Decimal
    discount: Decimal
    tax_code: str | None
    net: Decimal = Decimal(0)
    tax: Decimal = Decimal(0)
    contact: str | None = None                 # party line of a settlement
    document: str | None = None                # settled document of a party line


@dataclass
class Document:
    key: str
    source_type: str
    date: date
    due: date | None
    ref: str | None
    contact: str
    currency: str | None                       # currency object key; None is the base currency
    rate: Decimal | None
    include_tax: bool
    description: str | None
    lines: list[Line]
    applies_to: str | None = None              # the invoice a note settles

    @property
    def doc_type(self) -> str:
        return DOC_TYPES[self.source_type]

    @property
    def total(self) -> Decimal:
        return sum((ln.net + ln.tax for ln in self.lines), Decimal(0))

    @property
    def tax_total(self) -> Decimal:
        return sum((ln.tax for ln in self.lines), Decimal(0))


@dataclass
class Settlement:
    key: str
    source_type: str                           # Receipt or Payment
    date: date
    ref: str | None
    bank: str
    currency: str | None
    rate: Decimal | None
    description: str | None
    party_lines: list[Line]                    # receivable lines of a receipt, payable lines of a payment
    other_lines: list[Line]                    # everything else: kept as a journal
    include_tax: bool = True

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
    amount: Decimal


@dataclass
class JournalLine:
    account: str
    contact: str | None
    debit: Decimal
    credit: Decimal
    description: str | None


@dataclass
class Journal:
    key: str
    date: date
    ref: str | None
    narration: str | None
    currency: str | None
    lines: list[JournalLine]


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
    settlements: dict[str, Settlement] = field(default_factory=dict)
    transfers: dict[str, Transfer] = field(default_factory=dict)
    journals: dict[str, Journal] = field(default_factory=dict)
    attachments: dict[str, AttachmentRef] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)          # object key -> Manager type name
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    object_counts: dict[str, int] = field(default_factory=dict)
    history: dict[str, int] = field(default_factory=dict)

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

    def is_blocked(self, key: str) -> bool:
        return self.verdicts[key].coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER

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
_RECEIPT_LINE_LINKS = (5, 6, 9, 10, 11, 12, 13, 14, 19, 22, 23, 26, 27, 28, 38)
_PAYMENT_LINE_LINKS = (5, 6, 9, 10, 11, 12, 13, 14, 19, 22, 23, 26, 27, 28, 39)
_JOURNAL_LINE_LINKS = (4, 5, 6, 8, 9, 10, 11, 15, 17, 18, 20, 21, 22, 23, 24, 26, 30)


def _doc_line(m: Message, links: tuple[int, ...]) -> Line:
    _unsupported(m, links, "a line link")
    return Line(
        account=_ref(m.guid(2)), item=_ref(m.guid(1)), description=m.str(17),
        quantity=m.decimal(18) if m.values(18) else Decimal(1), unit_price=_money(m.decimal(19)),
        discount=Decimal(0), tax_code=_ref(m.guid(21)),
    )


def _line_discounts(msg: Message, lines: list[Message], parsed: list[Line], flag: int, kind: int) -> None:
    if not msg.bool(flag):
        return
    exact = msg.int(kind, 0) == 1
    for raw, line in zip(lines, parsed):
        line.discount = _money(raw.decimal(24)) if exact else -_money(raw.decimal(23))  # negative marks a percentage


def _due(msg: Message, issue: date, kind: int, days: int, on: int) -> date | None:
    if msg.int(kind, 0) == 1:
        return msg.date(on)
    count = msg.int(days)
    return issue + timedelta(days=count) if count else None


def _document(source_type: str, key: str, m: Message) -> Document:
    if source_type == "SalesInvoice":
        _unsupported(m, (29, 28, 35), "invoice rounding, withholding tax or an early payment discount")
        raw = m.messages(49)
        lines = [_doc_line(x, _SALES_LINE_LINKS) for x in raw]
        _line_discounts(m, raw, lines, 31, 32)
        issue = _require_date(m, 1)
        return Document(key, source_type, issue, _due(m, issue, 54, 22, 6), m.str(2), _ref(m.guid(3)), None,
                        _rate(m.decimal(64), m.bool(65)), m.bool(8), m.str(12), lines)
    if source_type == "PurchaseInvoice":
        _unsupported(m, (24, 65, 68), "withholding tax, freight or landed costs")
        raw = m.messages(23)
        lines = [_doc_line(x, _PURCHASE_LINE_LINKS) for x in raw]
        _line_discounts(m, raw, lines, 14, 15)
        issue = _require_date(m, 1)
        return Document(key, source_type, issue, _due(m, issue, 31, 19, 5), m.str(2), _ref(m.guid(3)), None,
                        _rate(m.decimal(61), m.bool(62)), m.bool(7), m.str(9), lines)
    if source_type == "CreditNote":
        _unsupported(m, (13,), "withholding tax")
        raw = m.messages(22)
        lines = [_doc_line(x, _SALES_LINE_LINKS) for x in raw]
        _line_discounts(m, raw, lines, 15, 16)
        return Document(key, source_type, _require_date(m, 1), None, m.str(2), _ref(m.guid(3)), None,
                        _rate(m.decimal(30), m.bool(31)), m.bool(6), m.str(9), lines, _ref(m.guid(8)))
    raw = m.messages(16)                                            # DebitNote
    lines = [_doc_line(x, _PURCHASE_LINE_LINKS) for x in raw]
    _line_discounts(m, raw, lines, 10, 11)
    return Document(key, source_type, _require_date(m, 1), None, m.str(2), _ref(m.guid(3)), None,
                    _rate(m.decimal(22), m.bool(23)), m.bool(5), m.str(7), lines, _ref(m.guid(6)))


def _settlement(source_type: str, key: str, m: Message) -> Settlement:
    receipt = source_type == "Receipt"
    if receipt:
        bank, rate, qty_col, price_col, exclusive = m.guid(7), _rate(m.decimal(45), m.bool(46)), 32, 33, 31
        _unsupported(m, (34, 35, 38, 39), "a discount or a fixed total")
        links, party_account, contact_field, doc_field = _RECEIPT_LINE_LINKS, AR, 3, 4
        other_party = (AP, 7, 8)
    else:
        bank, rate, qty_col, price_col, exclusive = m.guid(7), _rate(m.decimal(46), m.bool(47)), 33, 34, 32
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
                    discount=Decimal(0), tax_code=_ref(raw.guid(20)), net=amount)
        if account == party_account:
            line.contact, line.document = _ref(raw.guid(contact_field)), _ref(raw.guid(doc_field))
            if raw.values(20):
                raise Blocked("unsupported feature", "A tax code on a customer or supplier balance line.")
            party.append(line)
            continue
        if account == other_party[0]:
            if raw.values(other_party[2]):
                raise Blocked("unsupported feature", "Settles a document of the other ledger.")
            line.contact = _ref(raw.guid(other_party[1]))
        other.append(line)
    contacts = {ln.contact for ln in party}
    if len(contacts) > 1:
        raise Blocked("several contacts", "One settlement for several customers or suppliers.")
    return Settlement(key, source_type, _require_date(m, 1), m.str(2), str(bank), None, rate, m.str(10),
                      party, other, not m.bool(exclusive))


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
            lines.append(JournalLine(account, contact, debit, credit, raw.str(12)))
    debits = sum((ln.debit for ln in lines), Decimal(0))
    credits = sum((ln.credit for ln in lines), Decimal(0))
    if len(lines) < 2 or debits != credits or any(ln.debit and ln.credit for ln in lines):
        raise Blocked("unbalanced", "The journal entry does not balance.")
    return Journal(key, _require_date(m, 1), m.str(2), m.str(3), _ref(m.guid(8)), lines)


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
        rate = _rate(m.decimal(6), m.bool(7))
        currency = _ref(m.guid(2))
        if rate is None or currency is None:
            raise Blocked("unsupported feature", "The exchange rate has no currency or value.")
        book.rates.append(Rate(key, _require_date(m, 1), currency, rate))
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
        _unsupported(m, (12,), "a custom control account")
        book.accounts[key] = Account(key, name, m.str(1) or "", m.str(13), None, "asset", "bank",
                                     _ref(m.guid(3)), m.bool(10))
    elif key in BUILTINS:
        account_type, control, _ = BUILTINS[key]
        book.accounts[key] = Account(key, name, m.str(1) or BUILTINS[key][2], None, None, account_type, control)
    elif name == "TaxCode":
        if m.int(5, 0) == 1 or m.int(6, 0) == 1 or m.bool(11):
            raise Blocked("unsupported feature", "A total-rate, multiple-rate or reverse-charge tax code.")
        rate = _money(m.decimal(4)) if m.int(5, 0) == 2 else Decimal(0)
        book.tax_codes[key] = TaxCode(key, m.str(1) or "", rate, _ref(m.guid(7)) or TAX, m.bool(10))
    elif name == "Customer":
        _unsupported(m, (19,), "a custom control account")
        book.contacts[key] = Contact(key, name, m.str(1) or "", m.str(13), _ref(m.guid(14)), m.str(3), m.str(2),
                                     m.bool(15))
    elif name == "Supplier":
        _unsupported(m, (16,), "a custom control account")
        book.contacts[key] = Contact(key, name, m.str(1) or "", m.str(10), _ref(m.guid(11)), m.str(2), m.str(7),
                                     m.bool(12))
    elif name == "InventoryItem":
        code, item_name = m.str(1), m.str(11)
        book.items[key] = Item(key, code, item_name or code or "", m.str(13),
                               m.decimal(3) if m.bool(32) else None, m.decimal(2) if m.bool(31) else None, m.bool(10))
    elif name in DOC_TYPES:
        book.documents[key] = _document(name, key, m)
    elif name in ("Receipt", "Payment"):
        book.settlements[key] = _settlement(name, key, m)
    elif name == "InterAccountTransfer":
        source, dest = _ref(m.guid(2)), _ref(m.guid(3))
        credit, debit = _money(m.decimal(8)), _money(m.decimal(9))
        if source is None or dest is None:
            raise Blocked("unknown reference", "The transfer names no source or destination account.")
        if credit != debit or credit <= 0:
            raise Blocked("unequal amounts", "The two sides of the transfer differ.")
        book.transfers[key] = Transfer(key, _require_date(m, 1), m.str(6), m.str(5), source, dest, credit)
    elif name == "JournalEntry":
        book.journals[key] = _journal(key, m)
    elif name == "Attachment":
        book.attachments[key] = AttachmentRef(key, m.str(2) or "", m.int(4, 0), _ref(m.guid(6)), m.bytes(12))


# ── Resolution: references, account types, figures, currency ──────────────────

def _account_type(book: Book, account: Account) -> str | None:
    if account.account_type:
        return account.account_type
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


def _price_lines(book: Book, currency: str | None, lines: list[Line], include_tax: bool) -> None:
    """Net, discount and tax per line, rounded half up to the record currency's precision."""
    code = book.currency_code(currency)
    for line in lines:
        gross = round_money(line.quantity * line.unit_price, code) if line.net == 0 else line.net
        if line.discount < 0:                                       # a percentage, marked negative on decode
            line.discount = round_money(gross * -line.discount / 100, code)
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
    wanted = "Customer" if doc.source_type in ("SalesInvoice", "CreditNote") else "Supplier"
    _check(contact is not None and contact.source_type == wanted, f"a {wanted.lower()}")
    doc.currency = contact.currency
    for line in doc.lines:
        if line.item:
            _check(line.item in book.items, "an inventory item")
            if doc.source_type != "PurchaseInvoice":
                raise Blocked("inventory item line", "Inventory items on this record type are not carried yet.")
        else:
            _check(line.account in book.accounts, "an account")
        if line.tax_code:
            _check(line.tax_code in book.tax_codes, "a tax code")
    if doc.applies_to:
        target = book.documents.get(doc.applies_to)
        _check(target is not None and target.source_type == NOTE_OF[doc.source_type], "the invoice it settles")
    _price_lines(book, doc.currency, doc.lines, doc.include_tax)


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
        if line.document:
            doc = book.documents.get(line.document)
            _check(doc is not None and doc.source_type == wanted_doc and doc.contact == line.contact,
                   "the document it settles")
    for line in s.other_lines:
        _check(line.account in book.accounts, "an account")
        if line.account in (AR, AP):
            _check(line.contact in book.contacts, "a customer or supplier")
        if line.tax_code:
            _check(line.tax_code in book.tax_codes, "a tax code")
    if any(line.net <= 0 for line in s.party_lines):
        raise Blocked("unsupported feature", "A zero or negative customer or supplier amount.")
    _price_lines(book, s.currency, s.other_lines, s.include_tax)


def _resolve_journal(book: Book, j: Journal) -> None:
    for line in j.lines:
        _check(line.account in book.accounts, "an account")
        if line.account in (AR, AP):
            _check(line.contact in book.contacts, "a customer or supplier")


def _foreign_check(book: Book, name: str, key: str) -> None:
    record = (book.documents.get(key) or book.settlements.get(key) or book.journals.get(key))
    currencies: list[str | None] = []
    if record is not None:
        currencies.append(record.currency)
    elif key in book.transfers:
        t = book.transfers[key]
        currencies += [book.accounts[t.from_bank].currency, book.accounts[t.to_bank].currency]
    if not any(book.is_foreign(c) for c in currencies):
        return
    if name not in FX_PROVEN:
        raise Blocked("foreign currency", "Foreign currency treatment of this record type is not proven yet.")
    if getattr(record, "rate", None) is None:
        raise Blocked("foreign currency", "A foreign currency record without a stored exchange rate.")


def _resolve(book: Book) -> None:
    names = book.names
    for account in book.accounts.values():
        if account.account_type is None:
            account.account_type = _account_type(book, account)
            if account.account_type is None:
                book.block(account.source_type, account.key, "unresolved group",
                           "The account's group does not lead to a statement section.")
        if account.currency is not None and account.currency not in book.currencies:
            book.block(account.source_type, account.key, "unknown reference", "Refers to a currency not in the file.")
    for contact in book.contacts.values():
        if contact.currency is not None and contact.currency not in book.currencies:
            book.block(contact.source_type, contact.key, "unknown reference", "Refers to a currency not in the file.")
    for rate in book.rates:
        if rate.currency not in book.currencies:
            book.block("ExchangeRate", rate.key, "unknown reference", "Refers to a currency not in the file.")
    for tax in book.tax_codes.values():
        if tax.account not in book.accounts:
            book.block("TaxCode", tax.key, "unknown reference", "Refers to an account not in the file.")

    steps: list[tuple[dict, Callable[[Book, object], None]]] = [
        (book.documents, _resolve_document), (book.settlements, _resolve_settlement), (book.journals, _resolve_journal),
    ]
    for records, resolve in steps:
        for key, record in list(records.items()):
            try:
                resolve(book, record)
                _foreign_check(book, names[key], key)
            except Blocked as blocked:
                book.block(names[key], key, blocked.reason, blocked.note)
    for key, t in book.transfers.items():
        try:
            for bank in (t.from_bank, t.to_bank):
                _check(bank in book.accounts and book.accounts[bank].control == "bank", "a bank or cash account")
            _foreign_check(book, "InterAccountTransfer", key)
        except Blocked as blocked:
            book.block("InterAccountTransfer", key, blocked.reason, blocked.note)
    # A settlement that pays a blocked document, or a note on a blocked invoice, cannot stand alone.
    for key, s in book.settlements.items():
        if not book.is_blocked(key) and any(ln.document and book.is_blocked(ln.document) for ln in s.party_lines):
            book.block(s.source_type, key, "blocked document", "Settles a document that cannot be migrated.")
    for key, doc in book.documents.items():
        if not book.is_blocked(key) and doc.applies_to and book.is_blocked(doc.applies_to):
            book.block(doc.source_type, key, "blocked document", "Settles a document that cannot be migrated.")
    if not book.base_code or len(book.base_code) != 3:
        key = book.base_key or GUIDS["BaseCurrency"]
        book.block("BaseCurrency", key, "missing", "The business has no base currency code.")


def _receipt_verdicts(book: Book) -> None:
    """A settlement with lines other than customer or supplier balances keeps them as a journal."""
    for key, s in book.settlements.items():
        if book.is_blocked(key) or not s.other_lines:
            continue
        book.accept(s.source_type, key, label=f"{s.source_type} (journal fallback)",
                    klass=CoverageClass.MAPPED_WITH_LOSS, target="journal",
                    note="Lines not settling a customer or supplier balance are carried as a journal entry.")


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
        book.names[key] = name
        book.accept(name, key)
        if CONTENT_TYPES[ctype] not in (CoverageClass.MAPPED, CoverageClass.MAPPED_WITH_LOSS):
            continue
        try:
            if row.content is None:
                raise DecodeError(f"Object payload larger than {reader.max_object_bytes} bytes.")
            _decode_object(book, name, key, decode(row.content))
        except DecodeError as exc:
            book.block(name, key, "unreadable", f"Could not be read: {exc}")
        except Blocked as blocked:
            book.block(name, key, blocked.reason, blocked.note)
    for key, (account_type, control, label) in BUILTINS.items():
        book.accounts.setdefault(key, Account(key, TYPE_NAMES[key], label, None, None, account_type, control))
    _resolve(book)
    _receipt_verdicts(book)
    book.history = {"changes": reader.row_count("Changes"), "emails": reader.row_count("Emails")}
    return book
