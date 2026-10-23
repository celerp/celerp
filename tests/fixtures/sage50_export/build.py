# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A fictitious Ethiopian company's Sage 50 (Peachtree) export set: beginning balances,
then twenty-one months of trading in Birr across two fiscal years running July to June.

The books are kept here as Sage 50 keeps them, every transaction a set of distribution
lines that balance, and the files are written in the layouts its templates and reports use:

- The import/export templates follow the field lists in Sage 50's help, "Import/Export
  Field Definition Lists" (Chart of Accounts, Customer List, Vendor List, Inventory Item
  List, Employee List, General Journal, Sales Journal, Cash Receipts Journal, Purchases
  Journal and Payments / Cash Disbursements Journal), under the names they give each
  field: one row per distribution, each row repeating its transaction's header fields,
  Number of Distributions on every row; the sales tax as a distribution with Invoice/CM
  Distribution 0, Tax Type 0 and its Sales Tax Agency; every field each journal's template
  has to say what a transaction is (Credit Memo, Quote, Beginning Balance Transaction, and
  Voided by Transaction on the Sales and Payments Journals alone), regular transactions all,
  the Purchases Journal's Row Type, blank on every line as no purchase withholds retainage,
  and Waiting on Bill, and the Cash Receipts and Payments Journals' Vendor Receipt and Customer
  Payment, the refund direction, and Prepayment, FALSE on every receipt and payment, with each
  one's Total Paid on Invoice(s) and every payment a Detailed Payment;
  the Purchases Journal's Invoice #
  and Apply to Invoice #; a receipt or payment line applied to an invoice with its G/L
  Account empty, as Sage 50 leaves it; dates as MM/DD/YY, True and False as TRUE and FALSE,
  ANSI (Windows-1252) text, and each amount signed as it posts, debit positive. Goods are
  Tax Type 1 and services Tax Type 2, the company exempting services. `alternate` writes the
  names other releases and templates use ("Sales Tax Agency ID", "Invoice/CM #" and "Apply
  to Invoice Number" on purchases, "Check #") and fills the G/L Account of applied lines.
- The General Ledger Trial Balance and the Aged Receivables and Aged Payables reports
  follow Sage 50's own report layouts as Reports & Forms exports them: the company, the
  report title and "As of" date above the column headings, amounts with thousands
  separators and the business's currency sign on the first and total lines. The trial
  balance states profit and loss for the fiscal year to date and carries every earlier
  year's profit in retained earnings, as Sage 50's does. The aged reports name each
  customer or vendor on the first row of its block, then a total row.

Nothing here is random: the same call writes the same bytes.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import openpyxl

COMPANY = "Highland Office Supplies PLC"
SIGN = "Br"
ZERO = Decimal(0)
CENT = Decimal("0.01")
OPENED = date(2023, 6, 30)
END = date(2025, 3, 31)
VAT = "VAT15"
VAT_RATE = Decimal(15)

# Account ID, description, Sage 50 account type number
CHART = [
    ("1010", "Cash on Hand", 0), ("1020", "Commercial Bank Current Account", 0), ("1030", "Savings Account", 0),
    ("1100", "Accounts Receivable", 1), ("1200", "Inventory", 2), ("1400", "Prepaid Expenses", 4),
    ("1500", "Office Equipment", 5), ("1550", "Accumulated Depreciation - Office Equipment", 6),
    ("2000", "Accounts Payable", 10), ("2310", "VAT Payable", 12), ("2500", "Bank Loan", 14),
    ("3000", "Owner's Capital", 16), ("3900", "Retained Earnings", 18), ("4000", "Sales - Office Supplies", 21),
    ("4100", "Sales - Printing Services", 21), ("4900", "Sales Discounts", 21), ("5000", "Cost of Goods Purchased", 23),
    ("5050", "Purchase Discounts", 23), ("6000", "Rent Expense", 24), ("6100", "Utilities Expense", 24),
    ("6200", "Bank Charges", 24), ("6300", "Depreciation Expense", 24), ("6400", "Salaries Expense", 24),
]
PROFIT_AND_LOSS = {code for code, _, kind in CHART if kind >= 21}
NAMES = {code: name for code, name, _ in CHART}
# Beginning balances at the end of the fiscal year before the first transaction: the
# balance sheet, and that year's profit and loss.
OPENING = {"1010": Decimal("25000.00"), "1020": Decimal("640000.00"), "1200": Decimal("180000.00"),
           "1500": Decimal("300000.00"), "1550": Decimal("-60000.00"), "2500": Decimal("-200000.00"),
           "3000": Decimal("-824800.00"), "3900": Decimal("-150000.00"), "4000": Decimal("-1450000.00"),
           "4100": Decimal("-230000.00"), "5000": Decimal("820000.00"), "6000": Decimal("360000.00"),
           "6100": Decimal("30000.00"), "6200": Decimal("1800.00"), "6300": Decimal("18000.00"),
           "6400": Decimal("540000.00")}
assert sum(OPENING.values()) == 0

# Customer ID, name, charged VAT. Two customers share a name; Sage 50 tells them apart by ID.
CUSTOMERS = [("ALEM01", "Alem Stationery", True), ("BETH01", "Bethel School", False),
             ("CAFE01", "Café Lumière", True), ("DIRE01", "Dire Logistics", True),
             ("SELA01", "Selam Cafe", True), ("SELA02", "Selam Cafe", True), ("TENA01", "Tena Clinic", False)]
VENDORS = [("V100", "Merkato Paper Supply"), ("V200", "Bole Properties"), ("V300", "City Power Utility"),
           ("V400", "Highland Office Furniture")]
# Item ID, description, class, sales price
ITEMS = [("SUP-PAPER", "A4 Paper Ream", "Non-stock item", Decimal("450.00")),
         ("SUP-PEN", "Ballpoint Pens (box)", "Non-stock item", Decimal("180.00")),
         ("SUP-TONER", 'Toner "XL" cartridge, black', "Stock item", Decimal("3200.00")),
         ("SRV-PRINT", "Printing service,\nper 100 pages", "Service", Decimal("250.00"))]
EMPLOYEES = [("E001", "Employee One"), ("E002", "Employee Two"), ("E003", "Employee Three")]


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    """One distribution, signed as it posts: debit positive."""
    account: str
    amount: Decimal
    description: str = ""
    item: str = ""
    quantity: Decimal = ZERO
    unit: Decimal = ZERO
    agency: str = ""
    kind: str = ""                    # the line's Tax Type: 0 tax, 1 goods, 2 services
    paid: str = ""                    # the invoice a receipt or payment line pays
    discount: Decimal = ZERO          # a discount taken on that invoice


@dataclass
class Transaction:
    journal: str                      # general, sales, purchases, receipts, payments
    on: date
    reference: str                    # invoice/CM number, receipt reference, check number or journal reference
    lines: list[Line]
    party: str = ""                   # customer or vendor ID
    head: str = ""                    # receivable, payable or cash account
    credit: bool = False
    apply_to: str = ""
    due: date | None = None
    discount_account: str = ""

    @property
    def discount_sign(self) -> int:
        return 1 if self.journal == "receipts" else -1

    @property
    def head_amount(self) -> Decimal:
        """The customer, vendor or cash line, which balances the distributions and discounts."""
        return -sum((ln.amount + self.discount_sign * ln.discount for ln in self.lines), ZERO)

    def postings(self) -> list[tuple[str, Decimal]]:
        out = [(ln.account, ln.amount) for ln in self.lines]
        out += [(self.discount_account, self.discount_sign * ln.discount) for ln in self.lines if ln.discount]
        if self.head:
            out.append((self.head, self.head_amount))
        assert sum((a for _, a in out), ZERO) == 0, self
        return out


@dataclass
class Books:
    transactions: list[Transaction] = field(default_factory=list)

    def add(self, txn: Transaction) -> Transaction:
        txn.postings()
        self.transactions.append(txn)
        return txn

    def balances(self, on: date) -> dict[str, Decimal]:
        """Balances from the beginning balances on, at the end of `on`."""
        out: dict[str, Decimal] = defaultdict(lambda: ZERO, OPENING)
        for txn in self.transactions:
            if txn.on <= on:
                for account, value in txn.postings():
                    out[account] += value
        return out

    def documents(self, journal: str) -> list[Transaction]:
        return [x for x in self.transactions if x.journal == journal]

    def open_amounts(self, sales: bool) -> dict[str, Decimal]:
        """What is still owed on each invoice or purchase, and still to apply of each credit
        memo (negative), by number."""
        journal, cash = ("sales", "receipts") if sales else ("purchases", "payments")
        sign = 1 if sales else -1
        owed = {d.reference: sign * d.head_amount for d in self.documents(journal)}
        for d in self.documents(journal):
            if d.credit and d.apply_to:
                owed[d.apply_to] += owed[d.reference]
                owed[d.reference] = ZERO
        for x in self.documents(cash):
            for ln in x.lines:
                if ln.paid:
                    owed[ln.paid] -= sign * -ln.amount
        return owed

    def owed(self, sales: bool) -> dict[str, Decimal]:
        """Each customer's or vendor's balance, as the aged report totals it."""
        account, sign = ("1100", 1) if sales else ("2000", -1)
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for x in self.transactions:
            if x.party:
                out[x.party] += sign * sum((v for a, v in x.postings() if a == account), ZERO)
        return out


def fiscal_start(on: date) -> date:
    return date(on.year if on.month >= 7 else on.year - 1, 7, 1)


def _months() -> list[tuple[int, int]]:
    out, on = [], date(2023, 7, 1)
    while on <= END:
        out.append((on.year, on.month))
        on = date(on.year + on.month // 12, on.month % 12 + 1, 1)
    return out


def _vat(net: Decimal) -> Decimal:
    return money(net * VAT_RATE / 100)


def simulate() -> Books:
    books = Books()
    prices = {code: price for code, _, _, price in ITEMS}
    numbers = iter(range(10001, 99999))
    checks = iter(range(5001, 99999))
    receipts = iter(range(1, 99999))

    def line(code: str, amount: Decimal, description: str, quantity: Decimal) -> Line:
        service = code.startswith("SRV")
        return Line("4100" if service else "4000", amount, description, code, quantity, prices[code],
                    kind="2" if service else "1")

    def vat(customer: tuple[str, str, bool], lines: list[Line]) -> list[Line]:
        """The VAT distribution on the goods lines, for a customer charged VAT."""
        goods = -sum((ln.amount for ln in lines if ln.kind == "1"), ZERO)
        return [Line("2310", -_vat(goods), "VAT 15%", agency=VAT, kind="0")] if customer[2] and goods else []

    def invoice(customer: tuple[str, str, bool], on: date, items: list[tuple[str, int]]) -> Transaction:
        ident = customer[0]
        lines = [line(code, -money(prices[code] * qty), dict((c, d) for c, d, _, _ in ITEMS)[code], Decimal(qty))
                 for code, qty in items]
        lines += vat(customer, lines)
        return books.add(Transaction("sales", on, str(next(numbers)), lines, ident, "1100", due=on + timedelta(days=30)))

    def receive(doc: Transaction, value: Decimal, on: date, discount: Decimal = ZERO) -> None:
        if on <= END:
            books.add(Transaction("receipts", on, f"RCPT-{next(receipts):04d}",
                                  [Line("1100", -(value + discount), f"Invoice {doc.reference}", paid=doc.reference,
                                        discount=discount)], doc.party, "1020", discount_account="4900"))

    def bill(vendor: str, on: date, account: str, net: Decimal, vat: bool, reference: str) -> Transaction:
        lines = [Line(account, net, f"{reference} {on:%m/%Y}")]
        if vat:
            lines.append(Line("2310", _vat(net), "Input VAT 15%"))
        return books.add(Transaction("purchases", on, reference, lines, vendor, "2000", due=on + timedelta(days=30)))

    def pay(docs: list[Transaction], on: date, discount: Decimal = ZERO) -> None:
        if on <= END:
            open_ = books.open_amounts(False)
            lines = [Line("2000", open_[d.reference], f"Purchase {d.reference}", paid=d.reference,
                          discount=discount if i == 0 else ZERO) for i, d in enumerate(docs)]
            books.add(Transaction("payments", on, str(next(checks)), lines, docs[0].party, "1020",
                                  discount_account="5050"))

    for m, (year, month) in enumerate(_months()):
        first = date(year, month, 1)
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        for n in range(6):
            customer = CUSTOMERS[(m + n) % len(CUSTOMERS)]
            if n == 3 and m % 4 == 0:
                customer = CUSTOMERS[(m + 2) % len(CUSTOMERS)]
            on = first + timedelta(days=1 + n * 4)
            items = [(ITEMS[(m + n + i) % len(ITEMS)][0], 1 + (m + n + i) % 5) for i in range(1 + n % 3)]
            doc = invoice(customer, on, items)
            total = doc.head_amount
            if n == 0:
                receive(doc, total, on + timedelta(days=15))
            elif n == 1:
                half = money(total / 2)
                receive(doc, half, on + timedelta(days=10))
                receive(doc, total - half, on + timedelta(days=30))
            elif n == 2 and m % 4 == 0:
                pass                                   # paid with the next invoice, of the same customer
            elif n == 3 and m % 4 == 0:
                # One receipt pays this invoice and the one before it.
                earlier = books.documents("sales")[-2]
                if on + timedelta(days=20) <= END:
                    books.add(Transaction("receipts", on + timedelta(days=20), f"RCPT-{next(receipts):04d}", [
                        Line("1100", -earlier.head_amount, f"Invoice {earlier.reference}", paid=earlier.reference),
                        Line("1100", -total, f"Invoice {doc.reference}", paid=doc.reference)],
                        customer[0], "1020"))
            elif n in (2, 3):
                receive(doc, total, on + timedelta(days=20))
            elif n == 4 and m % 3 == 1:
                discount = money(total * Decimal("0.02"))
                receive(doc, total - discount, on + timedelta(days=8), discount)
            elif n == 4:
                receive(doc, total, on + timedelta(days=25))
            elif m % 5 == 2:
                code, _ = items[0]
                credit = [line(code, money(prices[code]), "Returned", Decimal(-1))]
                credit += vat(customer, credit)
                books.add(Transaction("sales", on + timedelta(days=3), str(next(numbers)), credit, customer[0], "1100",
                                      credit=True, apply_to=doc.reference))
                receive(doc, books.open_amounts(True)[doc.reference], on + timedelta(days=20))
            elif on + timedelta(days=40) <= END - timedelta(days=45):
                receive(doc, total, on + timedelta(days=40))
        if m % 3 == 2:
            net = Decimal(6000 + 100 * m)
            books.add(Transaction("receipts", last, f"CASH-{year}{month:02d}", [
                Line("4100", -net, "Walk-in printing"), Line("2310", -_vat(net), "VAT 15%")], "", "1010"))

        rent = bill("V200", first, "6000", Decimal("30000.00"), False, f"RENT-{year}{month:02d}")
        pay([rent], first + timedelta(days=4))
        power = bill("V300", first + timedelta(days=9), "6100", Decimal(2000 + 37 * m), True, f"EEU-{year}{month:02d}")
        pay([power], first + timedelta(days=24))
        paper = bill("V100", first + timedelta(days=6), "5000", Decimal(12000 + 150 * m), True, f"MPS-{year}{month:02d}")
        if m % 6 == 3:
            books.add(Transaction("purchases", first + timedelta(days=12), f"MPS-{year}{month:02d}-CM", [
                Line("5000", Decimal("-1000.00"), "Short delivery"), Line("2310", -_vat(Decimal(1000)), "Input VAT 15%")],
                "V100", "2000", credit=True, apply_to=paper.reference))
        if m % 4 == 1:
            second = bill("V100", first + timedelta(days=14), "5000", Decimal("4800.00"), True, f"MPS-{year}{month:02d}B")
            pay([paper, second], first + timedelta(days=27))
        elif m % 3 == 0:
            pay([paper], first + timedelta(days=16), discount=money(Decimal(120 + m)))
        else:
            pay([paper], first + timedelta(days=26))
        books.add(Transaction("payments", last, "", [Line("6200", Decimal("150.00"), "Monthly service charge")],
                              "", "1020"))
        books.add(Transaction("general", last, f"DEP-{year}{month:02d}", [
            Line("6300", Decimal("1500.00"), "Depreciation"), Line("1550", Decimal("-1500.00"), "Depreciation")]))
        books.add(Transaction("general", last, f"PAY-{year}{month:02d}", [
            Line("6400", Decimal("45000.00"), "Salaries"), Line("1020", Decimal("-45000.00"), "Salaries")]))
        if m % 3 == 2:
            books.add(Transaction("general", last, f"TRF-{year}{month:02d}", [
                Line("1030", Decimal("100000.00"), "To savings"), Line("1020", Decimal("-100000.00"), "To savings")]))
    furniture = bill("V400", date(2023, 9, 11), "1500", Decimal("85000.00"), True, "HOF-2309")
    pay([furniture], date(2023, 10, 2))
    books.add(Transaction("receipts", date(2025, 3, 20), f"RCPT-{next(receipts):04d}",
                          [Line("1100", Decimal("-5000.00"), "Advance for April order")], "ALEM01", "1020"))
    books.transactions.sort(key=lambda x: x.on)
    return books


# ── Writing the files ─────────────────────────────────────────────────────────

def _short(on: date | None, four: bool = False) -> str:
    return "" if on is None else on.strftime("%m/%d/%Y" if four else "%m/%d/%y")


def _number(value: Decimal) -> str:
    return f"{value:.2f}"


def _shown(value: Decimal, sign: bool = False) -> str:
    """An amount as a Sage 50 report prints it: thousands separators, the currency sign on
    the lines that carry it."""
    text = f"{value:,.2f}"
    return f"{SIGN} {text}" if sign else text


def _csv(header: list[str], rows: list[list], encoding: str = "cp1252", above: list[list] | None = None) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerows(above or [])
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode(encoding)


def _bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def chart() -> bytes:
    return _csv(["Account ID", "Account Description", "Inactive", "Account Type"],
                [[c, n, "FALSE", str(k)] for c, n, k in CHART], encoding="utf-8-sig")


def customers() -> bytes:
    header = ["Customer ID", "Customer Name", "Inactive", "Bill to Contact First Name", "Bill to Contact Last Name",
              "Bill to Address-Line One", "Bill to Address-Line Two", "Bill to City", "Bill to State", "Bill to Zip",
              "Bill to Country", "Telephone 1", "Customer E-mail", "Customer Type", "Resale Number"]
    rows = [[ident, name, "FALSE", "", "", f"Kebele {i + 1:02d}", "", "Addis Ababa", "", "", "Ethiopia",
             f"+251 11 000 {i:04d}", f"accounts{i + 1}@example.com", "WHOLESALE", ""]
            for i, (ident, name, _) in enumerate(CUSTOMERS)]
    return _csv(header, rows)


def vendors() -> bytes:
    header = ["Vendor ID", "Vendor Name", "Inactive", "Contact", "Address-Line One", "Address-Line Two", "City",
              "State", "Zip", "Country", "Telephone 1", "Vendor E-mail", "Tax ID Number", "Vendor Type"]
    rows = [[ident, name, "FALSE", "", f"Woreda {i + 1:02d}", "", "Addis Ababa", "", "", "Ethiopia", "",
             f"billing{i + 1}@example.com", f"00{i + 1}2345678", "SUPPLY"] for i, (ident, name) in enumerate(VENDORS)]
    return _csv(header, rows)


def items() -> bytes:
    header = ["Item ID", "Item Description", "Inactive", "Item Class", "Description for Sales", "Sales Price 1",
              "Item Type", "Stocking U/M", "G/L Sales Account", "G/L Inventory Account", "G/L Cost of Sales Account",
              "Last Unit Cost", "Cost Method"]
    rows = [[code, name.split(",")[0], "FALSE", kind, name, _number(price), "", "Each",
             "4100" if code.startswith("SRV") else "4000", "1200", "5000", "", "Average"]
            for code, name, kind, price in ITEMS]
    return _csv(header, rows)


def employees() -> bytes:
    return _csv(["Employee ID", "Employee Name", "Inactive", "Address-Line One", "City", "State", "Zip", "Telephone 1",
                 "Pay Method"], [[i, n, "FALSE", "", "Addis Ababa", "", "", "", "Salary"] for i, n in EMPLOYEES])


def general(books: Books, four: bool = False) -> bytes:
    header = ["Date", "Reference", "Date Clear in Bank Rec", "Number of Distributions", "G/L Account", "Description",
              "Amount", "Job ID", "Used for Reimbursable Expense", "Transaction Period", "Transaction Number"]
    rows = []
    for n, x in enumerate(books.documents("general"), 1):
        rows += [[_short(x.on, four), x.reference, "", len(x.lines), ln.account, ln.description, _number(ln.amount), "",
                  "FALSE", "", n] for ln in x.lines]
    return _csv(header, rows)


def documents(books: Books, sales: bool, four: bool = False, alternate: bool = False) -> bytes:
    if sales:
        header = ["Customer ID", "Customer Name", "Invoice/CM #", "Apply to Invoice Number", "Credit Memo", "Date",
                  "Date Due", "Accounts Receivable Account", "Accounts Receivable Amount", "Sales Tax ID",
                  "Number of Distributions", "Invoice/CM Distribution", "Quantity", "Item ID", "Description",
                  "G/L Account", "Unit Price", "Tax Type", "Amount",
                  "Sales Tax Agency ID" if alternate else "Sales Tax Agency", "Transaction Period", "Transaction Number",
                  "Quote", "Beginning Balance Transaction", "Voided by Transaction"]
    else:
        invoice, apply = ("Invoice/CM #", "Apply to Invoice Number") if alternate else ("Invoice #", "Apply to Invoice #")
        header = ["Vendor ID", "Vendor Name", invoice, apply, "Credit Memo", "Date", "Date Due",
                  "Accounts Payable Account", "Accounts Payable Amount", "Number of Distributions",
                  "Invoice/CM Distribution", "Quantity", "Item ID", "Description", "G/L Account", "Unit Price",
                  "Amount", "Transaction Period", "Transaction Number", "Beginning Balance Transaction", "Row Type",
                  "Waiting on Bill"]
    names = dict((i, n) for i, n, *_ in CUSTOMERS) if sales else dict(VENDORS)
    rows = []
    for n, x in enumerate(books.documents("sales" if sales else "purchases"), 1):
        line_number = 0
        for ln in x.lines:
            line_number += 0 if ln.agency else 1
            head = [x.party, names[x.party], x.reference, x.apply_to, _bool(x.credit), _short(x.on, four),
                    _short(x.due, four), x.head, _number(x.head_amount)]
            body = [len(x.lines), 0 if ln.agency else line_number, f"{ln.quantity:g}" if ln.item else "", ln.item,
                    ln.description, ln.account, _number(ln.unit) if ln.item else ""]
            if sales:
                # Every distribution of an invoice that charges VAT names the agency; Tax Type 0 is its tax.
                charged = VAT if any(m.agency for m in x.lines) else ""
                rows.append([*head, charged, *body, ln.kind, _number(ln.amount), charged, "", n, "FALSE", "FALSE", ""])
            else:
                rows.append([*head, *body, _number(ln.amount), "", n, "FALSE", "", "FALSE"])
    return _csv(header, rows)


def cash(books: Books, sales: bool, four: bool = False, alternate: bool = False) -> bytes:
    check = "Check #" if alternate else "Check Number"
    if sales:
        header = ["Deposit Ticket ID", "Customer ID", "Customer Name", "Reference", "Receipt Number", "Date",
                  "Payment Method", "Cash Account", "Cash Amount", "Discount Account", "Number of Distributions",
                  "Invoice Paid", "Discount Amount", "Quantity", "Item ID", "Description", "G/L Account", "Unit Price",
                  "Amount", "Transaction Period", "Transaction Number", "Vendor Receipt", "Prepayment",
                  "Total Paid on Invoice(s)"]
    else:
        header = ["Vendor ID", "Vendor Name", check, "Date", "Memo", "Cash Account", "Cash Amount", "Discount Account",
                  "Number of Distributions", "Invoice Paid", "Discount Amount", "Quantity", "Item ID", "Description",
                  "G/L Account", "Unit Price", "Amount", "Transaction Period", "Transaction Number",
                  "Voided by Transaction", "Customer Payment", "Prepayment", "Total Paid on Invoice(s)",
                  "Detailed Payment"]
    names = dict((i, n) for i, n, *_ in CUSTOMERS) if sales else dict(VENDORS)
    rows = []
    for n, x in enumerate(books.documents("receipts" if sales else "payments"), 1):
        # The cash the transaction paid on invoices: each applied line less the discount taken on it.
        paid = _number(sum((abs(ln.amount) - ln.discount for ln in x.lines if ln.paid), ZERO))
        for ln in x.lines:
            body = [len(x.lines), ln.paid, _number(ln.discount) if ln.discount else "", "", "", ln.description,
                    "" if ln.paid and not alternate else ln.account, "", _number(ln.amount), "", n]
            if sales:
                rows.append([_short(x.on, four), x.party, names.get(x.party, ""), x.reference, x.reference,
                             _short(x.on, four), "Check", x.head, _number(x.head_amount), x.discount_account, *body,
                             "FALSE", "FALSE", paid])
            else:
                rows.append([x.party, names.get(x.party, "Commercial Bank"), x.reference, _short(x.on, four), "",
                             x.head, _number(x.head_amount), x.discount_account, *body, "", "FALSE", "FALSE", paid,
                             "TRUE"])
    return _csv(header, rows)


def _report_above(title: str, on: date) -> list[list]:
    return [[COMPANY], [title], [f"As of {on:%b} {on.day}, {on.year}"],
            ["Filter Criteria includes: Report order is by ID. Report is printed in Detail Format."], []]


def trial_balance(books: Books, on: date) -> bytes:
    """Balances as of `on`: profit and loss for its fiscal year so far, earlier years' profit
    in retained earnings."""
    start = fiscal_start(on)
    now, before = books.balances(on), books.balances(start - timedelta(days=1))
    if on == OPENED:
        before = defaultdict(lambda: ZERO)
    earlier = sum((before[c] for c in PROFIT_AND_LOSS), ZERO)
    rows, debits, credits = [], ZERO, ZERO
    for code, name, _ in CHART:
        value = now[code] - before[code] if code in PROFIT_AND_LOSS else now[code] + (earlier if code == "3900" else ZERO)
        if not value:
            continue
        first = not rows
        rows.append([code, name, _shown(value, first) if value > 0 else "", _shown(-value, first) if value < 0 else ""])
        debits += max(value, ZERO)
        credits += max(-value, ZERO)
    rows.append(["", "Total:", _shown(debits, True), _shown(credits, True)])
    return _csv(["Account ID", "Account Description", "Debit Amt", "Credit Amt"], rows,
                above=_report_above("General Ledger Trial Balance", on))


def aged(books: Books, sales: bool) -> bytes:
    """Open invoices or purchases as of the last day, then each customer's or vendor's other
    open items, block by block with the ID on the first row; a workbook, as Excel exports it."""
    title, party = ("Aged Receivables", "Customer") if sales else ("Aged Payables", "Vendor")
    names = dict((i, n) for i, n, *_ in CUSTOMERS) if sales else dict(VENDORS)
    open_ = books.open_amounts(sales)
    owed = books.owed(sales)
    by_party: dict[str, list[tuple[str, date, Decimal]]] = defaultdict(list)
    for d in books.documents("sales" if sales else "purchases"):
        if open_[d.reference]:
            by_party[d.party].append((d.reference, d.on, open_[d.reference]))
    for x in books.documents("receipts" if sales else "payments"):
        if x.party and not any(ln.paid for ln in x.lines):
            # A payment applied to nothing: the customer's credit, the vendor's advance.
            sign = 1 if sales else -1
            by_party[x.party].append((x.reference, x.on, sign * sum((ln.amount for ln in x.lines), ZERO)))
    lines = [*_report_above(title, END), [f"{party} ID", party, "Invoice/CM #", "Date", "0 - 30", "31 - 60",
                                          "61 - 90", "Over 90 days", "Amount Due"]]
    total = ZERO
    for ident in sorted(by_party):
        assert sum((v for _, _, v in by_party[ident]), ZERO) == owed[ident], ident
        for i, (number, on, value) in enumerate(by_party[ident]):
            age = (END - on).days
            buckets = [value if lo <= age < hi else ZERO for lo, hi in ((0, 31), (31, 61), (61, 91), (91, 10 ** 6))]
            lines.append([ident if i == 0 else "", names[ident] if i == 0 else "", number, _short(on),
                          *[_shown(b) if b else "" for b in buckets], _shown(value, True)])
        lines.append(["", "", "", "", "", "", "", "", _shown(owed[ident], True)])
        total += owed[ident]
    lines.append(["Report Total", "", "", "", "", "", "", "", _shown(total, True)])
    book = openpyxl.Workbook()
    for line in lines:
        book.active.append(line)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


TRIAL_BALANCE_DATES = (OPENED, date(2024, 6, 30), END)


def files(books: Books | None = None, *, alternate: bool = False, four: bool = False) -> dict[str, bytes]:
    """Every file of the export set, by the name Sage 50 gives it. `alternate` writes the
    names other releases and templates use, `four` four-digit years."""
    books = books or simulate()
    out = {"CHART.CSV": chart(), "CUSTOMER.CSV": customers(), "VENDOR.CSV": vendors(), "INVENTRY.CSV": items(),
           "EMPLOYEE.CSV": employees(), "GENERAL.CSV": general(books, four),
           "SALES.CSV": documents(books, True, four, alternate), "RECEIPTS.CSV": cash(books, True, four, alternate),
           "PURCHASE.CSV": documents(books, False, four, alternate), "PAYMENTS.CSV": cash(books, False, four, alternate),
           "Aged Receivables.xlsx": aged(books, True), "Aged Payables.xlsx": aged(books, False)}
    for on in TRIAL_BALANCE_DATES:
        out[f"Trial Balance {on.isoformat()}.csv"] = trial_balance(books, on)
    return out
