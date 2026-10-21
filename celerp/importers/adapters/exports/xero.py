# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read a Xero export set into a `Book`.

The set is what Xero's own export menus write: the chart of accounts, contacts and
inventory items, the invoices and bills (sales and purchases share one layout, in
batches), the General Ledger, a Trial Balance with a column per month end for each
financial year, and the Aged Receivables and Aged Payables Detail reports as at the
last day of the ledger.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from celerp.importers.adapters.base import ArtifactSet, ScanError
from celerp.importers.adapters.exports import steps
from celerp.importers.adapters.exports.book import (
    ZERO, Account, Book, Contact, DocLine, Document, GLJournal, GLLine, Item, TaxCode, one_tax,
)
from celerp.importers.adapters.exports.files import (
    DateReader, Layout, SetCheck, Stranger, Table, amount, as_at, check_set, company_name, money, normalize, of,
    read_set, report_date,
)
from celerp.importers.adapters.exports.ledger import SETTLEMENT_KINDS, find_document
from ui.i18n import t

SOURCE_SYSTEM = "xero"

SOURCE_NAME = "Xero"
WARNING_KEY = "migration.export_warning_xero"

CHART = Layout("chart", "Chart of accounts", frozenset({"code", "name", "type", "tax code"}),
               menu="Accounting > Chart of accounts > Export")
CONTACTS = Layout("contacts", "Contacts", frozenset({"contactname", "firstname", "lastname"}),
                  menu="Contacts > All contacts > Export")
ITEMS = Layout("items", "Inventory items", frozenset({"itemcode", "itemname"}),
               menu="Business > Products and services > Export", optional=True)
DOCUMENTS = Layout("documents", "Invoices and bills", frozenset({
    "contactname", "invoicenumber", "invoicedate", "total", "description", "quantity", "unitamount",
    "accountcode", "taxtype", "currency", "type", "status"}), many=True,
    menu="Business > Invoices > Export, and Business > Bills to pay > Export",
    range_key="migration.export_range_all_documents")
LEDGER = Layout("ledger", "General Ledger", frozenset({
    "journal number", "date", "source", "account code", "debit", "credit"}),
    menu="Accounting > Reports > General Ledger > Export", range_key="migration.export_range_ledger")
TRIAL_BALANCE = Layout("trial_balance", "Trial Balance", frozenset({"account code", "account"}),
                       title="Trial Balance", many=True, menu="Accounting > Reports > Trial Balance > Export",
                       range_key="migration.export_range_tb_months")
AGED_RECEIVABLES = Layout("aged_receivable", "Aged Receivables Detail", frozenset({"invoice number", "total"}),
                          title="Aged Receivables Detail",
                          menu="Accounting > Reports > Aged Receivables Detail > Export",
                          range_key="migration.export_range_last_day")
AGED_PAYABLES = Layout("aged_payable", "Aged Payables Detail", frozenset({"invoice number", "total"}),
                       title="Aged Payables Detail", menu="Accounting > Reports > Aged Payables Detail > Export",
                       range_key="migration.export_range_last_day")
LAYOUTS = (CHART, CONTACTS, ITEMS, DOCUMENTS, LEDGER, TRIAL_BALANCE, AGED_RECEIVABLES, AGED_PAYABLES)

# Xero account type -> (account type, control)
ACCOUNT_TYPES = {
    "bank": ("asset", "bank"), "accounts receivable": ("asset", "receivable"),
    "accounts payable": ("liability", "payable"), "vat": ("liability", "tax"), "gst": ("liability", "tax"),
    "sales tax": ("liability", "tax"), "retained earnings": ("equity", "retained_earnings"),
    "revenue": ("revenue", None), "sales": ("revenue", None), "other income": ("revenue", None),
    "direct costs": ("cogs", None), "expense": ("expense", None), "overhead": ("expense", None),
    "depreciation": ("expense", None), "current asset": ("asset", None), "fixed asset": ("asset", None),
    "non-current asset": ("asset", None), "prepayment": ("asset", None), "inventory": ("asset", "inventory"),
    "current liability": ("liability", None), "liability": ("liability", None),
    "non-current liability": ("liability", None), "term liability": ("liability", None),
    "equity": ("equity", None),
}
# Xero names its realised exchange account itself; a renamed one is found by these words.
FX_NAMES = ("currency gain", "exchange")
DOCUMENT_TYPES = {
    "sales invoice": "invoice", "accrec": "invoice", "sales credit note": "credit_note", "accreccredit": "credit_note",
    "bill": "bill", "accpay": "bill", "bill credit note": "debit_note", "accpaycredit": "debit_note",
}
DOCUMENT_STATUS = {"awaiting payment": "posted", "paid": "posted", "voided": "void", "draft": "draft",
                   "submitted": "draft", "awaiting approval": "draft"}
DELETED = "Deleted invoice or bill"


def _required(tables: list[Table]) -> list[Layout]:
    """The files a set needs: the ledger, chart, contacts and trial balances always; the
    invoices and bills and the aged report for each side the ledger posts customer or
    supplier balances on."""
    required = [CHART, CONTACTS, LEDGER, TRIAL_BALANCE]
    sources = {normalize(tb.value(row, "source")) for tb in of(tables, LEDGER) for row in tb.rows}
    if any(s.startswith("receivable") for s in sources):
        required += [DOCUMENTS, AGED_RECEIVABLES]
    if any(s.startswith("payable") for s in sources):
        required += [DOCUMENTS, AGED_PAYABLES]
    return list(dict.fromkeys(required))


def _accounts(book: Book, tables: list[Table]) -> None:
    for tb in of(tables, CHART):
        for row in tb.rows:
            code, name, kind = tb.value(row, "code"), tb.value(row, "name"), normalize(tb.value(row, "type"))
            if kind not in ACCOUNT_TYPES:
                raise ScanError(t("migration.err_export_account_type", account=f"{code} {name}",
                                  type=tb.value(row, "type")))
            account_type, control = ACCOUNT_TYPES[kind]
            if control is None and any(word in name.lower() for word in FX_NAMES) and account_type == "expense":
                control = "exchange_difference"
            book.accounts[code] = Account(code, name, account_type, control)
            if tax := tb.value(row, "tax code"):
                book.tax_codes.setdefault(tax, TaxCode(tax, ZERO))


def _contacts(book: Book, tables: list[Table]) -> None:
    for tb in of(tables, CONTACTS):
        for row in tb.rows:
            name = tb.value(row, "contactname")
            if not name:
                continue
            address = ", ".join(v for v in (tb.value(row, c) for c in ("poaddressline1", "pocity", "popostalcode",
                                                                        "pocountry")) if v)
            book.contacts[name] = Contact(name, email=tb.value(row, "emailaddress") or None,
                                          phone=tb.value(row, "phonenumber") or None, address=address or None,
                                          tax_id=tb.value(row, "taxnumber") or None,
                                          currency=tb.value(row, "defaultcurrency").upper() or None)


def _items(book: Book, tables: list[Table]) -> None:
    for tb in of(tables, ITEMS):
        for row in tb.rows:
            code = tb.value(row, "itemcode")
            if code:
                price = tb.value(row, "salesunitprice")
                book.items[code] = Item(code, tb.value(row, "itemname") or code,
                                        tb.value(row, "salesdescription") or None,
                                        money(price, code) if price else None,
                                        normalize(tb.value(row, "status") or "active") == "active")


def _tracking(tb: Table, row: dict[str, str], prefix: str) -> dict[str, str]:
    out = {}
    for n in (1, 2):
        name, option = tb.value(row, f"{prefix}name{n}"), tb.value(row, f"{prefix}option{n}")
        if name and option:
            out[name] = option
    return out


def _documents(book: Book, tables: list[Table], read_date: DateReader) -> None:
    """Invoices and bills, one row per line, grouped by number and contact. A number used by
    two contacts keys each by its contact; one document in two files is an overlap."""
    groups: dict[tuple[bool, str, str], list[tuple[Table, dict[str, str]]]] = {}
    files: dict[tuple[bool, str, str], str] = {}
    for tb in of(tables, DOCUMENTS):
        for row in tb.rows:
            kind = normalize(tb.value(row, "type"))
            if kind not in DOCUMENT_TYPES:
                raise ScanError(t("migration.err_export_doc_type", type=tb.value(row, "type"), file=tb.name))
            group = (DOCUMENT_TYPES[kind] in ("invoice", "credit_note"), tb.value(row, "invoicenumber"),
                     tb.value(row, "contactname"))
            if files.setdefault(group, tb.name) != tb.name:
                raise ScanError(t("migration.err_export_overlap", number=group[1],
                                  files=f"{files[group]}, {tb.name}"))
            groups.setdefault(group, []).append((tb, row))
    numbers = Counter((sales, number) for sales, number, _ in groups)
    for (sales, number, contact), rows in groups.items():
        tb, first = rows[0]
        status = normalize(tb.value(first, "status"))
        if status == "deleted":
            book.skipped[DELETED] += 1
            continue
        if status not in DOCUMENT_STATUS:
            raise ScanError(t("migration.err_export_doc_status", status=tb.value(first, "status"), number=number))
        where = f"{tb.name} {number}"
        lines = []
        for _, row in rows:
            item = tb.value(row, "inventoryitemcode") or None
            if item and item not in book.items:
                book.items[item] = Item(item, tb.value(row, "description") or item)
            tax_code = tb.value(row, "taxtype") or None
            if tax_code:
                book.tax_codes.setdefault(tax_code, TaxCode(tax_code, ZERO))
            tracking = _tracking(tb, row, "tracking")
            book.tracking |= set(tracking)
            quantity = amount(tb.value(row, "quantity"), where)
            unit = amount(tb.value(row, "unitamount"), where)
            discount = amount(tb.value(row, "discount"), where) or None
            net = money(tb.value(row, "lineamount"), where) if tb.value(row, "lineamount") else money(
                str(quantity * unit * (1 - (discount or ZERO) / 100)))
            lines.append(DocLine(item, tb.value(row, "description"), quantity, unit, discount, net,
                                 one_tax(tax_code, money(tb.value(row, "taxamount"), where)),
                                 tb.value(row, "accountcode"),
                                 tracking))
        total = money(tb.value(first, "total"), where)
        if sum((ln.net + ln.tax for ln in lines), ZERO) != total:
            raise ScanError(t("migration.err_export_doc_total", number=number, file=tb.name))
        doc_type = DOCUMENT_TYPES[normalize(tb.value(first, "type"))]
        key = f"{'S' if sales else 'P'}:{number}" + (f"|{contact}" if numbers[(sales, number)] > 1 else "")
        due = tb.value(first, "duedate")
        book.documents[key] = Document(
            key=key, number=number, doc_type=doc_type, source_type=tb.value(first, "type"), contact=contact,
            reference=tb.value(first, "reference"), date=read_date(tb.value(first, "invoicedate"), where),
            due=read_date(due, where) if due else None,
            currency=tb.value(first, "currency").upper(), status=DOCUMENT_STATUS[status], lines=lines,
            total=total, tax_total=money(tb.value(first, "taxtotal"), where),
            paid=money(tb.value(first, "invoiceamountpaid"), where),
            amount_due=money(tb.value(first, "invoiceamountdue"), where), file=tb.name)
        book.contact(contact).roles.add("customer" if sales else "supplier")


def _ledger(book: Book, tables: list[Table], read_date: DateReader) -> None:
    by_number: dict[str, list[dict[str, str]]] = defaultdict(list)
    tb = of(tables, LEDGER)[0]
    for row in tb.rows:
        by_number[tb.value(row, "journal number")].append(row)
    missing: dict[str, str] = {}
    for number, rows in by_number.items():
        first = rows[0]
        where = f"{tb.name} {number}"
        lines = []
        for row in rows:
            code = tb.value(row, "account code")
            if code not in book.accounts:
                missing[code] = tb.value(row, "account name")
            base = money(tb.value(row, "debit"), where) - money(tb.value(row, "credit"), where)
            foreign = tb.value(row, "foreign amount")
            fx = abs(money(foreign, where)) * (1 if base >= 0 else -1) if foreign else None
            tracking = _tracking(tb, row, "tracking ")
            tracking = {k: v for k, v in tracking.items()}
            book.tracking |= set(tracking)
            if base or fx:
                lines.append(GLLine(code, base, fx, tb.value(row, "description"), tracking))
        source = tb.value(first, "source")
        book.journals.append(GLJournal(
            f"J:{number}", number, read_date(tb.value(first, "date"), where), source, source.lower(),
            tb.value(first, "reference"), tb.value(first, "invoice number"), tb.value(first, "contact"),
            tb.value(first, "currency").upper(), lines))
    if missing:
        listed = ", ".join(f"{code} {name}".strip() for code, name in sorted(missing.items()))
        raise ScanError(t("migration.err_export_account_missing", accounts=listed))
    book.journals.sort(key=lambda j: (j.date, int(j.number) if j.number.isdigit() else 0, j.number))


def _trial_balances(book: Book, tables: list[Table]) -> None:
    """Balances from the first record at each month end. A financial year's trial balance
    states profit and loss for the year so far and carries earlier years' profit in retained
    earnings; each file is one financial year."""
    years = []
    seen: dict[date, str] = {}
    for tb in of(tables, TRIAL_BALANCE):
        columns = [(c, report_date(c)) for c in tb.header if c not in ("account code", "account")]
        for _, on in columns:
            if on in seen:
                raise ScanError(t("migration.err_export_twice", label=TRIAL_BALANCE.label,
                                  files=f"{seen[on]}, {tb.name}"))
            seen[on] = tb.name
        values: dict[date, dict[str, Decimal]] = {on: {} for _, on in columns}
        missing = {}
        for row in tb.rows:
            code = tb.value(row, "account code")
            if not code:
                continue
            if code not in book.accounts:
                missing[code] = tb.value(row, "account")
            for column, on in columns:
                values[on][code] = money(row.get(column, ""), tb.name)
        if missing:
            listed = ", ".join(f"{c} {n}" for c, n in sorted(missing.items()))
            raise ScanError(t("migration.err_export_account_missing", accounts=listed))
        years.append(dict(sorted(values.items())))
    steps.roll_up_trial_balances(book, years)
    _refuse_missing_months(book)


def _refuse_missing_months(book: Book) -> None:
    if not book.journals:
        return
    first, last = steps.month_end(book.journals[0].date), steps.month_end(book.journals[-1].date)
    missing_years = set()
    on = first
    while on <= last:
        if on not in book.trial_balances:
            missing_years.add(on.year)
        on = steps.month_end(date.fromordinal(on.toordinal() + 1))
    if missing_years:
        raise ScanError(t("migration.err_export_tb_missing", label=TRIAL_BALANCE.label,
                          years=", ".join(str(y) for y in sorted(missing_years))))


def _aged(book: Book, tables: list[Table]) -> None:
    for layout, side, sign in ((AGED_RECEIVABLES, "receivable", 1), (AGED_PAYABLES, "payable", -1)):
        for tb in of(tables, layout):
            on = as_at(tb)
            steps.refuse_stale_aged(book, layout.label, on)
            owed: dict[str, Decimal] = {}
            for row in tb.rows:
                first = tb.value(row, tb.header[0])
                if first.startswith("Total ") and first != "Total":
                    owed[first.removeprefix("Total ").strip()] = sign * money(tb.value(row, "total"), tb.name)
            book.aged[side] = (on, owed)


def _dates(tables: list[Table]) -> DateReader:
    samples = [tb.value(row, c) for tb in of(tables, DOCUMENTS) for row in tb.rows for c in ("invoicedate", "duedate")]
    samples += [tb.value(row, "date") for tb in of(tables, LEDGER) for row in tb.rows]
    return DateReader(samples)


def _check(tables: list[Table], strangers: list[Stranger]) -> SetCheck:
    return check_set(tables, strangers, LAYOUTS, _required(tables), decider=LEDGER, source=SOURCE_NAME,
                     warning_key=WARNING_KEY)


def check(artifacts: ArtifactSet) -> SetCheck:
    """The upload checked against the Xero export checklist."""
    return _check(*read_set(artifacts, LAYOUTS))


def _settles(book: Book) -> None:
    """A receipt or payment journal settles the invoice or bill its ledger lines name."""
    for j in book.journals:
        if j.kind in SETTLEMENT_KINDS and j.invoice:
            doc = find_document(book, SETTLEMENT_KINDS[j.kind] == "receipt", j.invoice, j.contact)
            if doc is not None:
                j.settles = {doc.key: None}


def read_xero(artifacts: ArtifactSet) -> Book:
    tables, strangers = read_set(artifacts, LAYOUTS)
    _check(tables, strangers).refuse()
    read_date = _dates(tables)
    book = Book(SOURCE_SYSTEM)
    _accounts(book, tables)
    _contacts(book, tables)
    _items(book, tables)
    _ledger(book, tables, read_date)
    book.base = steps.base_currency(book)
    for j in book.journals:
        j.currency = j.currency or book.base
    _documents(book, tables, read_date)
    for doc in book.documents.values():
        doc.currency = doc.currency or book.base
    _settles(book)
    steps.bank_currencies(book)
    _trial_balances(book, tables)
    _aged(book, tables)
    steps.roles_from_ledger(book)
    steps.tax_rates(book)
    book.company_name = company_name(tables)
    book.object_counts.update({"Account": len(book.accounts), "Contact": len(book.contacts), "Item": len(book.items),
                               "Invoice or bill": len(book.documents) + book.skipped[DELETED],
                               "Journal": len(book.journals)})
    return book


def matches(artifacts: ArtifactSet) -> bool:
    """Whether any file of the upload is laid out as a Xero export: a ledger, a chart, or a report."""
    tables, _ = read_set(artifacts, LAYOUTS)
    return any(tb.layout in (LEDGER, CHART, DOCUMENTS, TRIAL_BALANCE) for tb in tables)
