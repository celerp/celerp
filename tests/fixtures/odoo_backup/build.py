# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A fictitious company's books written as an Odoo database manager backup.

The trading is one set of records, written as Odoo 13, 14 or 17 lays its tables out:
a zip of `manifest.json`, a plain PostgreSQL `dump.sql` whose COPY blocks hold the
rows, and `filestore/` with the attached files. Odoo 17 stores translated names as
JSON and keeps account types on the account; Odoo 13 and 14 keep them in a type
record named by its XML id and call the lot table `stock_production_lot`, and Odoo 13
names a payment only on its journal items. Odoo 17 calls a stock move line's quantity done
`quantity`, and earlier versions `qty_done`.

The company, Harbour Supplies Ltd, keeps its books in USD from January to April 2024:
capital paid in, a vendor bill for stock and its payment, customer invoices paid in
full, in part and in EUR at a later rate with Odoo's exchange difference entry, an open
credit note, a vendor refund applied to a bill, a draft and a cancelled invoice, a draft
journal entry, stock received in two lots, delivered, moved between shelves, counted and revalued,
and files attached to an invoice, contacts and a product.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal
COMPANY = "Harbour Supplies Ltd"
BASE, FOREIGN = "USD", "EUR"
USD, EUR = 2, 1

# id, code, name, Odoo 17 account type, reconcile
ACCOUNTS = [
    (1, "101000", "Bank", "asset_cash", False),
    (2, "101401", "Outstanding Receipts", "asset_current", True),
    (3, "101402", "Outstanding Payments", "asset_current", True),
    (4, "121000", "Account Receivable", "asset_receivable", True),
    (5, "211000", "Account Payable", "liability_payable", True),
    (6, "251000", "Tax Received", "liability_current", False),
    (7, "131000", "Tax Paid", "asset_current", False),
    (8, "400000", "Product Sales", "income", False),
    (9, "600000", "Expenses", "expense", False),
    (10, "500000", "Cost of Goods Sold", "expense_direct_cost", False),
    (11, "110100", "Stock Valuation", "asset_current", False),
    (12, "110200", "Stock Interim (Received)", "asset_current", False),
    (13, "999999", "Undistributed Profits/Losses", "equity_unaffected", False),
    (14, "300000", "Capital", "equity", False),
    (15, "441000", "Foreign Exchange Gain", "income_other", False),
    (16, "641000", "Foreign Exchange Loss", "expense", False),
    (17, "115000", "Withholding Tax Receivable", "asset_current", False),
    (18, "215000", "Withholding Tax Payable", "liability_current", False),
    (19, "900000", "Guarantees Given", "off_balance", False),
]
CODES = {i: code for i, code, *_ in ACCOUNTS}
GROUPS = {"asset": "asset", "liability": "liability", "equity": "equity", "income": "income", "expense": "expense"}
LEGACY = {  # Odoo 17 type -> (Odoo 14 type record XML id, internal type, internal group)
    "asset_cash": ("data_account_type_liquidity", "liquidity", "asset"),
    "asset_current": ("data_account_type_current_assets", "other", "asset"),
    "asset_receivable": ("data_account_type_receivable", "receivable", "asset"),
    "liability_payable": ("data_account_type_payable", "payable", "liability"),
    "liability_current": ("data_account_type_current_liabilities", "other", "liability"),
    "income": ("data_account_type_revenue", "other", "income"),
    "income_other": ("data_account_type_other_income", "other", "income"),
    "expense": ("data_account_type_expenses", "other", "expense"),
    "expense_direct_cost": ("data_account_type_direct_costs", "other", "expense"),
    "equity": ("data_account_type_equity", "other", "equity"),
    "equity_unaffected": ("data_unaffected_earnings", "other", "equity"),
    "off_balance": ("data_account_off_sheet", "other", "off_balance"),
}

# id, name, parent, customer rank, supplier rank, email
PARTNERS = [
    (1, COMPANY, None, 0, 0, "office@harbour.example"),
    (2, "OdooBot", None, 0, 0, "odoobot@example.com"),
    (3, "Administrator", None, 0, 0, "admin@harbour.example"),
    (7, "Lakeshore Trading Co", None, 3, 0, "accounts@lakeshore.example"),
    (8, "Pine & Co", None, 1, 0, "hello@pine.example"),
    (9, "Ann Pine", 8, 0, 0, "ann@pine.example"),
    (10, "Northwind Wholesale", None, 0, 2, "billing@northwind.example"),
    (11, "Lakeshore Trading Co", None, 1, 0, "other@lakeshore.example"),
]
# product id, template id, default code, name, type, list price
PRODUCTS = [(1, 1, "WID-1", "Widget", "product", D("50")), (2, 2, "SRV", "Consulting", "service", D("100"))]
# id, name, use, rate, tax account
TAXES = [(1, "15%", "sale", D("15"), 6), (2, "15%", "purchase", D("15"), 7), (3, "PST 7%", "sale", D("7"), 6),
         (4, "WHT 3%", "sale", D("-3"), 17), (5, "WHT 3%", "purchase", D("-3"), 18)]
WIDGET, CONSULTING = 1, 2
SALE_TAX, PURCHASE_TAX, PROVINCIAL_TAX, SALE_WHT, PURCHASE_WHT = 1, 2, 3, 4, 5
# Analytic plans and accounts (id, name, plan). From Odoo 16 a line spreads across accounts by
# percent; Odoo 13 to 15 name one account of the first plan and tag the rest by name.
ANALYTIC_PLANS = [(1, "Projects"), (2, "Regions")]
ANALYTIC_ACCOUNTS = [(1, "Project Alpha", 1), (2, "Project Beta", 1), (3, "North", 2)]
# (move id, line index) -> percent per analytic account.
ANALYTIC = {(4, 1): {1: D(60), 2: D(40), 3: D(100)}, (20, 1): {3: D(100)}}


@dataclass
class Line:
    account: int
    balance: Decimal                       # company currency, debit positive
    kind: str | None = None                # product, tax or payment_term on an invoice or bill; None on an entry
    partner: int | None = None
    currency: int = USD
    amount_currency: Decimal | None = None   # in `currency`; the balance when that is the company's
    product: int | None = None
    quantity: Decimal = D(0)
    price_unit: Decimal = D(0)
    subtotal: Decimal = D(0)
    total: Decimal = D(0)
    taxes: tuple[int, ...] = ()
    tax: int | None = None                 # the tax a tax line books
    name: str = ""
    id: int = 0


@dataclass
class Move:
    id: int
    name: str
    move_type: str
    state: str
    date: str
    lines: list[Line]
    partner: int | None = None
    currency: int = USD
    ref: str = ""
    payment: tuple[str, str] | None = None     # (partner type, payment type)
    total: Decimal = D(0)
    tax: Decimal = D(0)


def _invoice(id_, name, move_type, date, partner, items, state="posted", currency=USD, rate=D(1)):
    """An invoice or bill of (product, quantity, price, tax or taxes) lines at `rate` company
    units per document unit, one tax line per tax; the customer or supplier owes the total."""
    sales = move_type.startswith("out_")
    refund = move_type.endswith("refund")
    sign = (-1 if sales else 1) * (-1 if refund else 1)        # of the product lines' balance
    lines, tax_lines, net_total = [], {}, D(0)
    for product, quantity, price, taxes in items:
        taxes = taxes if isinstance(taxes, tuple) else (taxes,) if taxes else ()
        subtotal = quantity * price
        amounts = [(subtotal * TAXES[tax - 1][3] / 100).quantize(D("0.01")) for tax in taxes]
        account = 8 if sales else (12 if product == WIDGET else 9)
        lines.append(Line(account, sign * subtotal * rate, "product", partner, currency, sign * subtotal, product,
                          D(quantity), price, subtotal, subtotal + sum(amounts, D(0)), taxes,
                          name=dict((p[0], p[3]) for p in PRODUCTS)[product]))
        for tax, amount in zip(taxes, amounts):
            tax_lines[tax] = tax_lines.get(tax, D(0)) + amount
        net_total += subtotal
    for tax, amount in tax_lines.items():
        lines.append(Line(TAXES[tax - 1][4], sign * amount * rate, "tax", partner, currency, sign * amount,
                          tax=tax, name=TAXES[tax - 1][1]))
    tax_total = sum(tax_lines.values(), D(0))
    total = net_total + tax_total
    lines.append(Line(4 if sales else 5, -sign * total * rate, "payment_term", partner, currency, -sign * total,
                      name=name))
    return Move(id_, name, move_type, state, date, lines, partner, currency, total=total, tax=tax_total)


def _entry(id_, name, date, lines, ref="", partner=None, currency=USD, payment=None, state="posted"):
    return Move(id_, name, "entry", state, date, [Line(*ln) for ln in lines], partner, currency, ref, payment)


def _bulk(lines: int) -> list[Move]:
    """One entry of `lines` lines, each with its own label and amount, as a large book holds."""
    if not lines:
        return []
    amounts = [D(f"{n % 9973 + 1}.{n % 100:02d}") for n in range(lines // 2)]
    booked = [Line(account, sign * a, name=f"Transfer {n} of {len(amounts)}")
              for n, a in enumerate(amounts) for account, sign in ((1, 1), (14, -1))]
    return [Move(25, "MISC/2024/05/0001", "entry", "posted", "2024-05-01", booked, ref="Bulk transfers")]


def moves(bulk_lines: int = 0) -> list[Move]:
    """The company's entries, invoices and bills, plus one entry of `bulk_lines` lines."""
    return [
        _entry(1, "MISC/2024/01/0001", "2024-01-05", [(1, D(10000)), (14, D(-10000))], ref="Capital paid in"),
        _invoice(2, "BILL/2024/01/0001", "in_invoice", "2024-01-10", 10, [(WIDGET, 10, D(30), PURCHASE_TAX)]),
        _entry(3, "STJ/2024/00001", "2024-01-10", [(11, D(300)), (12, D(-300))], ref="WH/IN/00001"),
        _invoice(4, "INV/2024/00001", "out_invoice", "2024-01-20", 7,
                 [(WIDGET, 4, D(50), SALE_TAX), (CONSULTING, 1, D(100), SALE_TAX)]),
        _entry(5, "STJ/2024/00002", "2024-01-20", [(10, D(120)), (11, D(-120))], ref="WH/OUT/00001"),
        _entry(6, "PBNK1/2024/00001", "2024-02-05", [(2, D(345)), (4, D(-345), None, 7)], partner=7,
               payment=("customer", "inbound")),
        _invoice(7, "INV/2024/00002", "out_invoice", "2024-02-10", 9, [(CONSULTING, 2, D(100), SALE_TAX)]),
        _entry(8, "PBNK1/2024/00002", "2024-03-01", [(2, D(100)), (4, D(-100), None, 8)], partner=8,
               payment=("customer", "inbound")),
        _invoice(9, "RINV/2024/00001", "out_refund", "2024-02-15", 7, [(WIDGET, 1, D(50), SALE_TAX)]),
        _entry(10, "PBNK1/2024/00003", "2024-03-05", [(5, D(345), None, 10), (3, D(-345))], partner=10,
               payment=("supplier", "outbound")),
        _entry(11, "BNK1/2024/00001", "2024-03-20", [(1, D(345)), (2, D(-345))], ref="Lakeshore deposit"),
        _invoice(12, "BILL/2024/03/0001", "in_invoice", "2024-03-25", 10, [(CONSULTING, 2, D(100), PURCHASE_TAX)]),
        _invoice(13, "RBILL/2024/03/0001", "in_refund", "2024-03-28", 10, [(CONSULTING, D("0.3"), D(100),
                                                                            PURCHASE_TAX)]),
        _invoice(14, "/", "out_invoice", "2024-03-30", 7, [(CONSULTING, 1, D(100), SALE_TAX)], state="draft"),
        _invoice(15, "INV/2024/00003", "out_invoice", "2024-03-30", 8, [(WIDGET, 1, D(50), SALE_TAX)],
                 state="cancel"),
        _entry(16, "/", "2024-03-31", [(9, D(40)), (1, D(-40))], state="draft"),
        _invoice(17, "INV/2024/00004", "out_invoice", "2024-04-02", 7, [(CONSULTING, 1, D(100), None)],
                 currency=EUR, rate=D("1.10")),
        _entry(18, "PBNK1/2024/00004", "2024-04-20",
               [(2, D(112), None, None, EUR, D(100)), (4, D(-112), None, 7, EUR, D(-100))], partner=7, currency=EUR,
               payment=("customer", "inbound")),
        _entry(19, "EXCH/2024/04/0001", "2024-04-20", [(4, D(2), None, 7, EUR, D(0)), (15, D(-2), None, None, EUR,
                                                                                         D(0))], partner=7),
        _entry(20, "STJ/2024/00003", "2024-03-01", [(11, D(30)), (9, D(-30))], ref="Inventory adjustment"),
        _entry(21, "STJ/2024/00004", "2024-03-15", [(11, D(10)), (12, D(-10))], ref="Landed cost"),
        # Two taxes on one line, a sale the customer withholds tax from, and a bill the company
        # withholds tax from beside the tax it pays.
        _invoice(22, "INV/2024/00005", "out_invoice", "2024-04-10", 8,
                 [(CONSULTING, 1, D(100), (SALE_TAX, PROVINCIAL_TAX)), (WIDGET, 1, D(50), SALE_TAX)]),
        _invoice(23, "INV/2024/00006", "out_invoice", "2024-04-12", 7, [(CONSULTING, 2, D(100), SALE_WHT)]),
        _invoice(24, "BILL/2024/04/0001", "in_invoice", "2024-04-15", 10,
                 [(CONSULTING, 1, D(100), (PURCHASE_TAX, PURCHASE_WHT))]),
        # A guarantee kept on off-balance accounts, outside the books.
        _entry(26, "MISC/2024/04/0001", "2024-04-20", [(19, D(500)), (19, D(-500))], ref="Bank guarantee"),
    ] + _bulk(bulk_lines)


def _payment_term(moves_: dict[int, Move], move: int) -> Line:
    return next(ln for ln in moves_[move].lines if ln.account in (4, 5))


# (debit move, credit move, amount, amount in the lines' own currency where it is not the
# company's): the payment term or counterpart lines each pair reconciles.
PARTIALS = [(4, 6, D(345)), (7, 8, D(100)), (10, 2, D(345)), (13, 12, D("34.50")), (17, 18, D(110), D(100)),
            (19, 18, D(2), D(0))]

# Stock locations: id, name, usage
LOCATIONS = [(1, "Partners/Vendors", "supplier"), (2, "Partners/Customers", "customer"),
             (5, "Virtual Locations/Inventory adjustment", "inventory"), (8, "WH/Stock", "internal"),
             (9, "WH/Stock/Shelf 2", "internal")]
# id, reference, date, product, quantity, from, to, state, valuation layer value
STOCK_MOVES = [
    (1, "WH/IN/00001", "2024-01-10 09:00:00", WIDGET, D(10), 1, 8, "done", D(300)),
    (2, "WH/OUT/00001", "2024-01-20 10:00:00", WIDGET, D(4), 8, 2, "done", D(-120)),
    (3, "WH/INT/00001", "2024-02-01 11:00:00", WIDGET, D(2), 8, 9, "done", None),
    (4, "Product Quantity Updated", "2024-03-01 12:00:00", WIDGET, D(1), 5, 8, "done", D(30)),
    (5, "WH/OUT/00002", "2024-04-25 12:00:00", WIDGET, D(3), 8, 2, "assigned", None),
]
LANDED_COST = (D(10), "2024-03-15 08:00:00")
# The widget is tracked by lot. id, move, lot, quantity done, from, to
STOCK_MOVE_LINES = [(1, 1, 1, D(6), 1, 8), (2, 1, 2, D(4), 1, 8), (3, 2, 2, D(4), 8, 2), (4, 3, 1, D(2), 8, 9),
                    (5, 4, 1, D(1), 5, 8), (6, 5, 1, D(3), 8, 2)]
# id, name, ratio to the reference unit, rounding; every product counts in units
UOMS = [(1, "Units", "1", "0.01"), (2, "Dozens", "0.08333333333333333", "0.01")]
UNITS, DOZENS = 1, 2
# id, name; the third lot was made and never stocked
LOTS = [(1, "LOT-0001"), (2, "LOT-0002"), (3, "LOT-0003")]
# location, product, lot, quantity
QUANTS = [(8, WIDGET, 1, D(5)), (9, WIDGET, 1, D(2))]

PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
# id, model, record, field, name, content, stored in the database, in the filestore
ATTACHMENTS = [
    (1, "account.move", 4, None, "INV-2024-00001.pdf", PDF, False, True),
    (2, "res.partner", 7, None, "contract.txt", b"Net 30 days.\n", False, True),
    (3, "product.template", 1, None, "widget.png", PNG, False, True),
    (4, "account.move", 1, None, "capital.pdf", PDF, False, True),
    (5, "account.move", 4, None, "setup.exe", b"MZ\x90\x00", False, True),
    (6, "res.partner", 8, None, "photo.jpg", b"not a jpeg", False, True),
    (7, "product.template", 1, "image_1920", "image_1920", PNG, False, True),
    (8, "ir.ui.view", 1, None, "web.assets.css", b"body{}", False, True),
    (9, "res.partner", 10, None, "terms.txt", b"Payment on delivery.\n", True, False),
    (10, "res.partner", 7, None, "lost.pdf", PDF + b"%lost\n", False, False),
    (11, "product.template", 1, "image_128", "image_128", PNG + b"small", False, True),
    (12, "sale.order", 3, None, "quote.pdf", PDF + b"%quote\n", False, True),
]


@dataclass
class Tables:
    rows: dict[str, tuple[list[str], list[list]]] = field(default_factory=dict)

    def add(self, table: str, columns: list[str], rows: list[list]) -> None:
        self.rows[table] = (columns, rows)


def _residuals(all_moves: list[Move]) -> tuple[dict[tuple[int, int], Decimal], dict[int, Decimal]]:
    """Each reconcilable line's open amount in company currency, and each document's open
    amount in its own currency."""
    by_id = {m.id: m for m in all_moves}
    open_: dict[tuple[int, int], Decimal] = {}
    for m in all_moves:
        for n, ln in enumerate(m.lines):
            if ln.account in (2, 3, 4, 5) and m.state == "posted":
                open_[(m.id, n)] = ln.balance
    for debit, credit, amount, *_ in PARTIALS:
        for move, sign in ((debit, -1), (credit, 1)):
            n = next(n for n, ln in enumerate(by_id[move].lines) if ln.account in (4, 5))
            open_[(move, n)] += sign * amount
    documents = {}
    for m in all_moves:
        if m.move_type != "entry":
            n = next(n for n, ln in enumerate(m.lines) if ln.account in (4, 5))
            line = m.lines[n]
            rate = line.balance / line.amount_currency if line.amount_currency else D(1)
            documents[m.id] = abs(open_.get((m.id, n), line.balance) / rate) if m.state == "posted" else m.total
    return open_, documents


def _bool(value: bool) -> str:
    return "t" if value else "f"


def tables(version: int, bulk_lines: int = 0) -> Tables:
    """Every table the backup's dump holds, laid out as Odoo `version` lays it out, with an
    entry of `bulk_lines` lines when a test needs a large book."""
    modern = version >= 16

    def name(text: str):
        return json.dumps({"en_US": text}, ensure_ascii=False) if modern else text

    out = Tables()
    out.add("res_currency", ["id", "name", "symbol", "decimal_places", "active"],
            [[USD, BASE, "$", 2, "t"], [EUR, FOREIGN, "€", 2, "t"], [3, "GBP", "£", 2, "f"]])
    out.add("res_company", ["id", "name", "partner_id", "currency_id", "income_currency_exchange_account_id",
                            "expense_currency_exchange_account_id", "fiscalyear_last_month"],
            [[1, COMPANY, 1, USD, 15, 16, "12"]])
    out.add("res_partner", ["id", "company_id", "name", "parent_id", "commercial_partner_id", "street", "city",
                            "zip", "email", "phone", "vat", "active", "is_company", "customer_rank",
                            "supplier_rank"],
            [[i, None, n, parent, parent or i, "1 Quay Road" if i == 7 else None, "Portsmouth" if i == 7 else None,
              "PO1 1AA" if i == 7 else None, email, None, "GB123456789" if i == 10 else None, "t",
              _bool(parent is None), c, s]
             for i, n, parent, c, s, email in PARTNERS])
    if modern:
        out.add("account_account", ["id", "currency_id", "company_id", "code", "account_type", "internal_group",
                                    "name", "deprecated", "reconcile"],
                [[i, None, 1, code, kind, LEGACY[kind][2], name(n), "f", _bool(rec)]
                 for i, code, n, kind, rec in ACCOUNTS])
    else:
        types = sorted({kind for *_, kind, _ in ACCOUNTS})
        type_ids = {kind: n for n, kind in enumerate(types, 1)}
        out.add("account_account_type", ["id", "name", "type", "internal_group", "include_initial_balance"],
                [[type_ids[k], k.replace("_", " ").title(), LEGACY[k][1], LEGACY[k][2], "t"] for k in types])
        out.add("ir_model_data", ["id", "module", "model", "name", "res_id", "noupdate"],
                [[n, "account", "account.account.type", LEGACY[k][0], type_ids[k], "t"]
                 for n, k in enumerate(types, 1)]
                + [[100, "base", "res.partner", "main_partner", 1, "t"]]
                # A name for each exported record, as a database used for some years holds.
                + [[101 + n, "__export__", "account.move.line", f"line_{n}", n, "f"] for n in range(bulk_lines)])
        out.add("account_account", ["id", "currency_id", "company_id", "code", "user_type_id", "internal_type",
                                    "internal_group", "name", "deprecated", "reconcile"],
                [[i, None, 1, code, type_ids[kind], LEGACY[kind][1], LEGACY[kind][2], n, "f", _bool(rec)]
                 for i, code, n, kind, rec in ACCOUNTS])
    out.add("account_tax", ["id", "company_id", "sequence", "type_tax_use", "amount_type", "name", "amount",
                            "active", "price_include"],
            [[i, 1, i, use, "percent", name(n), f"{rate:.4f}", "t", "f"] for i, n, use, rate, _ in TAXES])
    repartition = []
    for i, _, _, _, account in TAXES:
        for document, base_id in (("invoice", 4 * i - 3), ("refund", 4 * i - 1)):
            repartition += [(base_id, i, document, "base", None), (base_id + 1, i, document, "tax", account)]
    if modern:
        out.add("account_tax_repartition_line", ["id", "account_id", "tax_id", "company_id", "repartition_type",
                                                 "document_type", "factor_percent"],
                [[r, account, tax, 1, kind, document, "100"] for r, tax, document, kind, account in repartition])
    else:
        out.add("account_tax_repartition_line", ["id", "account_id", "invoice_tax_id", "refund_tax_id",
                                                 "company_id", "repartition_type", "factor_percent"],
                [[r, account, tax if document == "invoice" else None, tax if document == "refund" else None, 1,
                  kind, "100"] for r, tax, document, kind, account in repartition])
    template_columns = ["id", "categ_id", "uom_id", "company_id", "type", "name", "description_sale", "list_price",
                        "active", "sale_ok", "purchase_ok"]
    if modern:
        template_columns.insert(4, "detailed_type")
    out.add("product_template", template_columns,
            [[t, 1, 1, None, *((kind,) if modern else ()), kind, name(n), name(f"{n} for sale"), f"{price}", "t",
              "t", "t"] for _, t, _, n, kind, price in PRODUCTS])
    out.add("product_product", ["id", "product_tmpl_id", "default_code", "barcode", "active"],
            [[p, t, code, None, "t"] for p, t, code, *_ in PRODUCTS])
    _ledger(out, version, bulk_lines)
    _stock(out, version)
    _attachments(out)
    out.add("ir_ui_view", ["id", "name", "type", "arch_db"], [[1, "web.layout", "qweb", "<t>\t\\tab</t>"]])
    return out


def _ledger(out: Tables, version: int, bulk_lines: int) -> None:
    modern = version >= 16
    # Odoo 13 calls the move type `type`, names a payment only on its lines, and leaves the
    # currency and foreign amount of a line in the company's currency empty.
    v13 = version == 13
    all_moves = moves(bulk_lines)
    open_, documents = _residuals(all_moves)
    move_rows, line_rows, tax_rel, payments, tags = [], [], [], [], []
    line_id = 0
    first_line: dict[tuple[int, int], int] = {}
    for m in all_moves:
        payment_id = len(payments) + 1 if m.payment else None
        if m.payment:
            partner_type, payment_type = m.payment
            amount = abs(next(ln.amount_currency or ln.balance for ln in m.lines if ln.account in (2, 3)))
            payments.append([payment_id, m.id, m.currency, m.partner, partner_type, payment_type, f"{amount}", "f"])
        posted = m.state == "posted"
        move_rows.append([m.id, 1, 1, m.name, m.ref or None, m.state, m.move_type, m.date,
                          m.date if m.move_type != "entry" else None, m.date if m.move_type != "entry" else None,
                          m.partner, m.partner if m.partner is None else dict(
                              (p[0], p[2] or p[0]) for p in PARTNERS)[m.partner], m.currency,
                          f"{m.tax}", f"{m.total}",
                          f"{documents.get(m.id, D(0))}", *(() if v13 else (payment_id,))])
        for n, ln in enumerate(m.lines):
            line_id += 1
            first_line[(m.id, n)] = line_id
            balance = ln.balance
            foreign = ln.amount_currency if ln.amount_currency is not None else balance
            currency = ln.currency
            if v13 and currency == USD:
                currency, foreign = None, D(0)
            if modern:
                display = ln.kind if m.move_type != "entry" else None
                version_columns = [display]
            else:
                version_columns = [None, _bool(ln.kind in ("tax", "payment_term")), "f"]
            analytic = ANALYTIC.get((m.id, n), {})
            if modern:
                version_columns.append(json.dumps({str(a): float(p) for a, p in analytic.items()}) if analytic
                                       else None)
            else:
                version_columns.append(next((a for a in analytic if ANALYTIC_ACCOUNTS[a - 1][2] == 1), None))
                tags += [[line_id, a] for a in analytic if ANALYTIC_ACCOUNTS[a - 1][2] != 1]
            partner = ln.partner if ln.partner is not None else (m.partner if ln.account in (4, 5) else None)
            line_rows.append([line_id, m.id, 1, 1, USD, n, ln.account, currency, partner, m.name, m.state,
                              ln.name or m.name, m.date, f"{max(balance, D(0))}", f"{max(-balance, D(0))}",
                              f"{balance}", f"{foreign}", f"{open_.get((m.id, n), D(0)) if posted else D(0)}",
                              ln.product, f"{ln.quantity}", f"{ln.price_unit}", f"{ln.subtotal}", f"{ln.total}",
                              "0", ln.tax, payment_id, *version_columns])
            tax_rel += [[line_id, tax] for tax in ln.taxes]
    # A section heading and a note on the first invoice: layout lines with no account or amount.
    invoice = next(m for m in all_moves if m.id == 4)
    for display in ("line_section", "line_note"):
        line_id += 1
        line_rows.append([line_id, invoice.id, 1, 1, USD, len(invoice.lines), None, None, None, invoice.name,
                          invoice.state, display.replace("_", " ").title(), invoice.date, "0", "0", "0", "0", "0",
                          None, "0", "0", "0", "0", "0", None, None,
                          *([display, None] if modern else [display, "f", "f", None])])
    out.add("account_move",["id", "journal_id", "company_id", "name", "ref", "state", "type" if v13 else "move_type",
                             "date", "invoice_date", "invoice_date_due", "partner_id", "commercial_partner_id",
                             "currency_id", "amount_tax", "amount_total", "amount_residual",
                             *(() if v13 else ("payment_id",))], move_rows)
    line_columns = ["id", "move_id", "journal_id", "company_id", "company_currency_id", "sequence", "account_id",
                    "currency_id", "partner_id", "move_name", "parent_state", "name", "date", "debit", "credit",
                    "balance", "amount_currency", "amount_residual", "product_id", "quantity", "price_unit",
                    "price_subtotal", "price_total", "discount", "tax_line_id", "payment_id"]
    line_columns += ["display_type", "analytic_distribution"] if modern else [
        "display_type", "exclude_from_invoice_tab", "is_rounding_line", "analytic_account_id"]
    out.add("account_move_line", line_columns, line_rows)
    if modern:
        out.add("account_analytic_plan", ["id", "name", "company_id"],
                [[i, json.dumps({"en_US": n}), None] for i, n in ANALYTIC_PLANS])
        out.add("account_analytic_account", ["id", "name", "plan_id", "company_id", "active"],
                [[i, json.dumps({"en_US": n}), plan, 1, "t"] for i, n, plan in ANALYTIC_ACCOUNTS])
    else:
        out.add("account_analytic_account", ["id", "name", "group_id", "company_id", "active"],
                [[i, n, None, 1, "t"] for i, n, plan in ANALYTIC_ACCOUNTS if plan == 1])
        out.add("account_analytic_tag", ["id", "name", "company_id", "active"],
                [[i, n, 1, "t"] for i, n, plan in ANALYTIC_ACCOUNTS if plan != 1])
        out.add("account_analytic_tag_account_move_line_rel", ["account_move_line_id", "account_analytic_tag_id"],
                tags)
    out.add("account_move_line_account_tax_rel", ["account_move_line_id", "account_tax_id"], tax_rel)
    by_id = {m.id: m for m in all_moves}
    # Odoo 14 states the amount in each line's currency; Odoo 13 in the currency both lines share.
    partials = []
    for n, (debit, credit, amount, *foreign) in enumerate(PARTIALS, 1):
        d = next(i for i, ln in enumerate(by_id[debit].lines) if ln.account in (4, 5))
        c = next(i for i, ln in enumerate(by_id[credit].lines) if ln.account in (4, 5))
        own = foreign[0] if foreign else amount
        partials.append([n, first_line[(debit, d)], first_line[(credit, c)], 1, f"{amount}", "2024-04-30",
                         *((f"{own}" if foreign else "0", EUR if foreign else None) if v13 else (f"{own}", f"{own}"))])
    out.add("account_partial_reconcile", ["id", "debit_move_id", "credit_move_id", "company_id", "amount",
                                          "max_date", *(("amount_currency", "currency_id") if v13 else (
                                              "debit_amount_currency", "credit_amount_currency"))], partials)
    if v13:
        out.add("account_payment", ["id", "currency_id", "partner_id", "partner_type", "payment_type", "amount",
                                    "state"], [[p[0], *p[2:7], "posted"] for p in payments])
    else:
        out.add("account_payment", ["id", "move_id", "currency_id", "partner_id", "partner_type", "payment_type",
                                    "amount", "is_internal_transfer"], payments)


def _stock(out: Tables, version: int) -> None:
    out.add("stock_location", ["id", "name", "complete_name", "usage", "company_id", "active"],
            [[i, n.rsplit("/", 1)[-1], n, usage, 1 if usage == "internal" else None, "t"]
             for i, n, usage in LOCATIONS])
    out.add("stock_move", ["id", "name", "reference", "date", "product_id", "product_uom_qty", "product_qty",
                           "location_id", "location_dest_id", "state", "company_id"],
            [[i, ref, ref, on, product, f"{qty}", f"{qty}", src, dest, state, 1]
             for i, ref, on, product, qty, src, dest, state, _ in STOCK_MOVES])
    layers = [[i, 1, product, i, f"{qty if dest == 8 else -qty}", f"{value}", ref, on]
              for i, ref, on, product, qty, src, dest, state, value in STOCK_MOVES if value is not None]
    layers.append([layers[-1][0] + 1, 1, WIDGET, None, "0", f"{LANDED_COST[0]}", "Landed cost", LANDED_COST[1]])
    out.add("stock_valuation_layer", ["id", "company_id", "product_id", "stock_move_id", "quantity", "value",
                                      "description", "create_date"], layers)
    moves_ = {m[0]: m for m in STOCK_MOVES}
    out.add("uom_uom", ["id", "name", "category_id", "factor", "rounding", "active"],
            [[i, json.dumps({"en_US": n}) if version >= 16 else n, 1, factor, rounding, "t"]
             for i, n, factor, rounding in UOMS])
    # Odoo 17 also stores what each line moved in its product's unit.
    out.add("stock_move_line", ["id", "move_id", "product_id", "lot_id", "location_id", "location_dest_id",
                                "product_uom_id", "quantity" if version >= 17 else "qty_done",
                                *(["quantity_product_uom"] if version >= 17 else []), "reference", "date", "state",
                                "company_id"],
            [[i, move, moves_[move][3], lot, src, dest, UNITS, f"{qty}", *([f"{qty}"] if version >= 17 else []),
              moves_[move][1], moves_[move][2], moves_[move][7], 1]
             for i, move, lot, qty, src, dest in STOCK_MOVE_LINES])
    out.add("stock_quant", ["id", "product_id", "company_id", "location_id", "lot_id", "quantity",
                            "reserved_quantity"],
            [[n, product, 1, location, lot, f"{qty}", "0"] for n, (location, product, lot, qty) in enumerate(QUANTS, 1)])
    out.add("stock_lot" if version >= 16 else "stock_production_lot", ["id", "name", "product_id", "company_id"],
            [[i, n, WIDGET, 1] for i, n in LOTS])


def store_name(content: bytes) -> str:
    checksum = hashlib.sha1(content).hexdigest()
    return f"{checksum[:2]}/{checksum}"


def _attachments(out: Tables) -> None:
    rows = []
    for i, model, record, field_, n, content, in_db, in_store in ATTACHMENTS:
        checksum = hashlib.sha1(content).hexdigest()
        rows.append([i, record, 1, len(content), n, model, field_, "binary", None,
                     None if in_db else store_name(content), checksum,
                     "image/png" if content.startswith(PNG) else "application/octet-stream",
                     "\\x" + content.hex() if in_db else None])
    out.add("ir_attachment", ["id", "res_id", "company_id", "file_size", "name", "res_model", "res_field", "type",
                              "url", "store_fname", "checksum", "mimetype", "db_datas"], rows)


def _copy_value(value) -> str:
    if value is None:
        return "\\N"
    text = str(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def dump_sql(out: Tables) -> str:
    parts = ["--\n-- PostgreSQL database dump\n--\n\n-- Dumped from database version 16.3\n\n",
             "SET statement_timeout = 0;\nSET client_encoding = 'UTF8';\n\n"]
    for table, (columns, _) in out.rows.items():
        parts.append(f"CREATE TABLE public.{table} (\n" + ",\n".join(f"    {c} text" for c in columns) + "\n);\n\n")
    for table, (columns, rows) in out.rows.items():
        parts.append(f"--\n-- Data for Name: {table}; Type: TABLE DATA; Schema: public; Owner: odoo\n--\n\n")
        parts.append(f"COPY public.{table} ({', '.join(columns)}) FROM stdin;\n")
        parts += ["\t".join(_copy_value(v) for v in row) + "\n" for row in rows]
        parts.append("\\.\n\n\n")
    parts.append("--\n-- PostgreSQL database dump complete\n--\n\n")
    return "".join(parts)


def manifest(version: int) -> dict:
    return {"odoo_dump": "1", "db_name": "harbour", "version": f"{version}.0",
            "version_info": [version, 0, 0, "final", 0, ""], "major_version": f"{version}.0", "pg_version": "16.3",
            "modules": {"base": f"{version}.0.1.3", "account": f"{version}.0.1.2", "stock": f"{version}.0.1.1"}}


def backup(version: int, out: Tables | None = None, manifest_: dict | None = None) -> bytes:
    """The backup zip Odoo's database manager writes for this company at `version`."""
    out = out or tables(version)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("dump.sql", dump_sql(out))
        z.writestr("manifest.json", json.dumps(manifest_ or manifest(version), indent=4))
        # The filestore keeps one copy of each content, named by its checksum.
        stored = {store_name(content): content for *_, content, in_db, in_store in ATTACHMENTS if in_store}
        for name, content in stored.items():
            z.writestr(f"filestore/{name}", content)
    return archive.getvalue()
