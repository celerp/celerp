# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The same fictitious company's books written as a QuickBooks Online export set.

The trading is the Xero fixture's three years (`xero_export.build.simulate`), so both
sources are checked against one set of books. Here they are written the way QuickBooks
Online's export menus and reports write them: workbooks headed by the company name,
the report title and its period; accounts shown as number and name; a Journal report
with one block per transaction; Sales by Customer Detail and Purchases by Vendor Detail
with one row per line; Invoices and Received Payments and Bills and Applied Payments for
what each payment settled; A/R and A/P Aging Detail as of the last day; and a Trial
Balance per period, each running from the first day of its financial year. QuickBooks
Online keeps no drafts and exports no deleted records, so those are left out, and a
voided invoice is one zero row.
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date

import openpyxl

from fixtures.xero_export.build import (
    BASE, CHART, COMPANY, CUSTOMERS, END, ITEMS, PROFIT_AND_LOSS, SUPPLIERS, YEARS, ZERO, Books, Document, simulate,
    to_base,
)

# Xero type -> QuickBooks Online type and detail type
TYPES = {
    "Bank": ("Bank", "Checking"), "Revenue": ("Income", "Sales of Product Income"),
    "Direct Costs": ("Cost of Goods Sold", "Supplies & Materials - COGS"),
    "Overhead": ("Expenses", "Office/General Administrative Expenses"), "Depreciation": ("Other Expense", "Depreciation"),
    "Accounts Receivable": ("Accounts receivable (A/R)", "Accounts Receivable (A/R)"),
    "Current Asset": ("Other Current Assets", "Prepaid Expenses"), "Fixed Asset": ("Fixed Assets", "Machinery & Equipment"),
    "Accounts Payable": ("Accounts payable (A/P)", "Accounts Payable (A/P)"), "VAT": ("Other Current Liabilities", "VAT Control"),
    "Current Liability": ("Other Current Liabilities", "Accrued Liabilities"),
    "Retained Earnings": ("Equity", "Retained Earnings"), "Equity": ("Equity", "Owner's Equity"),
}
SPECIAL = {"270": ("Other Income", "Interest Earned"), "499": ("Other Expense", "Exchange Gain or Loss"),
           "721": ("Fixed Assets", "Accumulated Depreciation")}
NAMES = {code: name for code, name, _, _ in CHART} | {"499": "Exchange Gain or Loss"}
TAX_CODES = {"20% (VAT on Income)": "20.0% S", "20% (VAT on Expenses)": "20.0% S", "Zero Rated Income": "0.0% Z",
             "No VAT": "No VAT"}
TRANSACTION_TYPES = {
    "Receivable Invoice": "Invoice", "Receivable Credit Note": "Credit Memo", "Payable Invoice": "Bill",
    "Payable Credit Note": "Vendor Credit", "Receivable Payment": "Payment", "Payable Payment": "Bill Payment (Check)",
    "Receivable Overpayment": "Payment", "Payable Overpayment": "Bill Payment (Check)", "Receive Money": "Deposit",
    "Spend Money": "Expense", "Manual Journal": "Journal Entry", "Bank Transfer": "Transfer",
}
DOCUMENT_TYPES = {"Sales invoice": "Invoice", "Sales credit note": "Credit Memo", "Bill": "Bill",
                  "Bill credit note": "Vendor Credit"}
ITEM_NAMES = {code: name for code, name, _ in ITEMS}


def account(code: str) -> str:
    return f"{code} {NAMES[code]}"


def _date(on: date) -> str:
    return on.strftime("%d/%m/%Y")


def _long(on: date) -> str:
    return f"{on.day} {on:%B %Y}"


def _xlsx(title: str, period: str, header: list[str], rows: list[list]) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    for line in ([COMPANY], [title], [period], [], header, *rows):
        sheet.append(line)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _amount(value) -> float | str:
    return float(value) if value else ""


def account_list() -> bytes:
    rows = []
    for code, name, kind, _ in CHART:
        qbo_type, detail = SPECIAL.get(code) or TYPES[kind]
        rows.append([code, NAMES[code], qbo_type, detail, "", ""])
    return _xlsx("Account List", "", ["Account #", "Account", "Type", "Detail Type", "Description", "Balance"], rows)


def customers() -> bytes:
    rows = [[name, f"0113 000 {i:04d}", f"accounts{i + 1}@example.com", "",
             f"{i + 1} Market Street, Leeds, LS1 1AA, United Kingdom", currency]
            for i, (name, currency, _, _) in enumerate(CUSTOMERS)]
    return _xlsx("Customer Contact List", "", ["Customer", "Phone Numbers", "Email", "Full Name", "Billing Address",
                                               "Currency"], rows)


def vendors() -> bytes:
    rows = [[name, "", f"billing{i + 1}@example.com", "", f"{i + 1} Dock Road, Hull, HU1 1AA, United Kingdom", "", BASE]
            for i, (name, _) in enumerate(SUPPLIERS)]
    return _xlsx("Vendor Contact List", "", ["Vendor", "Phone Numbers", "Email", "Full Name", "Address", "Account #",
                                             "Currency"], rows)


def products() -> bytes:
    rows = [[name, code, "Non-inventory", name, float(price), ""] for code, name, price in ITEMS]
    return _xlsx("Product/Service List", "", ["Product/Service", "SKU", "Type", "Description", "Price", "Cost"], rows)


def _period(start: date = date(YEARS[0], 1, 1), end: date = END) -> str:
    return f"{_long(start)} - {_long(end)}"


def journal(books: Books, start: date = date(YEARS[0], 1, 1), end: date = END) -> bytes:
    """The Journal report for one period; QuickBooks Online is exported a year at a time."""
    rows: list[list] = []
    for j in (j for j in books.journals if start <= j.on <= end):
        kind = TRANSACTION_TYPES[j.source]
        num = j.invoice if kind in ("Invoice", "Credit Memo", "Bill", "Vendor Credit") else (
            str(j.number) if kind == "Journal Entry" else "")
        for n, p in enumerate(j.postings):
            head = [_date(j.on), kind, num, j.contact] if n == 0 else ["", "", "", ""]
            foreign = "" if j.currency == BASE or p.foreign is None else float(p.foreign)
            rows.append([*head, p.description, account(p.account), _amount(max(p.amount, ZERO)),
                         _amount(max(-p.amount, ZERO)), j.currency, foreign])
        debit = sum((p.amount for p in j.postings if p.amount > 0), ZERO)
        rows.append(["", "", "", "", "", "", float(debit), float(debit), "", ""])
        rows.append([])
    return _xlsx("Journal", _period(start, end), ["Date", "Transaction Type", "Num", "Name", "Memo/Description", "Account", "Debit",
                                        "Credit", "Currency", "Foreign Amount"], rows)


def _exported(books: Books, sales: bool) -> list[Document]:
    return [d for d in books.documents if d.sales == sales and d.status not in ("Deleted", "Draft")]


def line_detail(books: Books, sales: bool) -> bytes:
    rows: list[list] = []
    for doc in _exported(books, sales):
        kind = DOCUMENT_TYPES[doc.kind]
        sign = -1 if doc.credit else 1
        foreign = doc.currency != BASE
        if doc.status == "Voided":
            rows.append([_date(doc.issued), kind, doc.number, doc.contact, _date(doc.due), "", "Voided", 0, 0, 0,
                         account(doc.lines[0].account), "", 0, doc.currency, "", "", ""])
            continue
        for ln in doc.lines:
            rows.append([_date(doc.issued), kind, doc.number, doc.contact, _date(doc.due),
                         ITEM_NAMES.get(ln.item or "", ""), ln.description, float(sign * ln.quantity), float(ln.unit),
                         float(sign * to_base(ln.net, doc.currency, doc.issued)), account(ln.account),
                         TAX_CODES[ln.tax_type], float(sign * ln.tax), doc.currency,
                         float(sign * ln.net) if foreign else "", ln.region, ln.project])
    party, price = ("Customer", "Sales Price") if sales else ("Vendor", "Rate")
    title = "Sales by Customer Detail" if sales else "Purchases by Vendor Detail"
    return _xlsx(title, _period(), ["Date", "Transaction Type", "Num", party, "Due Date", "Product/Service",
                                    "Memo/Description", "Qty", price, "Amount", "Account", "Tax Code", "Tax Amount",
                                    "Currency", "Foreign Amount", "Class", "Location"], rows)


def _paid_in_document_currency(j, party: str) -> float:
    line = next(p for p in j.postings if p.account == party)
    return float(abs(line.foreign if line.foreign is not None else line.amount))


def applied(books: Books, sales: bool) -> bytes:
    """Each invoice or bill followed by the payments and credits applied to it."""
    party = "610" if sales else "800"
    payment = "Receivable Payment" if sales else "Payable Payment"
    notes = defaultdict(list)
    for doc in _exported(books, sales):
        if doc.credit and doc.reference:
            notes[doc.reference].append(doc)
    rows: list[list] = []
    for doc in _exported(books, sales):
        if doc.credit or doc.status == "Voided":
            continue
        paid = [(j, j.applied[doc.number] if j.applied else _paid_in_document_currency(j, party))
                for j in books.journals if j.source == payment and (j.invoice == doc.number or doc.number in j.applied)]
        if not paid and not notes[doc.number]:
            continue
        rows.append([_date(doc.issued), DOCUMENT_TYPES[doc.kind], doc.number, doc.contact, float(doc.total)])
        for note in notes[doc.number]:
            rows.append([_date(note.issued), DOCUMENT_TYPES[note.kind], note.number, doc.contact, float(note.total)])
        for j, amount in paid:
            rows.append([_date(j.on), TRANSACTION_TYPES[payment], "", doc.contact, float(amount)])
    title = "Invoices and Received Payments" if sales else "Bills and Applied Payments"
    return _xlsx(title, _period(), ["Date", "Transaction Type", "Num", "Customer" if sales else "Vendor", "Amount"],
                 rows)


def aging(books: Books, sales: bool) -> bytes:
    """Open invoices and bills as of the last day, base and document currency, and each
    contact's unapplied payments."""
    party = "610" if sales else "800"
    sign = 1 if sales else -1
    owed: dict[str, object] = defaultdict(lambda: ZERO)
    for j in books.journals:
        for p in j.postings:
            if p.account == party:
                owed[j.contact] += sign * p.amount
    rows: list[list] = []
    listed: dict[str, object] = defaultdict(lambda: ZERO)
    for doc in _exported(books, sales):
        if doc.status == "Voided":
            continue
        open_ = doc.total - doc.paid
        if not open_:
            continue
        home = to_base(open_, doc.currency, doc.issued) * (-1 if doc.credit else 1)
        listed[doc.contact] += home
        rows.append([_date(doc.issued), DOCUMENT_TYPES[doc.kind], doc.number, doc.contact, _date(doc.due), "",
                     float(doc.total), float(home), float(open_) if doc.currency != BASE else ""])
    for contact in sorted(owed):
        rest = owed[contact] - listed[contact]
        if rest:
            rows.append([_date(END), "Payment" if sales else "Bill Payment (Check)", "", contact, "", "", "",
                         float(rest), ""])
    rows.append(["TOTAL", "", "", "", "", "", "", float(sum(owed.values(), ZERO)), ""])
    title = "A/R Aging Detail" if sales else "A/P Aging Detail"
    return _xlsx(title, f"As of {_long(END)}", ["Date", "Transaction Type", "Num", "Customer" if sales else "Vendor",
                                               "Due Date", "Past Due", "Amount", "Open Balance",
                                               "Foreign Open Balance"], rows)


def trial_balance(books: Books, on: date) -> bytes:
    """Balances as of `on`, profit and loss from the first day of its financial year, earlier
    years' profit in retained earnings, as QuickBooks Online prints them."""
    start = date(on.year, 1, 1)
    prior = books.balances(date(on.year - 1, 12, 31))
    now = books.balances(on)
    earlier = sum((prior[c] for c in PROFIT_AND_LOSS), ZERO)
    rows = []
    debits = credits = ZERO
    for code, _, _, _ in CHART:
        value = now[code] - prior[code] if code in PROFIT_AND_LOSS else now[code] + (earlier if code == "960" else ZERO)
        if value:
            rows.append([account(code), _amount(max(value, ZERO)), _amount(max(-value, ZERO))])
            debits += max(value, ZERO)
            credits += max(-value, ZERO)
    rows.append(["TOTAL", float(debits), float(credits)])
    return _xlsx("Trial Balance", f"{_long(start)} - {_long(on)}", ["", "Debit", "Credit"], rows)


TRIAL_BALANCE_DATES = [d for y in YEARS for d in (date(y, 6, 30), date(y, 12, 31))]


def files(books: Books | None = None) -> dict[str, bytes]:
    """Every file of the export set, by the name QuickBooks Online gives it."""
    books = books or simulate()
    out = {"Account List.xlsx": account_list(), "Customers.xlsx": customers(), "Vendors.xlsx": vendors(),
           "Products and Services.xlsx": products(), "Journal.xlsx": journal(books),
           "Sales by Customer Detail.xlsx": line_detail(books, True),
           "Purchases by Vendor Detail.xlsx": line_detail(books, False),
           "Invoices and Received Payments.xlsx": applied(books, True),
           "Bills and Applied Payments.xlsx": applied(books, False),
           "AR Aging Detail.xlsx": aging(books, True), "AP Aging Detail.xlsx": aging(books, False)}
    for on in TRIAL_BALANCE_DATES:
        out[f"Trial Balance {on.isoformat()}.xlsx"] = trial_balance(books, on)
    return out
