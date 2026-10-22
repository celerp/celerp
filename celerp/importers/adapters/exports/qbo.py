# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read a QuickBooks Online export set into a `Book`.

The set is what QuickBooks Online's lists and reports export as workbooks: the Account
List, the Customer and Vendor Contact Lists, the Product/Service List, the Journal report
(one block per transaction), Sales by Customer Detail and Purchases by Vendor Detail
(one row per invoice or bill line), Invoices and Received Payments and Bills and Applied
Payments (each invoice or bill followed by what was applied to it), a Trial Balance for
each financial year's end and the last ledger date, and A/R and A/P Aging Detail as of
the last ledger date. The transaction reports may be exported a year at a time; the
files of one report are read in date order and their periods must join. QuickBooks
Online keeps no drafts and exports no deleted records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from celerp.importers.adapters.base import ArtifactSet, ScanError
from celerp.importers.adapters.exports import steps
from celerp.importers.adapters.exports.book import (
    ZERO, Account, Book, Contact, DocLine, Document, GLJournal, GLLine, Item, TaxCode, one_tax,
)
from celerp.importers.adapters.exports.files import (
    DateReader, Layout, SetCheck, Stranger, Table, amount, as_at, check_set, company_name, money, normalize, of,
    period, read_set,
)
from celerp.importers.adapters.exports.ledger import find_document
from ui.i18n import t

SOURCE_SYSTEM = "quickbooks_online"

SOURCE_NAME = "QuickBooks Online"
WARNING_KEY = "migration.export_warning_qbo"


def _report(title: str) -> str:
    return f"Reports > {title} > Export to Excel"


def _yearly(kind: str, label: str, columns: frozenset[str]) -> Layout:
    """A transaction report: every date, in one file or a file per period that join."""
    return Layout(kind, label, columns, title=label, many=True, joined=True, menu=_report(label),
                  range_key="migration.export_range_by_year")


ACCOUNTS = Layout("accounts", "Account List", frozenset({"account", "type", "detail type"}), title="Account List",
                  menu=_report("Account List"))
CUSTOMERS = Layout("customers", "Customer Contact List", frozenset({"customer", "email"}),
                   title="Customer Contact List", menu=_report("Customer Contact List"))
VENDORS = Layout("vendors", "Vendor Contact List", frozenset({"vendor", "email"}), title="Vendor Contact List",
                 menu=_report("Vendor Contact List"))
PRODUCTS = Layout("products", "Product/Service List", frozenset({"product/service", "type"}),
                  title="Product/Service List", menu=_report("Product/Service List"), optional=True)
JOURNAL = _yearly("journal", "Journal", frozenset({
    "date", "transaction type", "num", "name", "account", "debit", "credit"}))
_LINES = frozenset({"date", "transaction type", "num", "product/service", "qty", "amount", "account"})
SALES = _yearly("sales", "Sales by Customer Detail", _LINES | {"customer"})
PURCHASES = _yearly("purchases", "Purchases by Vendor Detail", _LINES | {"vendor"})
RECEIVED = _yearly("received", "Invoices and Received Payments", frozenset({
    "date", "transaction type", "num", "customer", "amount"}))
APPLIED = _yearly("applied", "Bills and Applied Payments", frozenset({
    "date", "transaction type", "num", "vendor", "amount"}))
TRIAL_BALANCE = Layout("trial_balance", "Trial Balance", frozenset({"debit", "credit"}), title="Trial Balance",
                       many=True, menu=_report("Trial Balance"), range_key="migration.export_range_tb_year_end")
AR_AGING = Layout("ar_aging", "A/R Aging Detail", frozenset({"transaction type", "num", "customer", "open balance"}),
                  title="A/R Aging Detail", menu=_report("A/R Aging Detail"),
                  range_key="migration.export_range_last_day")
AP_AGING = Layout("ap_aging", "A/P Aging Detail", frozenset({"transaction type", "num", "vendor", "open balance"}),
                  title="A/P Aging Detail", menu=_report("A/P Aging Detail"),
                  range_key="migration.export_range_last_day")
LAYOUTS = (ACCOUNTS, CUSTOMERS, VENDORS, PRODUCTS, JOURNAL, SALES, PURCHASES, RECEIVED, APPLIED, TRIAL_BALANCE,
           AR_AGING, AP_AGING)

# QuickBooks Online account type -> (account type, control); a detail type can name the control.
ACCOUNT_TYPES = {
    "bank": ("asset", "bank"), "accounts receivable (a/r)": ("asset", "receivable"),
    "accounts receivable": ("asset", "receivable"), "other current assets": ("asset", None),
    "fixed assets": ("asset", None), "other assets": ("asset", None),
    "accounts payable (a/p)": ("liability", "payable"), "accounts payable": ("liability", "payable"),
    "credit card": ("liability", None), "other current liabilities": ("liability", None),
    "long term liabilities": ("liability", None), "equity": ("equity", None), "income": ("revenue", None),
    "other income": ("revenue", None), "cost of goods sold": ("cogs", None), "expenses": ("expense", None),
    "other expense": ("expense", None),
}
DETAIL_CONTROLS = {"retained earnings": "retained_earnings", "exchange gain or loss": "exchange_difference",
                   "sales tax payable": "tax", "vat control": "tax", "global tax payable": "tax",
                   "inventory": "inventory"}
# QuickBooks Online transaction type -> the journal kind the ledger reads.
JOURNAL_KINDS = {
    "invoice": "receivable invoice", "credit memo": "receivable credit note", "bill": "payable invoice",
    "vendor credit": "payable credit note", "payment": "receivable payment", "bill payment (check)": "payable payment",
    "bill payment (credit card)": "payable payment", "transfer": "bank transfer",
}
DOCUMENT_TYPES = {"invoice": "invoice", "credit memo": "credit_note", "bill": "bill", "vendor credit": "debit_note"}
NOTES = ("credit_note", "debit_note")
VOIDED = "voided"


class _Accounts:
    """Reports name an account as its number and name, or its name alone."""

    def __init__(self, book: Book) -> None:
        self.by_label = {}
        for a in book.accounts.values():
            self.by_label.update({normalize(a.code): a.code, normalize(a.name): a.code, normalize(a.label): a.code})

    def __call__(self, text: str) -> str | None:
        return self.by_label.get(normalize(text))


def _required(tables: list[Table]) -> list[Layout]:
    """The files a set needs: the account list, journal and trial balances always; the
    contact list, line detail, applied payments and aging report for each side the
    journal posts invoices or bills on."""
    required = [ACCOUNTS, JOURNAL, TRIAL_BALANCE]
    kinds = {JOURNAL_KINDS.get(normalize(tb.value(row, "transaction type")), "")
             for tb in of(tables, JOURNAL) for row in tb.rows}
    if kinds & {"receivable invoice", "receivable credit note"}:
        required += [CUSTOMERS, SALES, RECEIVED, AR_AGING]
    if kinds & {"payable invoice", "payable credit note"}:
        required += [VENDORS, PURCHASES, APPLIED, AP_AGING]
    return required


def _accounts(book: Book, tables: list[Table]) -> None:
    for tb in of(tables, ACCOUNTS):
        for row in tb.rows:
            name, kind = tb.value(row, "account"), normalize(tb.value(row, "type"))
            if not name:
                continue
            code = tb.value(row, "account #") or name
            if kind not in ACCOUNT_TYPES:
                raise ScanError(t("migration.err_export_account_type", account=f"{code} {name}".strip(),
                                  type=tb.value(row, "type")))
            account_type, control = ACCOUNT_TYPES[kind]
            control = control or DETAIL_CONTROLS.get(normalize(tb.value(row, "detail type")))
            book.accounts[code] = Account(code, name, account_type, control)


def _contacts(book: Book, tables: list[Table]) -> None:
    for layout, column, address, role in ((CUSTOMERS, "customer", "billing address", "customer"),
                                          (VENDORS, "vendor", "address", "supplier")):
        for tb in of(tables, layout):
            for row in tb.rows:
                name = tb.value(row, column)
                if not name:
                    continue
                contact = book.contacts.setdefault(name, Contact(name))
                contact.email = contact.email or tb.value(row, "email") or None
                contact.phone = contact.phone or tb.value(row, "phone numbers") or None
                contact.address = contact.address or tb.value(row, address) or None
                contact.tax_id = contact.tax_id or tb.value(row, "tax id") or None
                contact.currency = contact.currency or tb.value(row, "currency").upper() or None
                contact.roles.add(role)


def _items(book: Book, tables: list[Table]) -> dict[str, str]:
    """Items keyed by SKU, or by name where none is set; returns name -> key."""
    names = {}
    for tb in of(tables, PRODUCTS):
        for row in tb.rows:
            name = tb.value(row, "product/service")
            if not name:
                continue
            code = tb.value(row, "sku") or name
            price = tb.value(row, "price")
            book.items[code] = Item(code, name, tb.value(row, "description") or None,
                                    money(price, name) if price else None,
                                    normalize(tb.value(row, "status") or "active") != "inactive")
            names[name] = code
    return names


def _journal(book: Book, tables: list[Table], read_date: DateReader, account: _Accounts) -> None:
    """One block per transaction: its date, type, number and name on the first row, one
    account per row, then a totals row with no account."""
    missing: set[str] = set()
    current: GLJournal | None = None
    for tb, row in ((tb, row) for tb in sorted(of(tables, JOURNAL), key=period) for row in tb.rows):
        if tb.value(row, "date"):
            where = f"{tb.name} {tb.value(row, 'date')} {tb.value(row, 'num')}".strip()
            source = tb.value(row, "transaction type")
            number = str(len(book.journals) + 1)
            current = GLJournal(f"J:{number}", number, read_date(tb.value(row, "date"), where), source,
                                JOURNAL_KINDS.get(normalize(source), normalize(source)), tb.value(row, "num"),
                                tb.value(row, "num") if normalize(source) in DOCUMENT_TYPES else "",
                                tb.value(row, "name"), tb.value(row, "currency").upper(), [])
            book.journals.append(current)
        label = tb.value(row, "account")
        if current is None or not label:
            continue
        where = f"{tb.name} {current.reference or current.number}"
        code = account(label)
        if code is None:
            missing.add(label)
            continue
        current.currency = current.currency or tb.value(row, "currency").upper()
        base = money(tb.value(row, "debit"), where) - money(tb.value(row, "credit"), where)
        foreign = tb.value(row, "foreign amount")
        fx = abs(money(foreign, where)) * (1 if base >= 0 else -1) if foreign else None
        if base or fx:
            current.lines.append(GLLine(code, base, fx, tb.value(row, "memo/description")))
    if missing:
        raise ScanError(t("migration.err_export_account_missing", accounts=", ".join(sorted(missing))))
    book.journals.sort(key=lambda j: (j.date, int(j.number)))


def _documents(book: Book, tables: list[Table], read_date: DateReader, account: _Accounts,
               items: dict[str, str]) -> None:
    """Invoices, credit memos, bills and vendor credits, one row per line. Other sales and
    purchases these reports list (sales receipts, expenses, cheques) are journals."""
    for layout, party, price, sales in ((SALES, "customer", "sales price", True),
                                        (PURCHASES, "vendor", "rate", False)):
        groups: dict[tuple[str, str], list[tuple[Table, dict[str, str]]]] = defaultdict(list)
        for tb in of(tables, layout):
            for row in tb.rows:
                if normalize(tb.value(row, "transaction type")) in DOCUMENT_TYPES and tb.value(row, "num"):
                    groups[(tb.value(row, "num"), tb.value(row, party))].append((tb, row))
        numbers = Counter(number for number, _ in groups)
        for (number, contact), rows in groups.items():
            tb, first = rows[0]
            where = f"{tb.name} {number}"
            doc_type = DOCUMENT_TYPES[normalize(tb.value(first, "transaction type"))]
            sign = -1 if doc_type in NOTES else 1
            currency = tb.value(first, "currency").upper()
            voided = all(normalize(tb.value(r, "memo/description")) == VOIDED for _, r in rows)
            lines = []
            for tb, row in rows:
                tax_code = tb.value(row, "tax code") or None
                if tax_code:
                    book.tax_codes.setdefault(tax_code, TaxCode(tax_code, ZERO))
                line_account = account(tb.value(row, "account"))
                if line_account is None:
                    raise ScanError(t("migration.err_export_account_missing", accounts=tb.value(row, "account")))
                foreign = tb.value(row, "foreign amount")
                net = sign * money(foreign if foreign else tb.value(row, "amount"), where)
                tracking = {k: tb.value(row, c) for k, c in (("Class", "class"), ("Location", "location"))
                            if tb.value(row, c)}
                book.tracking |= set(tracking)
                item = items.get(tb.value(row, "product/service")) or None
                lines.append(DocLine(item, "" if voided else tb.value(row, "memo/description"),
                                     sign * amount(tb.value(row, "qty"), where),
                                     amount(tb.value(row, price), where), None, net,
                                     one_tax(tax_code, sign * money(tb.value(row, "tax amount"), where)),
                                     line_account,
                                     tracking))
            key = f"{'S' if sales else 'P'}:{number}" + (f"|{contact}" if numbers[number] > 1 else "")
            tb = rows[0][0]
            due = tb.value(first, "due date")
            total = sum((ln.net + ln.tax for ln in lines), ZERO)
            book.documents[key] = Document(
                key=key, number=number, doc_type=doc_type, source_type=tb.value(first, "transaction type"),
                contact=contact, reference="", date=read_date(tb.value(first, "date"), where),
                due=read_date(due, where) if due else None, currency=currency,
                status="void" if voided else "posted", lines=lines, total=total,
                tax_total=sum((ln.tax for ln in lines), ZERO), paid=total, amount_due=ZERO, file=tb.name)
            book.contact(contact).roles.add("customer" if sales else "supplier")


def _settled_amount(book: Book, j: GLJournal) -> Decimal:
    """What a payment journal moved on the customer or supplier balance, in its own currency."""
    parties = book.controlled("receivable", "payable")
    return sum((abs(ln.foreign if ln.foreign is not None else ln.amount) for ln in j.lines if ln.account in parties),
               ZERO)


def _applied(book: Book, tables: list[Table], read_date: DateReader) -> None:
    """Link each payment journal to the invoices or bills it was applied to. A row matches the
    journal of its date, contact and amount; rows left over settle together the one journal
    left on their date and contact whose amount they add up to. A payment matched to none
    stays a journal. A credit memo or vendor credit listed under an invoice or bill applies to it."""
    for layout, party, sales, kind in ((RECEIVED, "customer", True, "receivable payment"),
                                       (APPLIED, "vendor", False, "payable payment")):
        waiting: dict[tuple[date, str, Decimal], list[GLJournal]] = defaultdict(list)
        leftover: dict[tuple[date, str], list[tuple[Document, Decimal]]] = defaultdict(list)
        for j in book.journals:
            if j.kind == kind:
                waiting[(j.date, j.contact, _settled_amount(book, j))].append(j)
        for tb in of(tables, layout):
            target: Document | None = None
            for row in tb.rows:
                where = f"{tb.name} {tb.value(row, 'num')}".strip()
                contact, kind_name = tb.value(row, party), normalize(tb.value(row, "transaction type"))
                doc_type = DOCUMENT_TYPES.get(kind_name)
                if doc_type in ("invoice", "bill"):
                    target = find_document(book, sales, tb.value(row, "num"), contact)
                elif doc_type in NOTES:
                    note = find_document(book, sales, tb.value(row, "num"), contact)
                    if note is not None and target is not None:
                        note.reference = target.number
                elif JOURNAL_KINDS.get(kind_name) == kind and target is not None and tb.value(row, "date"):
                    key = (read_date(tb.value(row, "date"), where), contact, abs(money(tb.value(row, "amount"), where)))
                    if waiting.get(key):
                        waiting[key].pop(0).settles = {target.key: None}
                    else:
                        leftover[key[:2]].append((target, key[2]))
        for (on, contact), rows in leftover.items():
            left = [j for (d, c, _), js in waiting.items() if (d, c) == (on, contact) for j in js]
            if len(left) == 1 and sum(a for _, a in rows) == _settled_amount(book, left[0]):
                settles: dict[str, Decimal | None] = defaultdict(lambda: ZERO)
                for doc, amount in rows:
                    settles[doc.key] += amount
                left[0].settles = dict(settles)


def _trial_balances(book: Book, tables: list[Table], account: _Accounts) -> None:
    """One trial balance per file, as of its period's last day, profit and loss from the
    period's first day: the first day of its financial year. Each earlier year needs one as
    of its last day, and the latest must reach the last ledger date."""
    years: dict[date, dict[date, dict[str, Decimal]]] = defaultdict(dict)
    seen: dict[date, str] = {}
    missing: set[str] = set()
    for tb in of(tables, TRIAL_BALANCE):
        start, on = period(tb)
        if on in seen:
            raise ScanError(t("migration.err_export_twice", label=TRIAL_BALANCE.label, files=f"{seen[on]}, {tb.name}"))
        seen[on] = tb.name
        balances: dict[str, Decimal] = {}
        for row in tb.rows:
            label = tb.value(row, "")
            if not label or normalize(label) == "total":
                continue
            code = account(label)
            if code is None:
                missing.add(label)
                continue
            balances[code] = money(tb.value(row, "debit"), tb.name) - money(tb.value(row, "credit"), tb.name)
        years[start][on] = balances
    if missing:
        raise ScanError(t("migration.err_export_account_missing", accounts=", ".join(sorted(missing))))
    starts = sorted(years)
    needed = [following - timedelta(days=1) for following in starts[1:]]
    if book.journals and (not seen or max(seen) < book.journals[-1].date):
        needed.append(book.journals[-1].date)
    absent = [on for on in needed if on not in seen]
    if absent:
        raise ScanError(t("migration.err_export_tb_year_end", label=TRIAL_BALANCE.label,
                          dates=", ".join(steps.show(on) for on in absent)))
    steps.roll_up_trial_balances(book, list(years.values()))


def _aging(book: Book, tables: list[Table]) -> None:
    """Per-contact balances and each open invoice or bill's amount due, as of the report date."""
    for layout, party, side, sales in ((AR_AGING, "customer", "receivable", True),
                                       (AP_AGING, "vendor", "payable", False)):
        for tb in of(tables, layout):
            on = as_at(tb)
            steps.refuse_stale_aged(book, layout.label, on)
            owed: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for row in tb.rows:
                contact = tb.value(row, party)
                if not contact:
                    continue
                home = money(tb.value(row, "open balance"), tb.name)
                owed[contact] += home if sales else -home
                doc = find_document(book, sales, tb.value(row, "num"), contact) if tb.value(row, "num") else None
                if doc is not None:
                    foreign = tb.value(row, "foreign open balance")
                    doc.amount_due = abs(money(foreign, tb.name) if foreign and book.is_foreign(doc.currency) else home)
                    doc.paid = doc.total - doc.amount_due
            book.aged[side] = (on, dict(owed))


def _dates(tables: list[Table]) -> DateReader:
    samples = [tb.value(row, c) for layout in (JOURNAL, SALES, PURCHASES) for tb in of(tables, layout)
               for row in tb.rows for c in ("date", "due date")]
    return DateReader([s for s in samples if s])


def _check(tables: list[Table], strangers: list[Stranger]) -> SetCheck:
    return check_set(tables, strangers, LAYOUTS, _required(tables), decider=JOURNAL, source=SOURCE_NAME,
                     warning_key=WARNING_KEY)


def check(artifacts: ArtifactSet) -> SetCheck:
    """The upload checked against the QuickBooks Online export checklist."""
    return _check(*read_set(artifacts, LAYOUTS))


def read_qbo(artifacts: ArtifactSet) -> Book:
    tables, strangers = read_set(artifacts, LAYOUTS)
    _check(tables, strangers).refuse()
    read_date = _dates(tables)
    book = Book(SOURCE_SYSTEM)
    _accounts(book, tables)
    account = _Accounts(book)
    _contacts(book, tables)
    items = _items(book, tables)
    _journal(book, tables, read_date, account)
    book.base = steps.base_currency(book)
    for j in book.journals:
        j.currency = j.currency or book.base
    _documents(book, tables, read_date, account, items)
    for doc in book.documents.values():
        doc.currency = doc.currency or book.base
    _applied(book, tables, read_date)
    steps.bank_currencies(book)
    _trial_balances(book, tables, account)
    _aging(book, tables)
    steps.roles_from_ledger(book)
    steps.tax_rates(book)
    book.company_name = company_name(tables)
    book.object_counts.update({"Account": len(book.accounts), "Contact": len(book.contacts), "Item": len(book.items),
                               "Invoice or bill": len(book.documents), "Journal": len(book.journals)})
    return book


def matches(artifacts: ArtifactSet) -> bool:
    """Whether any file of the upload is laid out as a QuickBooks Online export: the journal,
    the account list, a line detail report or a trial balance."""
    tables, _ = read_set(artifacts, LAYOUTS)
    return any(tb.layout in (JOURNAL, ACCOUNTS, SALES, PURCHASES, TRIAL_BALANCE) for tb in tables)
