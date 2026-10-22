# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read one company's books from an Odoo backup into the shared `Book`.

Every posted journal entry is a ledger journal with its own lines; the customer
invoices, credit notes, vendor bills and refunds are its documents, posted, draft or
cancelled. A payment is linked to each invoice or bill it is reconciled with, and a
credit note or refund to each one it is applied to, by what each reconciliation applied, so the ledger carries
them as Celerp settlements and applied notes. The source's own figures for the
reconciliation are its posted move lines summed to each month end, the open amount of
each customer's and supplier's lines, each document's open amount, the stock on hand
in its internal locations and the value of its stock valuation layers.

Stock is carried per item, lot and internal location: each done move line that took
goods into, out of or between internal locations, with the share of its move's value its
valuation layers record. Files attached to contacts, products and
documents are read from the backup's filestore.
"""

from __future__ import annotations

import json
import mimetypes
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from celerp.importers.adapters.attachment_files import MAX_ATTACHMENT_BYTES
from celerp.importers.adapters.base import ScanError, SourceRevisionError
from celerp.importers.adapters.exports import steps
from celerp.importers.adapters.exports.book import (
    PARTY_SIGN, ZERO, Account, Book, Contact, DocLine, Document, GLJournal, GLLine, Item, LineTax, StockHolding,
    TaxCode,
)
from celerp.importers.adapters.odoo.dump import (
    DAMAGED, DOCUMENT_BYTES, DOCUMENT_LINE_BYTES, HOLDING_BYTES, MONTH_ACCOUNT_BYTES, STOCK_MOVE_BYTES, Backup, Row,
    freed,
)
from celerp.services.money import allocate_pro_rata, round_money
from ui.i18n import t

SOURCE_SYSTEM = "odoo"
SOURCE_NAME = "Odoo"

# Odoo account type (16 and later, and the older type each earlier type record maps to)
# -> (account type, control).
ACCOUNT_TYPES = {
    "asset_receivable": ("asset", "receivable"), "asset_cash": ("asset", "bank"),
    "asset_current": ("asset", None), "asset_non_current": ("asset", None), "asset_prepayments": ("asset", None),
    "asset_fixed": ("asset", None), "liability_payable": ("liability", "payable"),
    "liability_credit_card": ("liability", None), "liability_current": ("liability", None),
    "liability_non_current": ("liability", None), "equity": ("equity", None),
    "equity_unaffected": ("equity", "retained_earnings"), "income": ("revenue", None),
    "income_other": ("revenue", None), "expense": ("expense", None), "expense_depreciation": ("expense", None),
    "expense_direct_cost": ("cogs", None),
}
OFF_BALANCE = "off_balance"
# Odoo 13 to 15 keep the type in a record of their own; each standard one maps to the 16 type.
LEGACY_TYPES = {
    "data_account_type_receivable": "asset_receivable", "data_account_type_payable": "liability_payable",
    "data_account_type_liquidity": "asset_cash", "data_account_type_credit_card": "liability_credit_card",
    "data_account_type_current_assets": "asset_current", "data_account_type_non_current_assets": "asset_non_current",
    "data_account_type_prepayments": "asset_prepayments", "data_account_type_fixed_assets": "asset_fixed",
    "data_account_type_current_liabilities": "liability_current",
    "data_account_type_non_current_liabilities": "liability_non_current", "data_account_type_equity": "equity",
    "data_unaffected_earnings": "equity_unaffected", "data_account_type_other_income": "income_other",
    "data_account_type_revenue": "income", "data_account_type_depreciation": "expense_depreciation",
    "data_account_type_expenses": "expense", "data_account_type_direct_costs": "expense_direct_cost",
    "data_account_off_sheet": OFF_BALANCE,
}
# A type the company made itself, by its internal type, else its group.
LEGACY_KINDS = {"receivable": "asset_receivable", "payable": "liability_payable", "liquidity": "asset_cash"}
GROUP_TYPES = {"asset": "asset_current", "liability": "liability_current", "equity": "equity", "income": "income",
               "expense": "expense", "off_balance": OFF_BALANCE}

# Odoo move type -> (document type, the source's name for it, ledger journal kind).
DOCUMENTS = {
    "out_invoice": ("invoice", "Customer Invoice", "receivable invoice"),
    "out_refund": ("credit_note", "Customer Credit Note", "receivable credit note"),
    "in_invoice": ("bill", "Vendor Bill", "payable invoice"),
    "in_refund": ("debit_note", "Vendor Refund", "payable credit note"),
}
ENTRY_SOURCES = {"entry": "Journal Entry", "out_receipt": "Sales Receipt", "in_receipt": "Purchase Receipt"}
# (partner type, payment type) -> (the source's name, ledger journal kind) of a payment that settles documents.
PAYMENTS = {
    ("customer", "inbound"): ("Customer Payment", "receivable payment"),
    ("supplier", "outbound"): ("Vendor Payment", "payable payment"),
}
# Document type -> the payment kind of a move reconciled only with documents of that type.
SETTLING = {"invoice": PAYMENTS[("customer", "inbound")], "bill": PAYMENTS[("supplier", "outbound")]}
OTHER_PAYMENT = ("Payment", "payment")
INTERNAL_TRANSFER = ("Internal Transfer", "bank transfer")
STATUSES = {"posted": "posted", "draft": "draft", "cancel": "void"}

# Records the backup holds that no migration carries, by coverage label.
DRAFT_ENTRY = "Draft or cancelled journal entry"
OFF_BALANCE_LINE = "Off-balance journal item"
NO_CONTACT = "Draft document without a contact"
LOT = "Lot or serial number"
UNSTOCKED_LOT = "Lot or serial number never stocked"
# Tracking categories of an analytic account with no plan (Odoo 13 to 15) and of analytic tags.
ANALYTIC_ACCOUNT = "Analytic account"
ANALYTIC_TAG = "Analytic tag"

# Odoo models whose files a migration reads.
TARGET_MODELS = frozenset({"account.move", "account.payment", "res.partner", "product.product", "product.template"})
# The field holding a record's full-size picture; Odoo keeps smaller copies of it under other fields.
MAIN_IMAGES = frozenset({"image_1920", "image_variant_1920"})

# The tables read, by what they hold; the lot table is read too, by its name in the backup's
# version (Backup.lot_table), with the stock.
FAMILIES = {
    "company": ("res_company", "res_currency"),
    "chart": ("account_account", "account_account_type", "ir_model_data"),
    "contacts": ("res_partner",),
    "items": ("product_product", "product_template", "uom_uom"),
    "taxes": ("account_tax", "account_tax_repartition_line"),
    "journals": ("account_move", "account_move_line", "account_move_line_account_tax_rel"),
    "payments": ("account_partial_reconcile", "account_payment"),
    "analytics": ("account_analytic_account", "account_analytic_plan", "account_analytic_tag",
                  "account_analytic_tag_account_move_line_rel"),
    "stock": ("stock_location", "stock_move", "stock_move_line", "stock_quant", "stock_valuation_layer"),
    "attachments": ("ir_attachment",),
}
TABLES = tuple(table for tables in FAMILIES.values() for table in tables)
# The columns this reader uses, in any of the versions it reads; the dump's other columns are not kept.
COLUMNS = frozenset({
    "account_analytic_tag_id", "account_id", "account_move_line_id", "account_tax_id", "analytic_account_id",
    "analytic_distribution", "account_type", "active", "amount", "amount_currency",
    "amount_residual", "amount_tax", "amount_total", "amount_type", "checksum", "city", "code",
    "commercial_partner_id", "company_id", "complete_name", "create_date", "credit", "credit_amount_currency", "credit_move_id", "currency_id", "customer_rank",
    "date", "db_datas", "debit", "debit_amount_currency", "debit_move_id", "default_code", "deprecated", "description", "description_sale",
    "discount", "display_type", "document_type", "email", "exclude_from_invoice_tab", "id",
    "income_currency_exchange_account_id", "internal_group", "invoice_date_due", "invoice_tax_id",
    "is_internal_transfer", "is_rounding_line", "list_price", "location_dest_id", "location_id", "lot_id", "mimetype",
    "mobile", "model",
    "move_id", "move_type", "name", "partner_id", "partner_type", "payment_id", "payment_type", "phone", "plan_id",
    "price_subtotal", "price_total", "price_unit", "product_id", "product_qty", "product_tmpl_id", "product_uom_qty",
    "qty_done", "quantity", "ref",
    "reference", "repartition_type", "res_field", "res_id", "res_model", "sequence", "state", "stock_move_id",
    "store_fname", "street", "street2", "supplier_rank", "tax_id", "tax_line_id", "type", "usage", "user_type_id",
    "url", "value", "vat", "zip", "factor", "rounding", "uom_id", "product_uom_id", "quantity_product_uom",
})
# Of those, the ones holding an id, a number or a date. Backup.tables refuses a dump with any
# other value in one, whether or not the import goes on to use that value.
IDS = frozenset(c for c in COLUMNS if c == "id" or c.endswith("_id")) | {"res_id"}
NUMBERS = frozenset({
    "amount", "amount_currency", "amount_residual", "amount_tax", "amount_total", "credit", "credit_amount_currency",
    "customer_rank", "debit", "debit_amount_currency", "discount", "factor", "list_price", "price_subtotal",
    "price_total", "price_unit", "product_qty", "product_uom_qty", "qty_done", "quantity", "quantity_product_uom",
    "rounding", "sequence", "supplier_rank", "value",
})
DATES = frozenset({"create_date", "date", "invoice_date_due"})
COLUMN_KINDS = {**dict.fromkeys(IDS, "id"), **dict.fromkeys(NUMBERS, "number"), **dict.fromkeys(DATES, "date")}


def required_tables(modules: frozenset[str], version: int) -> set[str]:
    """The tables read that each installed module makes at `version`, empty or not: one
    missing from the dump means the backup was cut or edited. A module not installed makes
    none, so its absence is no damage."""
    old = version <= 15
    made = {
        "base": ("res_company", "res_currency", "res_partner", "ir_attachment", *(("ir_model_data",) if old else ())),
        "uom": ("uom_uom",),
        "product": ("product_product", "product_template"),
        "analytic": ("account_analytic_account",
                     *(("account_analytic_tag",) if old else ("account_analytic_plan",))),
        "account": ("account_account", "account_tax", "account_tax_repartition_line", "account_move",
                    "account_move_line", "account_move_line_account_tax_rel", "account_partial_reconcile",
                    "account_payment",
                    *(("account_account_type", "account_analytic_tag_account_move_line_rel") if old else ())),
        "stock": ("stock_location", "stock_move", "stock_move_line", "stock_quant",
                  "stock_production_lot" if old else "stock_lot"),
        "stock_account": ("stock_valuation_layer",),
    }
    return {table for module in modules & made.keys() for table in made[module]}


# The rows kept of a table the reader needs only a few rows of: Odoo names every record in
# ir_model_data, and only the names of the account types are read.
KEPT_ROWS = {"ir_model_data": lambda r: r["model"] == "account.account.type"}


@dataclass(frozen=True)
class StockMove:
    key: str
    source_type: str
    ref: str
    date: date
    item: str                                   # the item or holding whose stock moved
    quantity: Decimal                           # into the company's internal locations, signed
    value: Decimal | None                       # base, signed; None when the source values no stock
    record: str                                 # the Odoo move line or valuation layer it comes from


@dataclass(frozen=True)
class AttachmentRef:
    key: str
    name: str
    store_fname: str | None
    stored: bytes | None                        # content kept in the database instead of the filestore
    checksum: str | None                        # SHA-1 of the content, as Odoo records it
    target_type: str | None                     # source type of its record; None when Celerp holds no such record
    target: str | None
    item_image: bool = False                    # the item's own picture rather than a file attached to it
    too_long: bool = False                      # its row keeps more than Celerp reads from one record


@dataclass
class OdooBook:
    book: Book
    version: int
    moves: list[StockMove] = field(default_factory=list)
    attachments: dict[str, AttachmentRef] = field(default_factory=dict)
    system_files: int = 0                       # Odoo's interface files and the smaller copies of each picture
    locations: int = 0
    lots: int = 0                               # lots and serial numbers with stock carried
    companies: tuple[str, ...] = ()             # every company with books, when there are several
    other_entries: int = 0                      # entries of the companies not imported


def _dec(value: str | None) -> Decimal:
    return Decimal(value) if value not in (None, "") else ZERO


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _shares(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """`total` split in proportion to `weights`, the last part taking what is left."""
    whole = sum(weights, ZERO)
    if whole == total:
        return list(weights)
    shares = [total * w / whole for w in weights[:-1]]
    return [*shares, total - sum(shares, ZERO)]


def _true(value: str | None) -> bool:
    return value == "t"


def _internal(payment: Row) -> bool:
    return _true(payment.get("is_internal_transfer")) or payment.get("payment_type") == "transfer"


def _ours(row: Row, company: str) -> bool:
    return row.get("company_id") in (None, company)


class _Reader:
    def __init__(self, backup: Backup, company: str | None) -> None:
        self.backup = backup
        self.text = backup.text
        # A file kept in the database is hex after \x, two characters a byte: one larger than the
        # import takes is read as absent, and an attachment row too long to read is left out alone.
        self.rows = backup.tables((*TABLES, backup.lot_table), COLUMNS, KEPT_ROWS,
                                  required_tables(backup.modules, backup.version),
                                  capped={"db_datas": 3 + 2 * MAX_ATTACHMENT_BYTES}, may_leave_out=("ir_attachment",),
                                  kinds=COLUMN_KINDS)
        self.company, companies = self._company(company)
        currencies = {r["id"]: r["name"] for r in self.rows.get("res_currency", [])}
        self.currency = lambda i: currencies.get(i or "", "")
        # Currency -> its smallest unit. Odoo writes floats, so a stored amount's scale is not the currency's.
        self.book = Book(SOURCE_SYSTEM, company_name=self.company["name"] or "",
                         base=self.currency(self.company["currency_id"]))
        self.out = OdooBook(self.book, backup.version, companies=companies, other_entries=sum(
            1 for r in self.rows.get("account_move", []) if r["company_id"] != self.company["id"]))
        self.accounts: dict[str, str] = {}          # account id -> code
        self.partners: dict[str, str] = {}          # partner id -> contact key of its commercial partner
        self.products: dict[str, str] = {}          # product id -> item code
        self.uoms: dict[str, Row] = {}              # unit of measure id -> its row
        self.units: dict[str, Row | None] = {}      # product id -> its unit of measure's row
        self.taxes: dict[str, str] = {}             # tax id -> tax code name
        self.rates: dict[str, Decimal] = {}         # tax id -> rate percent, below zero when withheld
        self.documents: dict[str, Document] = {}    # move id -> document
        self.analytic: dict[str, dict[str, str]] = {}   # move line id -> tracking category -> option

    def table(self, name: str) -> list[Row]:
        return [r for r in self.rows.get(name, []) if _ours(r, self.company["id"])]

    def _unique(self, names: dict[str, str]) -> dict[str, str]:
        """A name per id that no other id shares: the oldest record keeps a shared name and each
        later one is told apart by its id."""
        first: dict[str, str] = {}
        for i in sorted(names, key=int):
            first.setdefault(names[i], i)
        return {i: (name if first[name] == i else self.backup.held_text(f"{name} [{i}]")) for i, name in names.items()}

    def _name(self, value: str | None, fallback: str) -> str:
        """A name without its outer spaces, else `fallback`."""
        name = (value or "").strip()
        return (name if name is value else self.backup.held_text(name)) or fallback

    def _company(self, chosen: str | None) -> tuple[Row, tuple[str, ...]]:
        """The company whose books are read: `chosen` by name, else the oldest with books; and
        the name of every company with books when there are several to choose from."""
        if "account_account" not in self.rows:
            raise ScanError(t("migration.err_odoo_no_accounting"))
        companies = {r["id"]: r for r in self.rows.get("res_company", [])}
        if not companies:
            raise ScanError(t("migration.err_odoo_no_accounting"))
        booked = sorted({r["company_id"] for r in self.rows.get("account_move", [])} & companies.keys(), key=int)
        names = self._unique({i: self._name(companies[i]["name"], f"Company {i}") for i in booked})
        options = tuple(names[i] for i in booked) if len(booked) > 1 else ()
        if chosen is None:
            return companies[booked[0] if booked else min(companies, key=int)], options
        found = next((i for i in booked if names[i] == chosen), None)
        if found is None:
            raise ScanError(t("migration.err_odoo_company_missing", company=chosen))
        return companies[found], options

    # ── Masters ──────────────────────────────────────────────────────────────

    def _account_kinds(self) -> dict[str, str]:
        """Account id -> its Odoo 16 account type."""
        if self.backup.version >= 16:
            return {r["id"]: r["account_type"] or GROUP_TYPES.get(r.get("internal_group") or "", "")
                    for r in self.table("account_account")}
        standard = {r["res_id"]: LEGACY_TYPES[r["name"]] for r in self.rows.get("ir_model_data", [])
                    if r["name"] in LEGACY_TYPES}
        types = {r["id"]: standard.get(r["id"]) or LEGACY_KINDS.get(r["type"] or "")
                 or GROUP_TYPES.get(r.get("internal_group") or "", "")
                 for r in self.rows.get("account_account_type", [])}
        return {r["id"]: types.get(r["user_type_id"] or "", "") for r in self.table("account_account")}

    def read_chart(self) -> None:
        kinds = self._account_kinds()
        tax_accounts = {r["account_id"] for r in self.rows.get("account_tax_repartition_line", [])
                        if r["repartition_type"] == "tax" and r["account_id"]}
        fx = self.company.get("income_currency_exchange_account_id")
        for row in sorted(self.table("account_account"), key=lambda r: r["code"]):
            kind = kinds[row["id"]]
            if kind == OFF_BALANCE:
                continue
            if kind not in ACCOUNT_TYPES:
                raise ScanError(t("migration.err_export_account_type", account=f"{row['code']} {self.text(row['name'])}",
                                  type=kind))
            account_type, control = ACCOUNT_TYPES[kind]
            if control is None and row["id"] in tax_accounts and account_type in ("asset", "liability"):
                control = "tax"
            if row["id"] == fx:
                control = "exchange_difference"
            currency = self.currency(row["currency_id"]) if control == "bank" else None
            account = Account(row["code"], self.text(row["name"]), account_type, control,
                              currency if self.book.is_foreign(currency) else None, not _true(row["deprecated"]))
            # Its label is built again for each figure checked against it.
            self.backup.held_text(account.label)
            self.book.accounts[row["code"]] = account
            self.accounts[row["id"]] = row["code"]

    def read_contacts(self, referenced: set[str]) -> None:
        """Commercial partners: customers, suppliers and any a carried record names. A contact
        person or address of a company is that company."""
        rows = {r["id"]: r for r in self.table("res_partner")}
        commercial = {i: (r.get("commercial_partner_id") or i) for i, r in rows.items()}
        own = {self.company["partner_id"]}
        named = {commercial[i] for i in referenced if i in commercial}
        carried = sorted((i for i, r in rows.items() if commercial[i] == i and (
            i in named or (i not in own and (_dec(r.get("customer_rank")) > 0 or _dec(r.get("supplier_rank")) > 0)))),
            key=int)
        keys = self._unique({i: self._name(rows[i]["name"], f"Partner {i}") for i in carried})
        for i in carried:
            r = rows[i]
            address = self.backup.held_text(", ".join(p for p in (r.get("street"), r.get("street2"), " ".join(
                p for p in (r.get("zip"), r.get("city")) if p)) if p))
            contact = Contact(keys[i], email=r.get("email") or None, phone=r.get("phone") or r.get("mobile") or None,
                              address=address or None, tax_id=r.get("vat") or None)
            if _dec(r.get("customer_rank")) > 0:
                contact.roles.add("customer")
            if _dec(r.get("supplier_rank")) > 0:
                contact.roles.add("supplier")
            self.book.contacts[keys[i]] = contact
        self.partners = {i: keys[c] for i, c in commercial.items() if c in keys}

    def read_items(self) -> None:
        templates = {r["id"]: r for r in self.table("product_template")}
        products = sorted((r for r in self.rows.get("product_product", []) if r["product_tmpl_id"] in templates),
                          key=lambda r: int(r["id"]))
        # A template's variants share its name and description, decoded once.
        names = {i: self.text(r["name"]) for i, r in templates.items()}
        descriptions = {i: self.text(r.get("description_sale")) or None for i, r in templates.items()}
        codes = self._unique({r["id"]: r["default_code"] or names[r["product_tmpl_id"]] for r in products})
        for r in products:
            template = templates[r["product_tmpl_id"]]
            price = template.get("list_price")
            self.book.items[codes[r["id"]]] = Item(
                codes[r["id"]], names[r["product_tmpl_id"]],
                descriptions[r["product_tmpl_id"]], _dec(price) if price else None,
                _true(r["active"]) and _true(template["active"]))
        self.products = {r["id"]: codes[r["id"]] for r in products}
        self.uoms = {r["id"]: r for r in self.rows.get("uom_uom", [])}
        # Odoo requires a unit's ratio and rounding above zero; the ratio divides a quantity.
        if any(_dec(r.get(c)) <= 0 for r in self.uoms.values() for c in ("factor", "rounding")):
            raise ValueError("unit of measure")
        self.units = {r["id"]: self.uoms.get(templates[r["product_tmpl_id"]].get("uom_id")) for r in products}

    def read_taxes(self) -> None:
        rows = sorted(self.table("account_tax"), key=lambda r: int(r["id"]))
        names = self._unique({r["id"]: self.text(r["name"]) for r in rows})
        accounts: dict[str, str] = {}
        for r in sorted(self.rows.get("account_tax_repartition_line", []), key=lambda r: int(r["id"])):
            tax = r.get("tax_id") if r.get("document_type", "invoice") == "invoice" else None
            tax = tax or r.get("invoice_tax_id")
            if r["repartition_type"] == "tax" and tax and r["account_id"] in self.accounts:
                accounts.setdefault(tax, self.accounts[r["account_id"]])
        for r in rows:
            rate = _dec(r["amount"]) if r["amount_type"] in ("percent", "division") else ZERO
            self.book.tax_codes[names[r["id"]]] = TaxCode(names[r["id"]], rate, accounts.get(r["id"]))
            self.taxes[r["id"]], self.rates[r["id"]] = names[r["id"]], rate

    def read_analytics(self) -> None:
        """Each move line's analytic accounts as tracking, by plan. From Odoo 16 a line spreads
        across accounts by percent (a key may join one account of each plan); before, it names
        one analytic account and any tags."""
        plans = {r["id"]: self.text(r["name"]) for r in self.rows.get("account_analytic_plan", [])}
        # Analytic account id -> its name and plan, decoded once for every line naming it.
        accounts = {r["id"]: (self.text(r["name"]), plans.get(r.get("plan_id") or "", ANALYTIC_ACCOUNT))
                    for r in self.rows.get("account_analytic_account", [])}
        tags = {r["id"]: self.text(r["name"]) for r in self.rows.get("account_analytic_tag", [])}
        tagged: dict[str, list[str]] = defaultdict(list)
        for r in self.rows.get("account_analytic_tag_account_move_line_rel", []):
            if r["account_analytic_tag_id"] in tags:
                tagged[r["account_move_line_id"]].append(tags[r["account_analytic_tag_id"]])
        # Lines analysed alike share one tracking, so what it holds is built once, not per line.
        spreads: dict[tuple[str | None, str | None, tuple[str, ...]], dict[str, str]] = {}
        for r in self.rows.get("account_move_line", []):
            spread = (r.get("analytic_distribution"), r.get("analytic_account_id"), tuple(tagged.get(r["id"], ())))
            if spread not in spreads:
                spreads[spread] = self._spread(*spread, accounts)
            if found := spreads[spread]:
                self.analytic[r["id"]] = found

    def _spread(self, distribution: str | None, account: str | None, tags: tuple[str, ...],
                accounts: dict[str, tuple[str, str]]) -> dict[str, str]:
        """Tracking category -> option of a line's analytic distribution, account and tags."""
        options: dict[str, list[str]] = defaultdict(list)
        for keys, percent in json.loads(distribution or "{}").items():
            share = format(Decimal(str(percent)).normalize(), "f")
            for i in (k for k in keys.split(",") if k in accounts):
                name, plan = accounts[i]
                options[plan].append(name if Decimal(share) == 100 else f"{name} {share}%")
        if account in accounts:
            options[ANALYTIC_ACCOUNT].append(accounts[account][0])
        options[ANALYTIC_TAG] += tags
        return {k: self.backup.held_text(", ".join(v)) for k, v in options.items() if v}

    def _tracking(self, row: Row) -> dict[str, str]:
        """A carried line's tracking; its categories are counted in the coverage report."""
        tracking = self.analytic.get(row["id"], {})
        self.book.tracking |= set(tracking)
        return dict(tracking)

    # ── Journals and documents ───────────────────────────────────────────────

    def _move_type(self, move: Row) -> str:
        return move.get("move_type") or move.get("type") or "entry"

    def read_ledger(self) -> None:
        book = self.book
        moves = {r["id"]: r for r in self.table("account_move")}
        lines: dict[str, list[Row]] = defaultdict(list)
        for r in self.rows.get("account_move_line", []):
            if r["move_id"] in moves:
                lines[r["move_id"]].append(r)
        self.lines = {r["id"]: r for group in lines.values() for r in group}
        self.posted = {i for i, m in moves.items() if m["state"] == "posted"}
        named = {r["partner_id"] for r in self.lines.values()}
        named |= {m.get("commercial_partner_id") or m.get("partner_id") for m in moves.values()}
        self.read_contacts(named - {None})
        tax_rel: dict[str, list[str]] = defaultdict(list)
        for r in self.rows.get("account_move_line_account_tax_rel", []):
            if r["account_tax_id"] in self.taxes:
                tax_rel[r["account_move_line_id"]].append(r["account_tax_id"])

        ordered = sorted(moves.values(), key=lambda m: (m["date"], int(m["id"])))
        for move in ordered:
            if self._move_type(move) in DOCUMENTS:
                self._document(move, lines[move["id"]], tax_rel)
        settles, settled_base = self._allocations()
        payments = {r["id"]: r for r in self.rows.get("account_payment", [])}
        self._mark_banks(ordered, lines, payments)
        for move in ordered:
            if move["state"] != "posted":
                if self._move_type(move) not in DOCUMENTS:
                    book.skipped[DRAFT_ENTRY] += 1
                continue
            journal = self._journal(move, lines[move["id"]], payments, settles.get(move["id"], {}),
                                    settled_base.get(move["id"]))
            if journal is not None:
                book.journals.append(journal)

    def _mark_banks(self, moves: list[Row], lines: dict[str, list[Row]], payments: dict[str, Row]) -> None:
        """An asset account a posted payment moves money through is a bank account: Odoo 14 and
        later post a payment to an outstanding receipts or payments account until the bank
        statement clears it."""
        for move in moves:
            payment = self._payment(move, lines[move["id"]], payments)
            if move["state"] != "posted" or payment is None or _internal(payment):
                continue
            for r in lines[move["id"]]:
                account = self.book.accounts.get(self.accounts.get(r["account_id"], ""))
                if account is not None and account.account_type == "asset" and account.control is None:
                    account.control = "bank"

    def _payment(self, move: Row, lines: list[Row], payments: dict[str, Row]) -> Row | None:
        """The payment a move posts: named on the move from Odoo 14, on its lines before."""
        found = move.get("payment_id") or next((r.get("payment_id") for r in lines if r.get("payment_id")), None)
        return payments.get(found or "")

    def _journal(self, move: Row, rows: list[Row], payments: dict[str, Row],
                 settles: dict[str, Decimal], settled_base: Decimal | None) -> GLJournal | None:
        """A posted move's lines. A payment or bank statement line reconciled only with invoices,
        or only with bills, settles them by what each reconciliation applied, in the documents'
        currency and in the base currency."""
        book = self.book
        move_type = self._move_type(move)
        parties = book.controlled("receivable", "payable")
        merged: dict[tuple[str, str | None], GLLine] = {}
        out: list[GLLine] = []
        for r in sorted(rows, key=lambda r: int(r["id"])):
            if r["account_id"] not in self.accounts:
                if r["account_id"]:  # section and note lines have no account
                    book.skipped[OFF_BALANCE_LINE] += 1
                continue
            account = self.accounts[r["account_id"]]
            amount = _dec(r["debit"]) - _dec(r["credit"])
            currency = self.currency(r["currency_id"])
            foreign = _dec(r["amount_currency"]) if book.is_foreign(currency) else None
            contact = self.partners.get(r["partner_id"] or "")
            if account in parties:
                held = merged.get((account, contact))
                if held is not None:
                    held.amount += amount
                    held.foreign = None if held.foreign is None or foreign is None else held.foreign + foreign
                    continue
            line = GLLine(account, amount, foreign, r["name"] or "", self._tracking(r),
                          contact=contact if account in parties else None)
            out.append(line)
            if account in parties:
                merged[(account, contact)] = line
        out = [ln for ln in out if ln.amount or ln.foreign]
        if not out:
            return None
        payment = self._payment(move, rows, payments)
        allocated: dict[str, Decimal | None] = {}
        allocated_base = None
        if move_type in DOCUMENTS:
            _, source, kind = DOCUMENTS[move_type]
            doc = self.documents[move["id"]]
            invoice, contact, document = doc.number, doc.contact, doc.key
        else:
            source, kind = ENTRY_SOURCES.get(move_type, ENTRY_SOURCES["entry"]), "journal"
            if payment is not None:
                source, kind = INTERNAL_TRANSFER if _internal(payment) else PAYMENTS.get(
                    (payment["partner_type"], payment["payment_type"]), OTHER_PAYMENT)
            invoice, document = "", None
            named = {ln.contact for ln in out if ln.contact}
            contact = next(iter(named)) if len(named) == 1 else self.partners.get(
                move.get("commercial_partner_id") or move.get("partner_id") or "", "")
            types = {book.documents[k].doc_type for k in settles}
            banked = payment is not None or any(book.accounts[ln.account].control == "bank" for ln in out)
            if banked and len(types) == 1 and next(iter(types)) in SETTLING and kind != INTERNAL_TRANSFER[1]:
                source, kind = SETTLING[next(iter(types))]
                allocated, contact = dict(settles), book.documents[next(iter(settles))].contact
                allocated_base = settled_base
        return GLJournal(f"J:{move['id']}", move["name"], _day(move["date"]), source, kind,
                         move.get("ref") or move["name"], invoice, contact, self.currency(move["currency_id"]), out,
                         allocated, allocated_base, document)

    def _document(self, move: Row, rows: list[Row], tax_rel: dict[str, list[str]]) -> None:
        book = self.book
        doc_type, source, _ = DOCUMENTS[self._move_type(move)]
        status = STATUSES.get(move["state"], "draft")
        contact = self.partners.get(move.get("commercial_partner_id") or move.get("partner_id") or "")
        if contact is None:
            book.skipped[NO_CONTACT] += 1
            return
        currency = self.currency(move["currency_id"])
        sign = PARTY_SIGN[doc_type]

        def amount(r: Row) -> Decimal:
            return -sign * (_dec(r["amount_currency"]) if book.is_foreign(currency)
                            else _dec(r["debit"]) - _dec(r["credit"]))

        carried = [(r, kind) for r in sorted(rows, key=lambda r: (int(r.get("sequence") or 0), int(r["id"])))
                   if (kind := self._line_kind(r)) is not None and r["account_id"] in self.accounts]
        self.backup.hold(DOCUMENT_BYTES + DOCUMENT_LINE_BYTES * len(carried))
        lines: list[DocLine] = []
        for r, kind in carried:
            net = amount(r)
            tax = _dec(r["price_total"]) - _dec(r["price_subtotal"]) if kind == "product" else ZERO
            discount = _dec(r.get("discount"))
            lines.append(DocLine(self.products.get(r["product_id"] or ""), r["name"] or "", _dec(r["quantity"]),
                                 _dec(r["price_unit"]), discount or None, net,
                                 self._line_taxes(tax_rel.get(r["id"], []), net, tax, currency),
                                 self.accounts[r["account_id"]],
                                 self._tracking(r)))
        # Each tax totals what Odoo booked on its tax lines: Odoo can round a tax on the whole
        # document, and that difference lands on the last line carrying the tax.
        booked: dict[str | None, Decimal] = defaultdict(lambda: ZERO)
        for r in rows:
            if r.get("tax_line_id") and r["account_id"] in self.accounts:
                booked[self.taxes.get(r["tax_line_id"])] += amount(r)
        carrying: dict[str | None, list[LineTax]] = defaultdict(list)
        for ln in lines:
            for x in ln.taxes:
                carrying[x.code].append(x)
        last = ([ln for ln in lines if ln.taxes] or lines or [None])[-1]
        for code, target in booked.items():
            if difference := target - sum((x.amount for x in carrying[code]), ZERO):
                if carrying[code]:
                    carrying[code][-1].amount += difference
                elif last is not None:
                    last.taxes.append(LineTax(code, difference))
        for x in (x for ln in lines for x in ln.taxes):
            if book.tax_account_for(x.code) is None:
                raise ScanError(t("migration.err_export_tax_account", tax=x.code or ""))
        total, tax_total = _dec(move["amount_total"]), _dec(move["amount_tax"])
        withheld: dict[str, Decimal] = defaultdict(lambda: ZERO)
        if doc_type in ("invoice", "credit_note"):
            # A sale holds no tax below zero: tax the customer withholds is part of the total,
            # settled from the withholding account.
            for ln in lines:
                kept = []
                for x in ln.taxes:
                    if x.code is None or book.tax_codes[x.code].rate >= 0:
                        kept.append(x)
                    else:
                        withheld[book.tax_account_for(x.code)] -= x.amount
                ln.taxes = kept
            total += sum(withheld.values(), ZERO)
            tax_total += sum(withheld.values(), ZERO)
        number = move["name"] if move["name"] and move["name"] != "/" else f"Draft {move['id']}"
        if sum((ln.net + ln.tax for ln in lines), ZERO) != total:
            raise ScanError(t("migration.err_odoo_doc_total", number=number))
        side = "S" if doc_type in ("invoice", "credit_note") else "P"
        key = f"{side}:{number}"
        for candidate in (key, f"{key}|{contact}", f"{key}|{contact}|{move['id']}"):
            if candidate not in book.documents:
                key = self.backup.held_text(candidate)
                break
        due = _dec(move["amount_residual"])
        book.documents[key] = Document(
            key=key, number=number, doc_type=doc_type, source_type=source, contact=contact, reference="",
            date=_day(move["date"]), due=_day(move.get("invoice_date_due")),
            currency=currency, status=status, lines=lines, total=total, tax_total=tax_total, paid=total - due,
            amount_due=due, withheld=dict(withheld))
        self.documents[move["id"]] = book.documents[key]

    def _line_taxes(self, taxes: list[str], net: Decimal, tax: Decimal, currency: str) -> list[LineTax]:
        """A line's tax split across its taxes by rate, each rounded half-up to the currency's
        smallest unit as Odoo rounds it, the rounding remainder on the last."""
        if not taxes:
            return [LineTax(None, tax)] if tax else []
        rates = [self.rates[i] for i in taxes]
        out = [LineTax(self.taxes[i], round_money(net * rate / 100, currency) if any(rates) else ZERO)
               for i, rate in zip(taxes, rates)]
        out[-1].amount += tax - sum((x.amount for x in out), ZERO)
        return out

    def _line_kind(self, row: Row) -> str | None:
        """A document line Celerp keeps: a product line or a cash rounding line. Tax, payment
        term and stock valuation lines are booked by the document itself."""
        display = row.get("display_type")
        if self.backup.version >= 16:
            return display if display in ("product", "rounding") else None
        if display:
            return None
        if _true(row.get("is_rounding_line")):
            return "rounding"
        return None if _true(row.get("exclude_from_invoice_tab")) else "product"

    def _allocations(self) -> tuple[dict[str, dict[str, Decimal]], dict[str, Decimal]]:
        """Move id of a posted entry that is not a document -> document key -> what each
        reconciliation with that document applied, in the document's currency; and move id ->
        what those reconciliations applied in the base currency, which Odoo states on each. A
        credit note or refund reconciled with an invoice or bill applies to it what the
        reconciliation did."""
        out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
        base: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in self.rows.get("account_partial_reconcile", []):
            sides = {side: self.lines.get(r[f"{side}_move_id"]) for side in ("debit", "credit")}
            if None in sides.values():
                continue
            for side, other in (("debit", "credit"), ("credit", "debit")):
                doc, move = self.documents.get(sides[side]["move_id"]), sides[other]["move_id"]
                if doc is None:
                    continue
                if (note := self.documents.get(move)) is not None:
                    if note.doc_type in ("credit_note", "debit_note"):
                        note.applied[doc.key] = note.applied.get(doc.key, ZERO) + self._applied(
                            r, other, sides[other], note)
                elif move in self.posted:
                    out[move][doc.key] += self._applied(r, side, sides[side], doc)
                    base[move] += _dec(r["amount"])
        return {move: dict(docs) for move, docs in out.items()}, dict(base)

    def _applied(self, partial: Row, side: str, line: Row, doc: Document) -> Decimal:
        """A reconciliation's amount in the currency of the document on its `side`: Odoo 14 and
        later state it per side, Odoo 13 once in the currency both lines share."""
        amount = _dec(partial["amount"])
        if not self.book.is_foreign(doc.currency):
            return amount
        if partial.get(f"{side}_amount_currency") is not None:
            return abs(_dec(partial[f"{side}_amount_currency"]))
        if partial.get("currency_id") is not None and partial["currency_id"] == line["currency_id"]:
            return abs(_dec(partial["amount_currency"]))
        balance = _dec(line["debit"]) - _dec(line["credit"])
        return round_money(amount * abs(_dec(line["amount_currency"]) / balance), doc.currency)

    # ── The source's own figures ─────────────────────────────────────────────

    def roll_trial_balances(self) -> None:
        """Balances from the first record at each month end through the last ledger date."""
        book = self.book
        if not book.journals:
            return
        running: dict[str, Decimal] = defaultdict(lambda: ZERO)
        months: dict[date, list[GLJournal]] = defaultdict(list)
        for j in book.journals:
            months[steps.month_end(j.date)].append(j)
        on = steps.month_end(book.journals[0].date)
        last = steps.month_end(book.journals[-1].date)
        while on <= last:
            self.backup.hold(MONTH_ACCOUNT_BYTES * len(book.accounts))
            # Each month end's balances are checked under every account's code and label again.
            for code, account in book.accounts.items():
                self.backup.held_text(code)
                self.backup.held_text(account.label)
            for j in months.get(on, []):
                for ln in j.lines:
                    running[ln.account] += ln.amount
            book.trial_balances[on] = dict(running)
            on = steps.month_end(date.fromordinal(on.toordinal() + 1))

    def read_open_balances(self) -> None:
        """Each contact's open receivable and payable as the source's open amounts state them."""
        book = self.book
        on = book.journals[-1].date if book.journals else date.min
        for side in ("receivable", "payable"):
            codes = set(book.control(side))
            owed: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for r in self.lines.values():
                if r["move_id"] in self.posted and self.accounts.get(r["account_id"]) in codes \
                        and r["partner_id"] in self.partners:
                    owed[self.partners[r["partner_id"]]] += _dec(r["amount_residual"])
            book.aged[side] = (on, dict(owed))

    # ── Stock ────────────────────────────────────────────────────────────────

    def read_stock(self) -> None:
        """Stock per item, lot and internal location, in the order it moved. Each done move line
        carries the quantity it moved, in the product's unit, and its share of its move's value; a line between
        two internal locations takes along its share of what the stock it left was worth, and a
        valuation with no move falls on the item's stock held at the time, by quantity."""
        book, base = self.book, self.book.base
        names = self._unique({r["id"]: r["complete_name"] or r["name"] or "" for r in self.table("stock_location")
                         if r["usage"] == "internal"})
        internal = sorted(names, key=int)
        self.out.locations = len(internal)
        book.locations = [names[i] for i in internal]
        lots = {r["id"]: r["name"] or r["id"] for r in self.table(self.backup.lot_table)}
        stocked: set[str] = set()

        def bucket(product: str, location: str, lot: str | None) -> str:
            """The item, or the holding, that keeps a product's stock of one lot at one location."""
            code = self.products[product]
            lot = lot if lot in lots else None
            if location == internal[0] and lot is None:
                return code
            key = code + (f"#{lots[lot]}" if lot else "") + ("" if location == internal[0] else f"@{names[location]}")
            if key not in book.holdings:
                # Its inventory count is labelled with the item's name, the lot and the location.
                self.backup.hold(HOLDING_BYTES)
                self.backup.held_text(key)
                self.backup.held_text(book.items[code].name)
                book.holdings[key] = StockHolding(key, code, names[location], lots[lot] if lot else None)
            if lot:
                stocked.add(lot)
            return book.holdings[key].key

        valued = "stock_valuation_layer" in self.rows
        layers = [r for r in self.table("stock_valuation_layer") if r["product_id"] in self.products]
        by_move: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in layers:
            if r.get("stock_move_id"):
                by_move[r["stock_move_id"]] += _dec(r["value"])
        done = "quantity" if self.out.version >= 17 else "qty_done"
        lines: dict[str, list[Row]] = defaultdict(list)
        for r in self.table("stock_move_line"):
            lines[r["move_id"]].append(r)
        events = [(r["date"] or "", 0, int(r["id"]), r) for r in self.table("stock_move")
                  if r["state"] == "done" and r["product_id"] in self.products]
        events += [(r["create_date"] or "", 1, int(r["id"]), r) for r in layers
                   if not r.get("stock_move_id") and _dec(r["value"])]
        held: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])    # key -> [quantity, value]
        stocked_under: dict[str, set[str]] = defaultdict(set)                # item -> its keys holding any
        moves: list[StockMove] = []

        def add(record: str, key: str, source_type: str, ref: str, on: date, target: str, quantity: Decimal,
                value: Decimal | None) -> None:
            self.backup.hold(STOCK_MOVE_BYTES)
            held[target][0] += quantity
            held[target][1] += value or ZERO
            if held[target][0] > 0:
                stocked_under[book.stock_item(target)].add(target)
            else:
                stocked_under[book.stock_item(target)].discard(target)
            moves.append(StockMove(key, source_type, ref, on, target, quantity, value, record))

        for when, is_layer, _, r in sorted(events, key=lambda e: e[:3]):
            code, on = self.products[r["product_id"]], _day(when)
            if is_layer:
                record = f"valuation:{r['id']}"
                holders = sorted(stocked_under[code])
                # A set keeps the room of every key it once held; its copy iterates in its own size.
                stocked_under[code] = set(holders)
                shares = allocate_pro_rata(_dec(r["value"]), [held[k][0] for k in holders], base) if holders \
                    else [_dec(r["value"])]
                description = self.text(r.get("description"))
                for target, share in zip(holders or [code], shares):
                    add(record, self.backup.held_text(f"{record}:{target}") if holders else record, "Stock valuation",
                        description, on, target, ZERO, share)
                continue
            # A move Odoo recorded without lines moves as one line of its own.
            ordered, in_units = _dec(r.get("product_uom_qty")), _dec(r["product_qty"])
            own = sorted(lines.get(r["id"], []), key=lambda ln: int(ln["id"]))
            parts = [(f"stock:{ln['id']}", ln.get("lot_id"), ln["location_id"], ln["location_dest_id"]) for ln in own] \
                or [(f"stock:move:{r['id']}", None, r["location_id"], r["location_dest_id"])]
            # What moved is what the lines record, which can differ from what was ordered: in the
            # product's unit when each line's own unit is known, else in the move's unit; lines that
            # record nothing share the order.
            counted = [self._in_product_units(ln, done, r["product_id"]) for ln in own]
            by_line = bool(own) and None not in counted
            weights = counted if by_line else [_dec(ln.get(done)) for ln in own] or [ordered]
            if not any(weights):
                weights = [Decimal(1)] * len(parts)
                quantities = _shares(in_units, weights)
            elif by_line or ordered in (ZERO, in_units):
                quantities = list(weights)
            else:
                quantities = _shares(sum(weights, ZERO) * in_units / ordered, weights)
            values = allocate_pro_rata(by_move[r["id"]], weights, base) if by_move[r["id"]] else [ZERO] * len(parts)
            ref = r.get("reference") or r.get("name") or ""
            for (record, lot, src, dest), quantity, value in zip(parts, quantities, values):
                if src in names and dest in names:
                    leaves, enters = bucket(r["product_id"], src, lot), bucket(r["product_id"], dest, lot)
                    if leaves == enters:
                        continue
                    on_hand, worth = held[leaves]
                    taken = (round_money(worth * quantity / on_hand, base) if on_hand > 0 else ZERO) if valued else None
                    add(record, f"{record}:to", "Stock move", ref, on, enters, quantity, taken)
                    add(record, f"{record}:from", "Stock move", ref, on, leaves, -quantity, None if taken is None else -taken)
                elif src in names or dest in names:
                    into = dest in names
                    add(record, record, "Stock move", ref, on, bucket(r["product_id"], dest if into else src, lot),
                        quantity if into else -quantity, value if valued else None)
        self.out.moves = sorted(moves, key=lambda m: (m.date, m.quantity < 0, m.key))

        on_hand: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in self.table("stock_quant"):
            if r["location_id"] in names and r["product_id"] in self.products:
                key = bucket(r["product_id"], r["location_id"], r.get("lot_id"))
                on_hand[key] += _dec(r["quantity"])
                if key in book.holdings:
                    on_hand[book.holdings[key].item] += _dec(r["quantity"])
        value: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in layers:
            value[self.products[r["product_id"]]] += _dec(r["value"])
        for code in {*value, *(book.stock_item(k) for k in (*on_hand, *(m.item for m in moves)))}:
            book.stock[code] = (on_hand[code], value[code] if valued else None)
        for key in book.holdings:
            book.stock[key] = (on_hand[key], None)
        self.out.lots = len(stocked)
        if len(lots) > len(stocked):
            book.skipped[UNSTOCKED_LOT] = len(lots) - len(stocked)

    def _in_product_units(self, line: Row, done: str, product: str) -> Decimal | None:
        """What a move line moved, in its product's unit of measure. Odoo 17 stores it; before
        that, the line's own unit converts through each unit's ratio to its category's reference
        unit, rounded to the product's unit as Odoo rounds it. None when a unit is not known."""
        if line.get("quantity_product_uom") is not None:
            return _dec(line["quantity_product_uom"])
        own, unit = self.uoms.get(line.get("product_uom_id")), self.units[product]
        if own is None or unit is None:
            return None
        if own is unit:
            return _dec(line.get(done))
        quantity, step = _dec(line.get(done)) * _dec(unit["factor"]) / _dec(own["factor"]), _dec(unit["rounding"])
        return (quantity / step).quantize(Decimal(1), ROUND_HALF_UP) * step if step else quantity

    # ── Attachments ──────────────────────────────────────────────────────────

    def read_attachments(self) -> None:
        book = self.book
        templates: dict[str, list[str]] = defaultdict(list)
        for r in self.rows.get("product_product", []):
            if r["id"] in self.products:
                templates[r["product_tmpl_id"]].append(self.products[r["id"]])
        for r in self.table("ir_attachment"):
            model, res_id, res_field = r["res_model"] or "", r["res_id"] or "", r["res_field"]
            if (not model or model.startswith("ir.") or (res_field and res_field not in MAIN_IMAGES)
                    or (r["type"] == "url" and (r["url"] or "").startswith("/"))):
                self.out.system_files += 1
                continue
            target_type, target = None, None
            if model == "account.move" and res_id in self.documents:
                target_type, target = self.documents[res_id].source_type, self.documents[res_id].key
            elif model == "res.partner" and res_id in self.partners:
                target_type, target = "Contact", self.partners[res_id]
            elif model == "product.product" and res_id in self.products:
                target_type, target = "Item", self.products[res_id]
            elif model == "product.template" and len(templates.get(res_id, [])) == 1:
                target_type, target = "Item", templates[res_id][0]
            name = r["name"] or ""
            extension = mimetypes.guess_extension(r["mimetype"] or "")
            if res_field and target and extension:
                # Odoo names a picture after its field; name it after its record instead.
                name = self.backup.held_text(target.replace("/", "-").replace("\\", "-") + extension)
            # A file kept in the database is held only when it can be imported: attached to a
            # record Celerp holds and within the size limit (one larger is read as absent).
            stored = r.get("db_datas") if target else None
            held = stored and stored.startswith("\\x")
            key = f"attachment:{r['id']}"
            self.out.attachments[key] = AttachmentRef(
                key, name, r.get("store_fname"), bytes.fromhex(stored[2:]) if held else None,
                r.get("checksum"), target_type, target, bool(res_field) and target_type == "Item")
        for row_id in self.backup.left_out.get("ir_attachment", []):
            key = f"attachment:{row_id}"
            self.out.attachments[key] = AttachmentRef(key, "", None, None, None, None, None, too_long=True)
        book.object_counts["Attachment"] = len(self.out.attachments)


# What converting a damaged field raises: a number that is not one (decimal's ArithmeticError),
# a date or JSON that does not parse, JSON of another shape than Odoo writes, a reference to a
# row that is not there, a NULL where Odoo requires a value, or a name nested past the parser's depth.
CONVERSION_ERRORS = (ArithmeticError, ValueError, TypeError, KeyError, AttributeError, RecursionError)


def read_odoo(backup: Backup, company: str | None = None) -> OdooBook:
    """The books of one company in the backup: `company` by name, else the oldest with books.
    A value that does not convert to what its column holds (CONVERSION_ERRORS) refuses the
    backup as damaged, and nothing read from it is kept."""
    try:
        return _read(backup, company)
    except CONVERSION_ERRORS as exc:
        freed(exc)
    raise SourceRevisionError(t(DAMAGED))


def _read(backup: Backup, company: str | None) -> OdooBook:
    reader = _Reader(backup, company)
    book = reader.book
    reader.read_chart()
    reader.read_items()
    reader.read_taxes()
    reader.read_analytics()
    reader.read_ledger()
    reader.roll_trial_balances()
    reader.read_open_balances()
    steps.roles_from_ledger(book)
    reader.read_stock()
    reader.read_attachments()
    book.object_counts.update({"Account": len(book.accounts), "Contact": len(book.contacts), "Item": len(book.items),
                               "Tax": len(book.tax_codes), "Invoice or bill": len(book.documents),
                               "Journal entry": len(book.journals),
                               "Stock move": len({m.record for m in reader.out.moves})})
    return reader.out
