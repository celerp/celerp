# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A fictitious company's Xero export set, three financial years long.

The books are kept here the way Xero keeps them: every invoice, bill, payment,
journal and transfer posts its own lines, foreign records at their own rate, and
the files are written from those books in the layouts Xero's export menus use. The
trial balance carries profit and loss accounts year to date and rolls each
finished year's profit into retained earnings, as Xero's report does. Nothing here
is random: the same call writes the same bytes.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import openpyxl

COMPANY = "Harbour Lane Supplies Ltd"
BASE = "GBP"
YEARS = (2023, 2024, 2025)
END = date(2025, 12, 31)
CENT = Decimal("0.01")
ZERO = Decimal(0)
BATCH = 500

# code, name, Xero type, tax code
CHART = [
    ("090", "Business Bank Account", "Bank", "No VAT"),
    ("091", "USD Account", "Bank", "No VAT"),
    ("092", "Savings Account", "Bank", "No VAT"),
    ("200", "Sales", "Revenue", "20% (VAT on Income)"),
    ("260", "Other Revenue", "Revenue", "20% (VAT on Income)"),
    ("270", "Interest Income", "Revenue", "No VAT"),
    ("310", "Cost of Goods Sold", "Direct Costs", "20% (VAT on Expenses)"),
    ("400", "Advertising", "Overhead", "20% (VAT on Expenses)"),
    ("404", "Bank Fees", "Overhead", "No VAT"),
    ("416", "Depreciation", "Depreciation", "No VAT"),
    ("429", "General Expenses", "Overhead", "20% (VAT on Expenses)"),
    ("430", "Entertainment", "Overhead", "No VAT"),
    ("469", "Rent", "Overhead", "20% (VAT on Expenses)"),
    ("477", "Wages and Salaries", "Overhead", "No VAT"),
    ("498", "Loss on Disposal", "Overhead", "No VAT"),
    ("499", "Realised Currency Gains", "Overhead", "No VAT"),
    ("610", "Accounts Receivable", "Accounts Receivable", "No VAT"),
    ("620", "Prepayments", "Current Asset", "No VAT"),
    ("720", "Computer Equipment", "Fixed Asset", "20% (VAT on Capital Purchases)"),
    ("721", "Less Accumulated Depreciation on Computer Equipment", "Fixed Asset", "No VAT"),
    ("800", "Accounts Payable", "Accounts Payable", "No VAT"),
    ("820", "VAT", "VAT", "No VAT"),
    ("825", "Accruals", "Current Liability", "No VAT"),
    ("960", "Retained Earnings", "Retained Earnings", "No VAT"),
    ("970", "Owner A Funds Introduced", "Equity", "No VAT"),
]
PROFIT_AND_LOSS = {code for code, _, kind, _ in CHART
                   if kind in ("Revenue", "Direct Costs", "Overhead", "Depreciation", "Expense", "Other Income")}
TAXES = {"20% (VAT on Income)": Decimal(20), "20% (VAT on Expenses)": Decimal(20), "Zero Rated Income": ZERO,
         "No VAT": ZERO}

# name, currency, region, project
CUSTOMERS = [
    ("Abbott Joinery", BASE, "North", "Alpha"), ("Birch Interiors", BASE, "South", "Beta"),
    ("Coastal Builders", BASE, "North", "Alpha"), ("Dunmore Fittings", BASE, "South", "Alpha"),
    ("Elm Street Studio", BASE, "North", "Beta"), ("Fernside Homes", BASE, "South", "Beta"),
    ("Granary Works", BASE, "North", "Alpha"), ("Hollins Contractors", BASE, "South", "Alpha"),
    ("Ivy Lane Design", BASE, "North", "Beta"), ("Juniper Retail", BASE, "South", "Alpha"),
    ("Keystone Imports Inc", "USD", "North", "Beta"), ("Lakeshore Trading Co", "USD", "South", "Alpha"),
]
SUPPLIERS = [("Mill Timber Supply", "429"), ("Northgate Properties", "469"), ("Oakfield Print", "400"),
             ("Pinewood Hardware", "310"), ("Quayside Tech", "720"), ("Redbank Utilities", "429")]
BANK_CONTACT = "Harbourside Bank"
ITEMS = [("WID-01", "Oak shelf bracket", Decimal("12.50")), ("WID-02", "Brass hinge set", Decimal("8.40")),
         ("WID-03", "Cabinet handle", Decimal("4.75")), ("SRV-01", "Fitting service hour", Decimal("45.00")),
         ("SRV-02", "Design consultation", Decimal("80.00"))]
# USD per GBP, by month, as Xero states a rate.
RATES = {(y, m): Decimal("1.25") + Decimal("0.01") * ((y - 2023) * 12 + m) % Decimal("0.20")
         for y in YEARS for m in range(1, 13)}


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_base(foreign: Decimal, currency: str, on: date) -> Decimal:
    return foreign if currency == BASE else money(foreign / RATES[(on.year, on.month)])


@dataclass
class Line:
    item: str | None
    description: str
    quantity: Decimal
    unit: Decimal
    account: str
    tax_type: str
    region: str
    project: str

    @property
    def net(self) -> Decimal:
        return money(self.quantity * self.unit)

    @property
    def tax(self) -> Decimal:
        return money(self.net * TAXES[self.tax_type] / 100)


@dataclass
class Document:
    number: str
    kind: str                       # Sales invoice, Sales credit note, Bill, Bill credit note
    contact: str
    currency: str
    issued: date
    due: date
    lines: list[Line]
    status: str = "Awaiting Payment"
    reference: str = ""
    paid: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return sum((ln.net + ln.tax for ln in self.lines), ZERO)

    @property
    def tax_total(self) -> Decimal:
        return sum((ln.tax for ln in self.lines), ZERO)

    @property
    def sales(self) -> bool:
        return self.kind.startswith("Sales")

    @property
    def credit(self) -> bool:
        return "credit" in self.kind


@dataclass
class Posting:
    account: str
    amount: Decimal                  # base, debit positive
    description: str = ""
    foreign: Decimal | None = None   # signed, in the record's currency
    region: str = ""
    project: str = ""


@dataclass
class Journal:
    number: int
    on: date
    source: str
    reference: str
    contact: str
    currency: str
    invoice: str
    postings: list[Posting] = field(default_factory=list)
    applied: dict[str, Decimal] = field(default_factory=dict)   # invoice -> amount, a payment applied to several


class Books:
    def __init__(self) -> None:
        self.documents: list[Document] = []
        self.journals: list[Journal] = []

    def post(self, on: date, source: str, postings: list[Posting], *, reference: str = "", contact: str = "",
             currency: str = BASE, invoice: str = "") -> None:
        assert sum((p.amount for p in postings), ZERO) == 0, (source, reference)
        self.journals.append(Journal(len(self.journals) + 1, on, source, reference, contact, currency, invoice,
                                     postings))

    def issue(self, doc: Document) -> None:
        self.documents.append(doc)
        if doc.status in ("Voided", "Deleted", "Draft"):
            return
        sign = (1 if doc.sales else -1) * (-1 if doc.credit else 1)
        lines = []
        for ln in doc.lines:
            lines.append(Posting(ln.account, -sign * to_base(ln.net, doc.currency, doc.issued), ln.description,
                                 -sign * ln.net, ln.region, ln.project))
            if ln.tax:
                lines.append(Posting("820", -sign * to_base(ln.tax, doc.currency, doc.issued), ln.description,
                                     -sign * ln.tax, ln.region, ln.project))
        party = "610" if doc.sales else "800"
        lines.append(Posting(party, -sum((p.amount for p in lines), ZERO), doc.number, sign * doc.total))
        source = {"Sales invoice": "Receivable Invoice", "Sales credit note": "Receivable Credit Note",
                  "Bill": "Payable Invoice", "Bill credit note": "Payable Credit Note"}[doc.kind]
        self.post(doc.issued, source, lines, reference=doc.reference, contact=doc.contact, currency=doc.currency,
                  invoice=doc.number)

    def pay(self, doc: Document, amount: Decimal, on: date, bank: str, extra: Decimal = ZERO) -> None:
        """A receipt or payment of `amount` on `doc`, plus `extra` paid over, in the document's currency."""
        doc.paid += amount
        doc.status = "Paid" if doc.paid == doc.total else "Awaiting Payment"
        sign = 1 if doc.sales else -1
        party = "610" if doc.sales else "800"
        postings = [Posting(bank, sign * to_base(amount, doc.currency, on), doc.number, sign * amount),
                    Posting(party, -sign * to_base(amount, doc.currency, doc.issued), doc.number, -sign * amount)]
        gain = -sum((p.amount for p in postings), ZERO)
        if gain:
            postings.append(Posting("499", gain, "Currency gain"))
        self.post(on, "Receivable Payment" if doc.sales else "Payable Payment", postings, reference=doc.number,
                  contact=doc.contact, currency=doc.currency, invoice=doc.number)
        if extra:
            over = to_base(extra, doc.currency, on)
            self.post(on, "Receivable Overpayment" if doc.sales else "Payable Overpayment",
                      [Posting(bank, sign * over, "Overpayment", sign * extra),
                       Posting(party, -sign * over, "Overpayment", -sign * extra)],
                      reference=f"{doc.number} overpaid", contact=doc.contact, currency=doc.currency)

    def balances(self, on: date) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for j in self.journals:
            if j.on <= on:
                for p in j.postings:
                    out[p.account] += p.amount
        return out


def month_ends(year: int) -> list[date]:
    return [(date(year, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1) for m in range(1, 13)]


def simulate() -> Books:
    """Three years of trading, every day's records in order."""
    books = Books()
    books.post(date(2023, 1, 2), "Receive Money",
               [Posting("090", Decimal("25000.00"), "Capital"), Posting("970", Decimal("-25000.00"), "Capital")],
               reference="Owner capital", contact="Owner A")
    sales = purchases = credits = 0
    for year in YEARS:
        for month in range(1, 13):
            first = date(year, month, 1)
            for n in range(18):
                cust = CUSTOMERS[(n + month + year) % len(CUSTOMERS)]
                name, currency, region, project = cust
                lines = []
                for i in range(1 + (n % 3)):
                    code, desc, price = ITEMS[(n + i + month) % len(ITEMS)]
                    tax = "Zero Rated Income" if currency != BASE else "20% (VAT on Income)"
                    unit = price if currency == BASE else money(price * Decimal("1.3"))
                    lines.append(Line(code, desc, Decimal(1 + (n + i) % 4), unit, "200", tax, region, project))
                sales += 1
                issued = first + timedelta(days=n)
                doc = Document(f"INV-{sales:04d}", "Sales invoice", name, currency, issued,
                               issued + timedelta(days=30), lines)
                if n == 7 and month in (3, 9):
                    doc.status = "Voided"
                elif n == 11 and month == 6:
                    doc.status = "Deleted"
                elif n == 13 and month == 12 and year == 2025:
                    doc.status = "Draft"
                books.issue(doc)
                if doc.status != "Awaiting Payment":
                    continue
                paid_on = issued + timedelta(days=20)
                bank = "091" if currency != BASE else "090"
                if paid_on > END:
                    continue
                if n == 5:
                    half = money(doc.total / 2)
                    books.pay(doc, half, paid_on, bank)
                    if paid_on + timedelta(days=25) <= END:
                        books.pay(doc, doc.total - half, paid_on + timedelta(days=25), bank)
                elif n == 9 and month in (4, 10):
                    books.pay(doc, doc.total, paid_on, bank, extra=Decimal("15.00"))
                elif n == 15 and month == 8:
                    credits += 1
                    note = Document(f"CN-{credits:04d}", "Sales credit note", name, currency, paid_on,
                                    paid_on, [Line(None, "Damaged on delivery", Decimal(1), lines[0].unit, "200",
                                                   lines[0].tax_type, region, project)], reference=doc.number)
                    books.issue(note)
                    note.status = "Paid"
                    note.paid = note.total
                    doc.paid += note.total
                    books.pay(doc, doc.total - doc.paid, paid_on + timedelta(days=2), bank)
                elif n != 17:
                    books.pay(doc, doc.total, paid_on, bank)
            for s, (supplier, account) in enumerate(SUPPLIERS):
                if account == "720" and month not in (2, 8):
                    continue
                purchases += 1
                issued = first + timedelta(days=3 + s)
                amount = Decimal(120 + 35 * s + month) if account != "720" else Decimal("1800.00")
                tax = "No VAT" if account == "429" and s == 5 else "20% (VAT on Expenses)"
                bill = Document(f"BILL-{purchases:04d}", "Bill", supplier, BASE, issued, issued + timedelta(days=30),
                                [Line(None, f"{supplier} {month:02d}/{year}", Decimal(1), amount, account, tax,
                                      "North" if s % 2 else "South", "Alpha")], reference=f"S{year}{month:02d}{s}")
                books.issue(bill)
                if issued + timedelta(days=14) <= END and not (year == 2025 and month == 12):
                    books.pay(bill, bill.total, issued + timedelta(days=14), "090")
            end = month_ends(year)[month - 1]
            books.post(end, "Spend Money", [Posting("404", Decimal("12.50"), "Monthly fee"),
                                            Posting("090", Decimal("-12.50"), "Monthly fee")],
                       reference=f"Fee {month:02d}/{year}", contact=BANK_CONTACT)
            books.post(end, "Manual Journal", [Posting("416", Decimal("150.00"), "Depreciation"),
                                               Posting("721", Decimal("-150.00"), "Depreciation")],
                       reference=f"Depreciation {month:02d}/{year}")
            if month % 3 == 0:
                books.post(end, "Bank Transfer", [Posting("092", Decimal("2000.00"), "To savings"),
                                                  Posting("090", Decimal("-2000.00"), "To savings")],
                           reference=f"Savings {month:02d}/{year}")
                books.post(end, "Receive Money", [Posting("092", Decimal("6.10"), "Interest"),
                                                  Posting("270", Decimal("-6.10"), "Interest")],
                           reference=f"Interest {month:02d}/{year}", contact=BANK_CONTACT)
        books.post(date(year, 12, 31), "Manual Journal", [Posting("429", Decimal("300.00"), "Year end accrual"),
                                                         Posting("825", Decimal("-300.00"), "Year end accrual")],
                   reference=f"Accrual {year}")
        books.post(date(year + 1, 1, 1) if year < YEARS[-1] else date(year, 12, 31), "Manual Journal",
                   [Posting("825", Decimal("300.00"), "Reverse accrual"),
                    Posting("429", Decimal("-300.00"), "Reverse accrual")], reference=f"Accrual {year} reversed")
    usd = sum((p.foreign or ZERO for j in books.journals for p in j.postings if p.account == "091"), ZERO)
    on = date(2025, 11, 28)
    moved = money(usd / 2)
    books.post(on, "Bank Transfer", [Posting("090", to_base(moved, "USD", on), "From USD"),
                                     Posting("091", -to_base(moved, "USD", on), "From USD", -moved)],
               reference="USD to GBP", currency="USD")
    books.post(date(2025, 10, 31), "Manual Journal",
               [Posting("721", Decimal("1500.00"), "Laptop disposed"), Posting("498", Decimal("300.00"), "Laptop disposed"),
                Posting("720", Decimal("-1800.00"), "Laptop disposed")], reference="Fixed asset disposal")
    books.journals.sort(key=lambda j: (j.on, j.number))
    return books


def _date(on: date) -> str:
    return on.strftime("%d/%m/%Y")


def _report_date(on: date) -> str:
    return f"{on.day} {on.strftime('%b %Y')}"


def _csv(header: list[str], rows: list[list]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def _xlsx(lines: list[list]) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    for line in lines:
        sheet.append(line)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def chart() -> bytes:
    rows = [[c, n, k, t, "", "No", "No", "No", ""] for c, n, k, t in CHART]
    return _csv(["*Code", "*Name", "*Type", "*Tax Code", "Description", "Dashboard", "Expense Claims",
                 "Enable Payments", "Balance"], rows)


def contacts() -> bytes:
    header = ["*ContactName", "AccountNumber", "EmailAddress", "FirstName", "LastName", "POAddressLine1", "POCity",
              "POPostalCode", "POCountry", "PhoneNumber", "TaxNumber", "DefaultCurrency", "TrackingName1",
              "SalesTrackingOption1", "TrackingName2", "SalesTrackingOption2"]
    rows = []
    for i, (name, currency, region, project) in enumerate(CUSTOMERS):
        rows.append([name, f"C{i + 1:03d}", f"accounts{i + 1}@example.com", "", "", f"{i + 1} Market Street",
                     "Leeds", "LS1 1AA", "United Kingdom", f"0113 000 {i:04d}", "", currency, "Region", region,
                     "Project", project])
    for i, (name, _) in enumerate(SUPPLIERS):
        rows.append([name, f"S{i + 1:03d}", f"billing{i + 1}@example.com", "", "", f"{i + 1} Dock Road", "Hull",
                     "HU1 1AA", "United Kingdom", "", "", BASE, "", "", "", ""])
    rows.append([BANK_CONTACT, "", "", "", "", "", "", "", "", "", "", BASE, "", "", "", ""])
    rows.append(["Owner A", "", "", "", "", "", "", "", "", "", "", BASE, "", "", "", ""])
    return _csv(header, rows)


def items() -> bytes:
    header = ["*ItemCode", "ItemName", "Quantity", "PurchasesDescription", "PurchasesUnitPrice", "PurchasesAccount",
              "PurchasesTaxRate", "SalesDescription", "SalesUnitPrice", "SalesAccount", "SalesTaxRate",
              "InventoryAssetAccount", "CostOfGoodsSoldAccount", "Status"]
    return _csv(header, [[code, name, "", "", "", "", "", name, str(price), "200", "20% (VAT on Income)", "", "",
                          "Active"] for code, name, price in ITEMS])


DOCUMENT_HEADER = ["*ContactName", "EmailAddress", "POAddressLine1", "POCity", "POPostalCode", "POCountry",
                   "*InvoiceNumber", "Reference", "*InvoiceDate", "*DueDate", "Total", "TaxTotal",
                   "InvoiceAmountPaid", "InvoiceAmountDue", "InventoryItemCode", "*Description", "*Quantity",
                   "*UnitAmount", "Discount", "LineAmount", "*AccountCode", "*TaxType", "TaxAmount", "TrackingName1",
                   "TrackingOption1", "TrackingName2", "TrackingOption2", "Currency", "Type", "Sent", "Status"]


def _document_rows(doc: Document) -> list[list]:
    return [[doc.contact, "", "", "", "", "", doc.number, doc.reference, _date(doc.issued), _date(doc.due),
             str(doc.total), str(doc.tax_total), str(doc.paid), str(doc.total - doc.paid), ln.item or "",
             ln.description, str(ln.quantity), str(ln.unit), "", str(ln.net), ln.account, ln.tax_type, str(ln.tax),
             "Region", ln.region, "Project", ln.project, doc.currency, doc.kind, "Sent", doc.status]
            for ln in doc.lines]


def document_batches(books: Books, sales: bool) -> list[bytes]:
    docs = [d for d in books.documents if d.sales == sales]
    return [_csv(DOCUMENT_HEADER, [row for d in docs[i:i + BATCH] for row in _document_rows(d)])
            for i in range(0, len(docs), BATCH)]


GL_HEADER = ["Journal Number", "Date", "Source", "Reference", "Invoice Number", "Contact", "Account Code",
             "Account Name", "Description", "Debit", "Credit", "Currency", "Foreign Amount", "Tracking Name 1",
             "Tracking Option 1", "Tracking Name 2", "Tracking Option 2"]


def general_ledger(books: Books) -> bytes:
    names = {c: n for c, n, _, _ in CHART}
    out = io.StringIO()
    writer = csv.writer(out, delimiter="\t", lineterminator="\r\n")
    writer.writerow(GL_HEADER)
    for j in books.journals:
        for p in j.postings:
            foreign = "" if j.currency == BASE or p.foreign is None else str(p.foreign)
            writer.writerow([j.number, _date(j.on), j.source, j.reference, j.invoice, j.contact, p.account,
                             names[p.account], p.description, str(max(p.amount, ZERO)) if p.amount > 0 else "",
                             str(-p.amount) if p.amount < 0 else "", j.currency, foreign,
                             "Region" if p.region else "", p.region, "Project" if p.project else "", p.project])
    return out.getvalue().encode("utf-8")


def trial_balance(books: Books, year: int) -> bytes:
    """Xero's trial balance with a column per month end: profit and loss year to date,
    retained earnings carrying every earlier year's profit."""
    ends = month_ends(year)
    start = date(year, 1, 1)
    lines: list[list] = [["Trial Balance"], [COMPANY], [f"As at {_report_date(ends[-1])}"], [],
                         ["Account Code", "Account", *[_report_date(d) for d in ends]]]
    prior = books.balances(start - timedelta(days=1))
    earlier_profit = sum((prior[c] for c in PROFIT_AND_LOSS), ZERO)
    for code, name, _, _ in CHART:
        values = []
        for on in ends:
            balance = books.balances(on)[code]
            if code in PROFIT_AND_LOSS:
                balance -= prior[code]
            elif code == "960":
                balance += earlier_profit
            values.append(float(balance))
        if any(values):
            lines.append([code, name, *values])
    return _xlsx(lines)


def aged(books: Books, sales: bool) -> bytes:
    """Aged receivables or payables detail at the last day, one block per contact, base currency."""
    title = "Aged Receivables Detail" if sales else "Aged Payables Detail"
    lines: list[list] = [[title], [COMPANY], [f"As at {_report_date(END)}"], [],
                         ["Date", "Invoice Number", "Due Date", "Current", "< 1 Month", "1 Month", "2 Months",
                          "Older", "Total"]]
    party = "610" if sales else "800"
    owed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for j in books.journals:
        for p in j.postings:
            if p.account == party:
                owed[j.contact] += p.amount if sales else -p.amount
    for contact in sorted(owed):
        if not owed[contact]:
            continue
        lines.append([contact])
        lines.append([_report_date(END), "", "", float(owed[contact]), 0, 0, 0, 0, float(owed[contact])])
        lines.append([f"Total {contact}", "", "", float(owed[contact]), 0, 0, 0, 0, float(owed[contact])])
    total = sum(owed.values(), ZERO)
    lines.append(["Total", "", "", float(total), 0, 0, 0, 0, float(total)])
    return _xlsx(lines)


def files(books: Books | None = None) -> dict[str, bytes]:
    """Every file of the export set, by the name Xero gives it."""
    books = books or simulate()
    out = {"ChartOfAccounts.csv": chart(), "Contacts.csv": contacts(), "InventoryItems.csv": items(),
           "General Ledger.txt": general_ledger(books),
           "Aged Receivables Detail.xlsx": aged(books, True), "Aged Payables Detail.xlsx": aged(books, False)}
    for n, data in enumerate(document_batches(books, True), 1):
        out[f"Invoices {n}.csv"] = data
    for n, data in enumerate(document_batches(books, False), 1):
        out[f"Bills {n}.csv"] = data
    for year in YEARS:
        out[f"Trial Balance {year}.xlsx"] = trial_balance(books, year)
    return out


def write(folder: Path, books: Books | None = None) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in files(books).items():
        (folder / name).write_bytes(data)
        paths.append(folder / name)
    return paths
