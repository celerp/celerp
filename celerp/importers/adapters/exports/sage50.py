# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read a Sage 50 (US edition, also sold as Peachtree) export set into a `Book`.

Sage 50 keeps its books in a database only its own engine reads, so the set is what its
File > Select Import/Export templates write, with Include Headings ticked: the Chart of
Accounts List, the Customer, Vendor, Inventory Item and Employee Lists, and the General,
Sales, Cash Receipts, Purchase and Cash Disbursements Journals; with a General Ledger
Trial Balance as of the last day of each fiscal year and of the last transaction, and the
Aged Receivables and Aged Payables reports as of that day, exported from Reports & Forms.
Peachtree 2010 onward and current Sage 50 write the same templates; the columns an older
version heads differently are read under the current names (ALIASES).

Sage 50 differs from the other export sets in four ways, each read here:

- There is no general ledger export. The five journals are the ledger: every transaction
  is one row per distribution line, consecutive rows sharing the transaction's header
  fields, as many as its Number of Distributions says. Each amount is the line's effect
  on its account, debit positive, and a transaction's lines and its customer, vendor or
  cash line balance; a transaction that does not is refused as damaged.
- Journals name customers, vendors and accounts by their IDs; the lists give the names.
- Sales tax is a distribution of its own per tax agency (Tax Type 0), not a line's tax.
  Which tax types are taxable is set in the company's item defaults, which are not
  exported, so the tax is spread over the invoice's lines (by the one allocation of an
  amount over parts) only when they share one tax type; otherwise it stays one tax line on
  the invoice, listed in the coverage. The invoice keeps its total tax either way.
- The exports state no currency. The user chooses it from the currencies Celerp keeps books
  in (CURRENCY_QUESTION); the one a sign the reports print names, such as $ or Br, is suggested.

A trial balance dated before the first transaction holds the books' beginning balances
and is carried as one journal on that day. Templates Sage 50 can leave empty (a business
that never entered a purchase) are optional; one not uploaded is listed in the coverage
with what it means for the import.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from celerp.importers.adapters.base import ArtifactSet, MappingQuestion, MigrationDecisions, ScanError
from celerp.importers.adapters.exports import steps
from celerp.importers.adapters.exports.book import (
    PARTY_SIGN, ZERO, Account, Book, Contact, DocLine, Document, GLJournal, GLLine, Item, LineTax, TaxCode,
)
from celerp.importers.adapters.exports.files import (
    CURRENCY_SIGNS, DateReader, Layout, SetCheck, Stranger, Table, amount, as_at, check_set, company_name, money,
    normalize, of, read_set,
)
from celerp.importers.adapters.exports.ledger import find_document
from celerp.services import currencies
from celerp.services.money import allocate_pro_rata
from ui.i18n import t

SOURCE_SYSTEM = "sage50"
SOURCE_NAME = "Sage 50 / Peachtree"
WARNING_KEY = "migration.export_warning_sage50"
CURRENCY_QUESTION = "currency"
EMPLOYEE = "Employee"
STOCK = "Stock item quantity on hand"
UNASSIGNED_TAX = "Invoice with its sales tax on one line"
# A distribution's Tax Type: 0 is a tax distribution. Which of the others are taxable is set in
# each company's own item defaults, which Sage 50 does not export.
TAX_DISTRIBUTION = "0"
# Sage 50 accepts at most this many distribution lines on one transaction.
MAX_DISTRIBUTIONS = 560


def _template(area: str, template: str) -> str:
    return f"File > Select Import/Export > {area} > {template} > Export"


def _report(area: str, title: str) -> str:
    return f"Reports & Forms > {area} > {title} > Excel"


def _journal(kind: str, label: str, columns: set[str], area: str) -> Layout:
    """A journal template: every transaction, in one file."""
    return Layout(kind, label, frozenset(columns | {"date", "number of distributions", "g/l account", "amount"}),
                  menu=_template(area, label), range_key="migration.export_range_ledger", optional=True)


CHART = Layout("chart", "Chart of Accounts List", frozenset({"account id", "account description", "account type"}),
               menu=_template("General Ledger", "Chart of Accounts List"))
CUSTOMERS = Layout("customers", "Customer List", frozenset({"customer id", "customer name"}),
                   menu=_template("Accounts Receivable", "Customer List"), optional=True)
VENDORS = Layout("vendors", "Vendor List", frozenset({"vendor id", "vendor name"}),
                 menu=_template("Accounts Payable", "Vendor List"), optional=True)
ITEMS = Layout("items", "Inventory Item List", frozenset({"item id", "item description"}),
               menu=_template("Inventory", "Item List"), optional=True)
EMPLOYEES = Layout("employees", "Employee List", frozenset({"employee id", "employee name"}),
                   menu=_template("Payroll", "Employee List"), optional=True)
GENERAL = _journal("general", "General Journal", {"reference"}, "General Ledger")
SALES = _journal("sales", "Sales Journal", {"customer id", "invoice/cm #", "accounts receivable account"},
                 "Accounts Receivable")
RECEIPTS = _journal("receipts", "Cash Receipts Journal", {"customer id", "reference", "cash account", "cash amount"},
                    "Accounts Receivable")
PURCHASES = _journal("purchases", "Purchase Journal", {"vendor id", "invoice/cm #", "accounts payable account"},
                     "Accounts Payable")
PAYMENTS = _journal("payments", "Cash Disbursements Journal", {"vendor id", "check number", "cash account",
                                                               "cash amount"}, "Accounts Payable")
TRIAL_BALANCE = Layout("trial_balance", "General Ledger Trial Balance",
                       frozenset({"account id", "account description", "debit amt", "credit amt"}),
                       title="General Ledger Trial Balance", many=True,
                       menu=_report("General Ledger", "General Ledger Trial Balance"),
                       range_key="migration.export_range_sage50_trial_balance")
AGED_RECEIVABLES = Layout("aged_receivables", "Aged Receivables", frozenset({"customer id", "invoice/cm #", "amount due"}),
                          title="Aged Receivables", menu=_report("Accounts Receivable", "Aged Receivables"),
                          range_key="migration.export_range_last_day", optional=True)
AGED_PAYABLES = Layout("aged_payables", "Aged Payables", frozenset({"vendor id", "invoice/cm #", "amount due"}),
                       title="Aged Payables", menu=_report("Accounts Payable", "Aged Payables"),
                       range_key="migration.export_range_last_day", optional=True)
LAYOUTS = (CHART, CUSTOMERS, VENDORS, ITEMS, EMPLOYEES, GENERAL, SALES, RECEIPTS, PURCHASES, PAYMENTS, TRIAL_BALANCE,
           AGED_RECEIVABLES, AGED_PAYABLES)
JOURNALS = (GENERAL, SALES, RECEIPTS, PURCHASES, PAYMENTS)
# A journal not uploaded, as the coverage lists it, and what leaving it out means for the import.
ABSENT = "{} (not uploaded)"
ABSENT_NOTES = {GENERAL.kind: "migration.sage50_absent_general", SALES.kind: "migration.sage50_absent_sales",
                RECEIPTS.kind: "migration.sage50_absent_receipts", PURCHASES.kind: "migration.sage50_absent_purchases",
                PAYMENTS.kind: "migration.sage50_absent_payments"}

# Columns one template or release heads differently from another, read under one name: the
# Purchase Journal's "Invoice #" and "Apply to Invoice #" (the Sales Journal's "Invoice/CM #" and
# "Apply to Invoice Number"), and the names older Peachtree releases and reports write.
ALIASES = {"invoice #": "invoice/cm #", "apply to invoice #": "apply to invoice number", "check #": "check number",
           "gl account": "g/l account", "sales tax agency id": "sales tax agency", "debit amount": "debit amt",
           "credit amount": "credit amt"}

# Sage 50 account type, as the template writes it (a number) or as the chart shows it
# (a name) -> (account type, control).
ACCOUNT_TYPES = {
    "0": ("asset", "bank"), "1": ("asset", "receivable"), "2": ("asset", "inventory"), "4": ("asset", None),
    "5": ("asset", None), "6": ("asset", None), "8": ("asset", None), "10": ("liability", "payable"),
    "12": ("liability", None), "14": ("liability", None), "16": ("equity", None),
    "18": ("equity", "retained_earnings"), "19": ("equity", None), "21": ("revenue", None), "23": ("cogs", None),
    "24": ("expense", None),
    "cash": ("asset", "bank"), "accounts receivable": ("asset", "receivable"), "inventory": ("asset", "inventory"),
    "other current assets": ("asset", None), "fixed assets": ("asset", None),
    "accumulated depreciation": ("asset", None), "other assets": ("asset", None),
    "accounts payable": ("liability", "payable"), "other current liabilities": ("liability", None),
    "long term liabilities": ("liability", None), "equity-doesn't close": ("equity", None),
    "equity-retained earnings": ("equity", "retained_earnings"), "equity-gets closed": ("equity", None),
    "income": ("revenue", None), "cost of sales": ("cogs", None), "expenses": ("expense", None),
}
# Item classes whose quantity on hand Sage 50 tracks; its exports do not state the quantity.
STOCK_CLASSES = frozenset({"stock item", "serialized stock item", "assembly", "serialized assembly"})
# The currency a sign printed in a report names. Sage 50 US prints $ unless the business set its own sign.
SIGN_CURRENCIES = {"$": "USD", "Br": "ETB", "ETB": "ETB", "ብር": "ETB", "£": "GBP", "€": "EUR"}
assert set(SIGN_CURRENCIES) <= set(CURRENCY_SIGNS)
# Journals of one day are read in this order, so a document is booked before what settles it.
ORDER = {"G": 0, "S": 1, "P": 2, "R": 3, "D": 4}


@dataclass
class SageBook(Book):
    absent: tuple[str, ...] = ()                # kinds of the journals not uploaded


def _true(text: str) -> bool:
    return normalize(text) in ("true", "yes", "1")


def _first(tb: Table, row: dict[str, str], *columns: str) -> str:
    """The first of `columns` the row fills: the same field under each version's name."""
    return next((v for v in (tb.value(row, c) for c in columns) if v), "")


def _required(tables: list[Table]) -> list[Layout]:
    """The files a set needs: the chart and trial balances always; a list for every customer
    or vendor the journals name by ID, and the aged report for each side that has invoices
    or purchases."""
    def names(layouts: tuple[Layout, ...], column: str) -> bool:
        return any(tb.value(row, column) for layout in layouts for tb in of(tables, layout) for row in tb.rows)

    required = [CHART, TRIAL_BALANCE]
    if names((SALES, RECEIPTS), "customer id"):
        required.append(CUSTOMERS)
    if names((SALES,), "customer id"):
        required.append(AGED_RECEIVABLES)
    if names((PURCHASES, PAYMENTS), "vendor id"):
        required.append(VENDORS)
    if names((PURCHASES,), "vendor id"):
        required.append(AGED_PAYABLES)
    return required


def _transactions(tb: Table, keys: tuple[str, ...]) -> Iterator[tuple[str, list[dict[str, str]]]]:
    """Each transaction of a journal: as many consecutive rows as its Number of Distributions
    says, sharing its header fields `keys`. Rows that do not are a damaged file."""
    rows, i = tb.rows, 0
    while i < len(rows):
        first = rows[i]
        head = tuple(tb.value(first, k) for k in keys)
        where = " ".join([tb.name, *(v for v in head if v)])
        text = tb.value(first, "number of distributions")
        count = amount(text, where)
        if count != count.to_integral_value() or not 1 <= count <= MAX_DISTRIBUTIONS:
            raise ScanError(t("migration.err_export_number", value=text, where=where))
        group = rows[i:i + int(count)]
        if len(group) < count or any(tuple(tb.value(r, k) for k in keys) != head for r in group):
            raise ScanError(t("migration.err_sage50_distributions", where=where, count=int(count),
                              label=tb.layout.label))
        yield where, group
        i += int(count)


def _unbalanced(where: str, label: str, difference: Decimal) -> ScanError:
    return ScanError(t("migration.err_sage50_unbalanced", where=where, difference=f"{abs(difference):,.2f}",
                       label=label))


def _balanced(where: str, label: str, lines: list[GLLine]) -> None:
    if difference := sum((ln.amount for ln in lines), ZERO):
        raise _unbalanced(where, label, difference)


def _merged(lines: list[GLLine]) -> list[GLLine]:
    """One line per account and contact: a receipt that pays several invoices credits the
    receivable once, as the ledger reads a settlement."""
    out: dict[tuple[str, str | None], GLLine] = {}
    for ln in lines:
        key = (ln.account, ln.contact)
        if key in out:
            out[key].amount += ln.amount
        else:
            out[key] = GLLine(ln.account, ln.amount, None, ln.description, contact=ln.contact)
    return [ln for ln in out.values() if ln.amount]


def suggested_currency(tables: list[Table]) -> str | None:
    """The currency the signs the reports print beside their amounts name, when they name one
    Celerp keeps books in."""
    found: set[str] = set()
    for layout, columns in ((TRIAL_BALANCE, ("debit amt", "credit amt")), (AGED_RECEIVABLES, ("amount due",)),
                            (AGED_PAYABLES, ("amount due",))):
        for tb in of(tables, layout):
            for row in tb.rows:
                for text in (tb.value(row, c).strip("()-+ ") for c in columns):
                    found |= {code for sign, code in SIGN_CURRENCIES.items()
                              if text.startswith(sign) or text.endswith(sign)}
    return found.pop() if len(found) == 1 and found <= currencies.CURRENCY_CODES else None


def chosen_currency(decisions: MigrationDecisions) -> str:
    """The currency the user chose for the books: an import is built only in a chosen one."""
    currency = decisions.mappings.get(CURRENCY_QUESTION)
    if not currency:
        raise ScanError(t("migration.err_sage50_currency"))
    return currency


def question(suggested: str | None) -> MappingQuestion:
    return MappingQuestion(CURRENCY_QUESTION, t("migration.sage50_currency_source"), "",
                           t("migration.sage50_currency_question"), currencies.CURRENCIES, suggested)


class _Reader:
    """Reads one export set: the lists first, then each journal, the trial balances and the aged reports."""

    def __init__(self, tables: list[Table], base: str) -> None:
        self.tables = tables
        self.book = SageBook(SOURCE_SYSTEM, base=base)
        self.read_date = DateReader([], day_first=False)   # Sage 50 writes month first: MM/DD/YY
        self.ids: dict[str, dict[str, str]] = {CUSTOMERS.kind: {}, VENDORS.kind: {}}
        self.missing_accounts: set[str] = set()
        self.missing_contacts: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.party_accounts: dict[str, str] = {}       # document key -> its receivable or payable account
        self.sequence = 0

    # ── Lists ────────────────────────────────────────────────────────────────

    def read_chart(self) -> None:
        for tb in of(self.tables, CHART):
            for row in tb.rows:
                code, name = tb.value(row, "account id"), tb.value(row, "account description")
                if not code:
                    continue
                kind = normalize(tb.value(row, "account type"))
                if kind not in ACCOUNT_TYPES:
                    raise ScanError(t("migration.err_export_account_type", account=f"{code} {name}".strip(),
                                      type=tb.value(row, "account type")))
                account_type, control = ACCOUNT_TYPES[kind]
                self.book.accounts[code] = Account(code, name or code, account_type, control,
                                                   active=not _true(tb.value(row, "inactive")))

    def read_contacts(self) -> None:
        """Customers and vendors, keyed by name as the other export sets key them; two records
        of one list sharing a name are told apart by their IDs."""
        for layout, prefix, role in ((CUSTOMERS, "customer", "customer"), (VENDORS, "vendor", "supplier")):
            rows = [(tb, row) for tb in of(self.tables, layout) for row in tb.rows
                    if tb.value(row, f"{prefix} id")]
            names = defaultdict(int)
            for tb, row in rows:
                names[tb.value(row, f"{prefix} name") or tb.value(row, f"{prefix} id")] += 1
            for tb, row in rows:
                ident = tb.value(row, f"{prefix} id")
                name = tb.value(row, f"{prefix} name") or ident
                key = name if names[name] == 1 else f"{name} ({ident})"
                self.ids[layout.kind][ident] = key
                bill = "bill to " if prefix == "customer" else ""
                address = ", ".join(v for v in (tb.value(row, f"{bill}{c}") for c in (
                    "address-line one", "address-line two", "city", "state", "zip", "country")) if v)
                contact = self.book.contacts.setdefault(key, Contact(key))
                contact.email = contact.email or _first(tb, row, f"{prefix} e-mail", "e-mail") or None
                contact.phone = contact.phone or _first(tb, row, "telephone 1", "telephone") or None
                contact.address = contact.address or address or None
                contact.tax_id = contact.tax_id or tb.value(row, "tax id number") or None
                contact.roles.add(role)

    def read_items(self) -> None:
        for tb in of(self.tables, ITEMS):
            for row in tb.rows:
                code = tb.value(row, "item id")
                if not code:
                    continue
                price = tb.value(row, "sales price 1")
                self.book.items[code] = Item(code, tb.value(row, "item description") or code,
                                             tb.value(row, "description for sales") or None,
                                             money(price, f"{tb.name} {code}") if price else None,
                                             not _true(tb.value(row, "inactive")))
                if normalize(tb.value(row, "item class")) in STOCK_CLASSES:
                    self.book.skipped[STOCK] += 1

    def read_employees(self) -> None:
        self.book.skipped[EMPLOYEE] += sum(1 for tb in of(self.tables, EMPLOYEES) for row in tb.rows
                                           if tb.value(row, "employee id"))

    # ── References ───────────────────────────────────────────────────────────

    def account(self, code: str) -> str:
        if code not in self.book.accounts:
            self.missing_accounts.add(code or "--")
        return code

    def contact(self, ident: str, sales: bool, label: str) -> str:
        """The contact a journal or report names by its ID; "" when it names none."""
        if not ident:
            return ""
        listed = CUSTOMERS if sales else VENDORS
        key = self.ids[listed.kind].get(ident)
        if key is None:
            self.missing_contacts[(label, listed.label)].add(ident)
            return ident
        return key

    def refuse_missing(self) -> None:
        if self.missing_accounts:
            raise ScanError(t("migration.err_export_account_missing", accounts=", ".join(sorted(self.missing_accounts))))
        if self.missing_contacts:
            raise ScanError(" ".join(t("migration.err_sage50_contact_missing", label=label, ids=", ".join(sorted(ids)),
                                       list=listed) for (label, listed), ids in sorted(self.missing_contacts.items())))

    def journal(self, prefix: str, on: date, source: str, kind: str, reference: str, contact: str,
                lines: list[GLLine], **extra) -> GLJournal:
        self.sequence += 1
        number = f"{prefix}{self.sequence}"
        j = GLJournal(f"J:{number}", number, on, source, kind, reference, extra.pop("invoice", ""), contact,
                      self.book.base, lines, **extra)
        self.book.journals.append(j)
        return j

    # ── Journals ─────────────────────────────────────────────────────────────

    def read_documents(self, layout: Layout, sales: bool) -> None:
        """Invoices and credit memos, or purchases and vendor credit memos, with their lines,
        tax and the journal that posts them."""
        party, side = ("customer id", "receivable") if sales else ("vendor id", "payable")
        letter = "S" if sales else "P"
        for tb in of(self.tables, layout):
            for where, rows in _transactions(tb, (party, "invoice/cm #", "date")):
                first = rows[0]
                credit = _true(tb.value(first, "credit memo"))
                doc_type = ("credit_note" if credit else "invoice") if sales else ("debit_note" if credit else "bill")
                sign = PARTY_SIGN[doc_type]
                contact = self.contact(tb.value(first, party), sales, layout.label)
                number = tb.value(first, "invoice/cm #")
                on = self.read_date(tb.value(first, "date"), where)
                distributions: list[GLLine] = []
                lines: list[DocLine] = []
                kinds: list[str] = []                    # each line's Tax Type
                taxes: dict[str, Decimal] = defaultdict(lambda: ZERO)
                for row in rows:
                    account = self.account(tb.value(row, "g/l account"))
                    posted = money(tb.value(row, "amount"), where)
                    description = tb.value(row, "description")
                    distributions.append(GLLine(account, posted, None, description))
                    kind = tb.value(row, "tax type")
                    agency = tb.value(row, "sales tax agency") if sales else ""
                    if agency or (sales and kind == TAX_DISTRIBUTION):
                        # A tax distribution names its agency; one that does not is the customer's tax code's.
                        agency = agency or tb.value(first, "sales tax id") or description or "--"
                        taxes[agency] += -sign * posted
                        tax = self.book.tax_codes.setdefault(agency, TaxCode(agency, ZERO, account))
                        if tax.account in self.book.accounts and self.book.accounts[tax.account].control is None:
                            self.book.accounts[tax.account].control = "tax"
                        continue
                    net = -sign * posted
                    quantity = abs(amount(tb.value(row, "quantity"), where))
                    item = tb.value(row, "item id") or None
                    if item and item not in self.book.items:
                        self.book.items[item] = Item(item, description or item)
                    lines.append(DocLine(item, description, quantity if net >= 0 else -quantity,
                                         abs(amount(tb.value(row, "unit price"), where)), None, net, [], account))
                    kinds.append(kind)
                # The customer or vendor line balances the distributions. Its stated amount, where the
                # template has it, is checked by size: the distributions carry the sign.
                party_line = -sum((ln.amount for ln in distributions), ZERO)
                stated = tb.value(first, f"accounts {side} amount")
                if stated and abs(money(stated, where)) != abs(party_line):
                    raise _unbalanced(where, layout.label, abs(money(stated, where)) - abs(party_line))
                self._spread_taxes(lines, kinds, taxes)
                # A number two contacts share keys the later one by its contact as well; an unnumbered
                # document (one never printed) by its journal.
                key = f"{letter}:{number}" if number else f"{letter}:#{self.sequence + 1}"
                if key in self.book.documents:
                    key = f"{key}|{contact}"
                if key in self.book.documents:
                    key = f"{key}|{self.sequence + 1}"
                total = sign * party_line
                source = {"invoice": "Sales invoice", "credit_note": "Credit memo", "bill": "Purchase",
                          "debit_note": "Vendor credit memo"}[doc_type]
                due = tb.value(first, "date due")
                self.book.documents[key] = Document(
                    key=key, number=number, doc_type=doc_type, source_type=source, contact=contact,
                    reference=tb.value(first, "apply to invoice number") if credit else "", date=on,
                    due=self.read_date(due, where) if due else None, currency=self.book.base, status="posted",
                    lines=lines, total=total, tax_total=sum((ln.tax for ln in lines), ZERO), paid=total,
                    amount_due=ZERO, file=tb.name)
                party_account = self.account(tb.value(first, f"accounts {side} account"))
                self.party_accounts[key] = party_account
                self.journal(letter, on, source, f"{side} {'credit note' if credit else 'invoice'}",
                             number, contact, [GLLine(party_account, party_line, None, number), *distributions],
                             invoice=number, document=key)

    def _spread_taxes(self, lines: list[DocLine], kinds: list[str], taxes: dict[str, Decimal]) -> None:
        """Each tax agency's line spread over the lines it was charged on, in proportion to them:
        the document's one charged line, or its charged lines when they share one Tax Type. Lines
        of different tax types may be taxable or exempt, which the export does not say, so their
        document keeps each tax as a line of its own and is listed in the coverage."""
        charged = [i for i, ln in enumerate(lines) if ln.net > 0]
        types = {kinds[i] for i in charged}
        spread = len(charged) == 1 or (len(types) == 1 and "" not in types)
        if any(taxes.values()) and not spread:
            self.book.skipped[UNASSIGNED_TAX] += 1
        weights = [ln.net if i in charged else ZERO for i, ln in enumerate(lines)]
        for agency, tax in taxes.items():
            if not tax:
                continue
            if not spread:
                lines.append(DocLine(None, agency, ZERO, ZERO, None, ZERO, [LineTax(agency, tax)],
                                     self.book.tax_codes[agency].account or ""))
                continue
            # Sage 50 keeps every amount at cents, whatever the currency.
            for ln, share in zip(lines, allocate_pro_rata(tax, weights, "")):
                if share:
                    ln.taxes.append(LineTax(agency, share))

    def read_cash(self, layout: Layout, sales: bool) -> None:
        """Receipts or payments: the cash line, its distributions and a discount taken, the
        invoices or purchases each distribution pays. Sage 50 leaves the G/L Account of a
        distribution applied to an invoice or purchase empty: it posts to that document's
        receivable or payable account."""
        party, reference = ("customer id", "reference") if sales else ("vendor id", "check number")
        discount_sign = 1 if sales else -1
        for tb in of(self.tables, layout):
            for where, rows in _transactions(tb, (party, reference, "date")):
                first = rows[0]
                contact = self.contact(tb.value(first, party), sales, layout.label)
                lines = [GLLine(self.account(tb.value(first, "cash account")), money(tb.value(first, "cash amount"), where),
                                None, "")]
                settles: dict[str, Decimal | None] = defaultdict(lambda: ZERO)
                for row in rows:
                    posted = money(tb.value(row, "amount"), where)
                    paid = tb.value(row, "invoice paid")
                    doc = find_document(self.book, sales, paid, contact) if paid and contact else None
                    account = self.account(tb.value(row, "g/l account")
                                           or (self.party_accounts[doc.key] if doc is not None else ""))
                    lines.append(GLLine(account, posted, None, tb.value(row, "description")))
                    discount = money(tb.value(row, "discount amount"), where)
                    if discount:
                        lines.append(GLLine(self.account(tb.value(first, "discount account")), discount_sign * discount,
                                            None, tb.value(row, "description")))
                    if doc is not None:
                        settles[doc.key] += -discount_sign * posted
                _balanced(where, layout.label, lines)
                settles = {k: v for k, v in settles.items() if v}
                kind = ("receivable payment" if sales else "payable payment") if settles else \
                    ("cash receipt" if sales else "cash disbursement")
                self.journal("R" if sales else "D", self.read_date(tb.value(first, "date"), where),
                             "Receipt" if sales else "Payment", kind, tb.value(first, reference), contact,
                             _merged(lines), settles=settles)

    def read_general(self) -> None:
        for tb in of(self.tables, GENERAL):
            for where, rows in _transactions(tb, ("date", "reference")):
                first = rows[0]
                lines = [GLLine(self.account(tb.value(r, "g/l account")), money(tb.value(r, "amount"), where), None,
                                tb.value(r, "description")) for r in rows]
                _balanced(where, GENERAL.label, lines)
                lines = [ln for ln in lines if ln.amount]
                banks = all(self.book.accounts.get(ln.account) and self.book.accounts[ln.account].control == "bank"
                            for ln in lines)
                self.journal("G", self.read_date(tb.value(first, "date"), where), "General journal entry",
                             "bank transfer" if banks and len(lines) == 2 else "general journal",
                             tb.value(first, "reference"), "", lines)

    # ── Reports ──────────────────────────────────────────────────────────────

    def read_trial_balances(self) -> None:
        """Balances at each trial balance's date. Each is as of the last day of a fiscal year,
        profit and loss for that year, earlier years' profit in retained earnings, except the
        latest, which must reach the last transaction. One dated before the first transaction
        holds the beginning balances, carried as a journal on its date."""
        years: list[dict[date, dict[str, Decimal]]] = []
        seen: dict[date, str] = {}
        missing: set[str] = set()
        for tb in of(self.tables, TRIAL_BALANCE):
            on = as_at(tb)
            if on in seen:
                raise ScanError(t("migration.err_export_twice", label=TRIAL_BALANCE.label,
                                  files=f"{seen[on]}, {tb.name}"))
            seen[on] = tb.name
            balances: dict[str, Decimal] = {}
            for row in tb.rows:
                code = tb.value(row, "account id")
                if not code or normalize(code).startswith("total"):
                    continue
                if code not in self.book.accounts:
                    missing.add(code)
                    continue
                balances[code] = money(tb.value(row, "debit amt"), tb.name) - money(tb.value(row, "credit amt"), tb.name)
            years.append({on: balances})
        if missing:
            raise ScanError(t("migration.err_export_account_missing", accounts=", ".join(sorted(missing))))
        journals = self.book.journals
        if journals and (not seen or max(seen) < journals[-1].date):
            raise ScanError(t("migration.err_sage50_tb_last", label=TRIAL_BALANCE.label,
                              date=steps.show(journals[-1].date)))
        steps.roll_up_trial_balances(self.book, years)
        first = journals[0].date if journals else date.max
        opening = max((on for on in seen if on < first), default=None)
        if opening is not None:
            lines = [GLLine(code, value, None, "") for code, value in self.book.trial_balances[opening].items() if value]
            if lines:
                journals.insert(0, GLJournal("J:opening", "0", opening, "Beginning balances", "beginning balances",
                                             "", "", "", self.book.base, lines))

    def read_aged(self) -> None:
        """Each customer's or vendor's open balance and each open document's amount due, as of
        the report date. The report names a customer or vendor on the first row of its block."""
        for layout, party, side, sales in ((AGED_RECEIVABLES, "customer id", "receivable", True),
                                           (AGED_PAYABLES, "vendor id", "payable", False)):
            for tb in of(self.tables, layout):
                on = as_at(tb)
                steps.refuse_stale_aged(self.book, layout.label, on)
                owed: dict[str, Decimal] = defaultdict(lambda: ZERO)
                current = ""
                for row in tb.rows:
                    current = tb.value(row, party) or current
                    number = tb.value(row, "invoice/cm #")
                    if not number or not current:
                        continue
                    contact = self.contact(current, sales, layout.label)
                    due = money(tb.value(row, "amount due"), tb.name)
                    owed[contact] += due if sales else -due
                    doc = find_document(self.book, sales, number, contact)
                    if doc is not None:
                        doc.amount_due = abs(due)
                        doc.paid = doc.total - doc.amount_due
                self.book.aged[side] = (on, dict(owed))


def _check(tables: list[Table], strangers: list[Stranger]) -> SetCheck:
    return check_set(tables, strangers, LAYOUTS, _required(tables), decider=CHART, source=SOURCE_NAME,
                     warning_key=WARNING_KEY)


def _tables(artifacts: ArtifactSet) -> tuple[list[Table], list[Stranger]]:
    return read_set(artifacts, LAYOUTS, ALIASES)


def check(artifacts: ArtifactSet) -> SetCheck:
    """The upload checked against the Sage 50 export checklist."""
    return _check(*_tables(artifacts))


def read_sage50(artifacts: ArtifactSet, currency: str | None) -> SageBook:
    """The set's books in `currency`, or in the currency its reports suggest when none is
    chosen yet (no currency when they suggest none)."""
    tables, strangers = _tables(artifacts)
    _check(tables, strangers).refuse()
    reader = _Reader(tables, currency or suggested_currency(tables) or "")
    book = reader.book
    reader.read_chart()
    reader.read_contacts()
    reader.read_items()
    reader.read_employees()
    reader.read_general()
    reader.read_documents(SALES, True)
    reader.read_documents(PURCHASES, False)
    reader.read_cash(RECEIPTS, True)
    reader.read_cash(PAYMENTS, False)
    book.journals.sort(key=lambda j: (j.date, ORDER[j.number[0]], int(j.number[1:])))
    reader.read_trial_balances()
    reader.read_aged()
    reader.refuse_missing()
    steps.roles_from_ledger(book)
    steps.tax_rates(book)
    book.company_name = company_name(tables)
    book.absent = tuple(layout.kind for layout in JOURNALS if not of(tables, layout))
    book.object_counts.update({"Account": len(book.accounts), "Contact": len(book.contacts), "Item": len(book.items),
                               "Invoice or bill": len(book.documents), "Journal": len(book.journals)})
    return book


def matches(artifacts: ArtifactSet) -> bool:
    """Whether any file of the upload is laid out as a Sage 50 export: the chart, a journal or a trial balance."""
    tables, _ = _tables(artifacts)
    return any(tb.layout in (CHART, *JOURNALS, TRIAL_BALANCE) for tb in tables)
