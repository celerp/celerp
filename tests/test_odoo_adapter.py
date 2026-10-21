# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Odoo backups: one database manager zip read as one company's books.

The fixture is a fictitious company's first four months, written as Odoo 13, 14 and 17
lay their tables out (tests/fixtures/odoo_backup/build.py)."""

from __future__ import annotations

import io
import json
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from functools import cache

import pytest
from sqlalchemy import text

from celerp.importers.adapters.base import MigrationDecisions, ScanError, SourceRevisionError
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.manager_io.support import BASIC, artifact
from fixtures.odoo_backup import build
from migration_support import count, load_run, maker, real_engine  # noqa: F401
from test_migration_e2e import _passing, migrate_set

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = date(2024, 2, 29)
VERSIONS = (13, 14, 17)
D = Decimal


@cache
def _backup(version: int) -> bytes:
    return build.backup(version)


def _file(tmp_path, data: bytes, name: str = "harbour.zip") -> list:
    path = tmp_path / name
    path.write_bytes(data)
    return [artifact(path)]


def _adapter():
    from celerp.importers.adapters.odoo import OdooAdapter

    return OdooAdapter()


def _zip(members: dict[str, bytes | str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def _source_balances(on: date | None = None) -> dict[str, Decimal]:
    """Each account's balance from the fixture's own posted entries, debit positive."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for move in build.moves():
        if move.state == "posted" and (on is None or date.fromisoformat(move.date) <= on):
            for line in move.lines:
                out[build.CODES[line.account]] += line.balance
    return dict(out)


# ── Recognition ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_an_odoo_backup_is_recognised_by_content_alone(tmp_path, version):
    from celerp.importers.adapters.registry import detect_adapter

    files = _file(tmp_path, _backup(version), "download")
    assert detect_adapter(files).key == "odoo"
    scan = _adapter().inspect(files)
    assert (scan.company_name, scan.base_currency, scan.source_schema_version) == (build.COMPANY, "USD", str(version))
    assert scan.currencies == ("EUR", "USD")
    assert (scan.period_start, scan.period_end) == (date(2024, 1, 5), date(2024, 4, 20))
    assert set(scan.features) == {"foreign_currency", "inventory", "tax", "attachments"}


def test_only_a_real_odoo_backup_is_recognised(tmp_path):
    from celerp.importers.adapters.registry import detect_adapter
    from fixtures.xero_export import build as xero

    real = zipfile.ZipFile(io.BytesIO(_backup(17)))
    dump, manifest = real.read("dump.sql"), json.loads(real.read("manifest.json"))
    strangers = {
        "no manifest": _zip({"dump.sql": dump}),
        "no dump": _zip({"manifest.json": json.dumps(manifest)}),
        "not an odoo manifest": _zip({"dump.sql": dump, "manifest.json": json.dumps({"version": "17.0"})}),
        "not a database dump": _zip({"dump.sql": "SELECT 1;\n", "manifest.json": json.dumps(manifest)}),
        "manifest not json": _zip({"dump.sql": dump, "manifest.json": "odoo_dump"}),
        "not a zip": b"-- PostgreSQL database dump\n",
    }
    for label, data in strangers.items():
        result = _adapter().detect(_file(tmp_path, data, f"{label}.zip"))
        assert result.matched is False, label
        assert "not an Odoo backup" in result.reason, label
    # Two files, a Manager business file and an export set are not a backup; each keeps its own adapter.
    assert _adapter().detect([*_file(tmp_path, _backup(17)), *_file(tmp_path, _backup(14), "b.zip")]).matched is False
    assert _adapter().detect([artifact(BASIC)]).matched is False
    assert detect_adapter([artifact(BASIC)]).key == "manager_io"
    xero_zip = _zip({name: data for name, data in xero.files().items()})
    assert _adapter().detect(_file(tmp_path, xero_zip, "xero.zip")).matched is False
    assert detect_adapter(_file(tmp_path, xero_zip, "xero.zip")).key == "xero"


@pytest.mark.parametrize("version", (12, 18))
def test_a_backup_from_an_unsupported_odoo_version_is_refused_with_its_own_message(tmp_path, version):
    from celerp.importers.adapters.registry import detect_adapter

    data = build.backup(17, manifest_=build.manifest(version))
    with pytest.raises(SourceRevisionError, match=f"Odoo {version}.*Odoo 13 to 17"):
        detect_adapter(_file(tmp_path, data))


def test_translated_names_from_odoo_16_are_read_in_english(tmp_path):
    from celerp.importers.adapters.odoo.dump import Backup

    tables = build.tables(17)
    raw = {row[0]: row[6] for row in tables.rows["account_account"][1]}
    assert json.loads(raw[1]) == {"en_US": "Bank"}             # the dump holds JSON
    scan = _adapter().build_manifest(_file(tmp_path, _backup(17)), FULL)
    assert {a.code: a.name for a in scan.bundle.accounts}["101000"] == "Bank"
    assert {i.sku: i.name for i in scan.bundle.items} == {"SRV": "Consulting", "WID-1": "Widget"}
    assert {i.sku: i.description for i in scan.bundle.items}["WID-1"] == "Widget for sale"
    assert sorted(t.name for t in scan.bundle.tax_codes) == ["15%", "15% [2]", "PST 7%", "WHT 3%", "WHT 3% [5]"]

    with Backup(tmp_path / "harbour.zip") as backup:
        assert backup.text('{"es_ES": "Banco"}') == "Banco"     # no English name: the first one stored
        assert backup.text("plain") == "plain"
        backup.version = 15
        assert backup.text('{"en_US": "Bank"}') == '{"en_US": "Bank"}'   # before 16 a name is plain text
        assert backup.lot_table == "stock_production_lot"


def test_a_backup_of_several_companies_or_no_accounting_is_refused(tmp_path):
    tables = build.tables(17)
    columns, rows = tables.rows["account_move"]
    company = columns.index("company_id")
    rows[0][company] = 2
    tables.rows["res_company"][1].append([2, "Second Co", 1, build.USD, None, None, "12"])
    with pytest.raises(ScanError, match="books of 2 companies"):
        _adapter().inspect(_file(tmp_path, build.backup(17, tables)))

    tables = build.tables(17)
    del tables.rows["account_account"]
    with pytest.raises(ScanError, match="Accounting is not installed"):
        _adapter().inspect(_file(tmp_path, build.backup(17, tables)))


def test_a_document_whose_lines_do_not_add_up_is_refused(tmp_path):
    tables = build.tables(17)
    columns, rows = tables.rows["account_move"]
    row = next(r for r in rows if r[columns.index("name")] == "INV/2024/00002")
    row[columns.index("amount_total")] = "231.00"
    with pytest.raises(ScanError, match="INV/2024/00002 do not add up"):
        _adapter().inspect(_file(tmp_path, build.backup(17, tables)))


# ── Masters ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_chart_taxes_contacts_and_products(tmp_path, version):
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    accounts = {a.code: (a.account_type.value, a.control and a.control.value) for a in bundle.accounts}
    assert accounts == {
        "101000": ("asset", "bank"), "101401": ("asset", "bank"), "101402": ("asset", "bank"),
        "121000": ("asset", "receivable"), "211000": ("liability", "payable"), "251000": ("liability", "tax"),
        "131000": ("asset", "tax"), "400000": ("revenue", None), "600000": ("expense", None),
        "500000": ("cogs", None), "110100": ("asset", None), "110200": ("asset", None),
        "999999": ("equity", "retained_earnings"), "300000": ("equity", None),
        "441000": ("revenue", "exchange_difference"), "641000": ("expense", None),
        "115000": ("asset", "tax"), "215000": ("liability", "tax"),
    }
    assert {(t.name, t.rate_percent, t.account_external_id) for t in bundle.tax_codes} == {
        ("15%", D(15), "251000"), ("15% [2]", D(15), "131000"), ("PST 7%", D(7), "251000"),
        ("WHT 3%", D(-3), "115000"), ("WHT 3% [5]", D(-3), "215000")}
    contacts = {c.name: (sorted(c.roles), c.email) for c in bundle.contacts}
    # A contact person is its company; internal users and the company itself are not contacts;
    # a second contact of the same name is told apart by its id.
    assert contacts == {
        "Lakeshore Trading Co": (["customer"], "accounts@lakeshore.example"),
        "Lakeshore Trading Co [11]": (["customer"], "other@lakeshore.example"),
        "Pine & Co": (["customer"], "hello@pine.example"),
        "Northwind Wholesale": (["supplier"], "billing@northwind.example"),
    }
    lakeshore = next(c for c in bundle.contacts if c.name == "Lakeshore Trading Co")
    assert lakeshore.address == "1 Quay Road, PO1 1AA Portsmouth"
    assert next(c for c in bundle.contacts if c.name == "Northwind Wholesale").tax_id == "GB123456789"
    assert {(i.sku, i.name, i.retail_price, i.status) for i in bundle.items} == {
        ("WID-1", "Widget", D(50), "available"), ("SRV", "Consulting", D(100), "available")}


# ── Documents, payments and journals ─────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_invoices_bills_payments_and_journals(tmp_path, version):
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    assert {k: (d.doc_type.value, d.status, d.total, d.amount_outstanding) for k, d in docs.items()} == {
        "P:BILL/2024/01/0001": ("bill", "paid", D("345.00"), D(0)),
        "S:INV/2024/00001": ("invoice", "paid", D("345.00"), D(0)),
        "S:INV/2024/00002": ("invoice", "awaiting_payment", D("230.00"), D("130.00")),
        "S:RINV/2024/00001": ("credit_note", "awaiting_payment", D("57.50"), D("57.50")),
        "P:BILL/2024/03/0001": ("bill", "awaiting_payment", D("230.00"), D("195.50")),
        "P:RBILL/2024/03/0001": ("debit_note", "paid", D("34.50"), D(0)),
        "S:INV/2024/00004": ("invoice", "paid", D(100), D(0)),
        "S:Draft 14": ("invoice", "draft", D("115.00"), D(0)),
        "S:INV/2024/00003": ("invoice", "void", D("57.50"), D(0)),
        "S:INV/2024/00005": ("invoice", "awaiting_payment", D("179.50"), D("179.50")),
        "S:INV/2024/00006": ("invoice", "awaiting_payment", D(200), D(194)),
        "P:BILL/2024/04/0001": ("bill", "awaiting_payment", D(112), D(112)),
    }
    invoice = docs["S:INV/2024/00001"]
    assert invoice.contact_external_id == "Lakeshore Trading Co"
    assert [(ln.item_external_id, ln.quantity, ln.unit_price, ln.total_price, ln.account_external_id)
            for ln in invoice.line_items] == [("WID-1", D(4), D(50), D(200), "400000"),
                                              ("SRV", D(1), D(100), D(100), "400000")]
    assert _line_taxes(invoice) == [[("15%", "251000", D(30))], [("15%", "251000", D(15))]]
    assert docs["S:INV/2024/00002"].contact_external_id == "Pine & Co"       # billed to its contact person
    assert docs["P:RBILL/2024/03/0001"].metadata == {"applies_to": "P:BILL/2024/03/0001"}
    assert docs["S:RINV/2024/00001"].metadata == {}                          # open, applied to nothing
    euro = docs["S:INV/2024/00004"]
    assert (euro.currency, euro.exchange_rate) == ("EUR", D("1.1"))

    settlements = {s.source_external_id: (s.settlement_type.value, s.amount, s.bank_account_external_id,
                                          s.allocations[0].document_external_id) for s in bundle.settlements}
    assert settlements == {
        "J:6": ("receipt", D(345), "101401", "S:INV/2024/00001"),
        "J:8": ("receipt", D(100), "101401", "S:INV/2024/00002"),
        "J:10": ("payment", D(345), "101402", "P:BILL/2024/01/0001"),
        "J:18": ("receipt", D(100), "101401", "S:INV/2024/00004"),
    }
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.12")
    journals = {j.source_external_id: j for j in bundle.journals}
    # Entries that are no document or payment; the draft entry is not carried. The EUR payment
    # posts at its own rate where Celerp relieves the invoice at the invoice's, so the difference
    # rides on it as a fallback, and Odoo's exchange difference entry is carried as itself.
    assert set(journals) == {"J:1", "J:3", "J:5", "J:11", "J:19", "J:20", "J:21", "J:18:journal"}
    assert journals["J:18:journal"].fallback is True
    assert (journals["J:1"].source_type, journals["J:1"].source_ref) == ("Journal Entry", "Capital paid in")


# ── Taxes on document lines ───────────────────────────────────────────────────

def _line_taxes(doc) -> list[list[tuple]]:
    return [[(x.tax_code_external_id, x.tax_account_external_id, x.amount) for x in ln.taxes]
            for ln in doc.line_items]


@pytest.mark.parametrize("version", VERSIONS)
def test_each_tax_on_a_line_posts_to_its_own_account_as_odoo_split_it(tmp_path, version):
    """A line with two taxes carries both; a bill's withholding is a tax of its own on its own
    account; a sale's withholding is tax the customer kept back, applied to the invoice from
    the withholding account, because a sale holds no tax below zero."""
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    two = docs["S:INV/2024/00005"]
    assert _line_taxes(two) == [[("15%", "251000", D(15)), ("PST 7%", "251000", D(7))],
                                [("15%", "251000", D("7.50"))]]
    assert (two.total, two.tax_total, two.amount_outstanding) == (D("179.50"), D("29.50"), D("179.50"))

    withheld = docs["S:INV/2024/00006"]
    assert _line_taxes(withheld) == [[]]
    assert (withheld.total, withheld.tax_total, withheld.amount_outstanding) == (D(200), D(0), D(194))
    assert withheld.metadata == {"withheld": {"115000": "6.00"}}

    bill = docs["P:BILL/2024/04/0001"]
    assert _line_taxes(bill) == [[("15% [2]", "131000", D(15)), ("WHT 3% [5]", "215000", D(-3))]]
    assert (bill.total, bill.tax_total, bill.amount_outstanding) == (D(112), D(12), D(112))
    assert all(x.tax_account_external_id for d in docs.values() for ln in d.line_items for x in ln.taxes)
    assert {(t.name, t.rate_percent, t.account_external_id) for t in bundle.tax_codes} >= {
        ("PST 7%", D(7), "251000"), ("WHT 3%", D(-3), "115000"), ("WHT 3% [5]", D(-3), "215000")}


def test_odoo_rounding_a_tax_on_the_whole_document_lands_on_that_tax(tmp_path):
    """Odoo can round each tax on the document's total: the difference lands on the last
    line carrying that tax, so each tax's total is Odoo's."""
    tables = build.tables(17)
    cols = tables.rows["account_move_line"][0]
    pst = next(r for r in _lines_of(tables, 22) if r[cols.index("display_type")] == "tax"
               and r[cols.index("tax_line_id")] == build.PROVINCIAL_TAX)
    standard = next(r for r in _lines_of(tables, 22) if r[cols.index("display_type")] == "tax"
                    and r[cols.index("tax_line_id")] == build.SALE_TAX)
    # 0.01 moves from the standard tax to the provincial one; the total stays.
    _set(tables, pst, balance="-7.01", amount_currency="-7.01", credit="7.01")
    _set(tables, standard, balance="-22.49", amount_currency="-22.49", credit="22.49")
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle
    doc = next(d for d in bundle.documents if d.source_external_id == "S:INV/2024/00005")
    assert _line_taxes(doc) == [[("15%", "251000", D(15)), ("PST 7%", "251000", D("7.01"))],
                                [("15%", "251000", D("7.49"))]]


# ── Payments matched by reconciliation ────────────────────────────────────────

def _lines_of(tables: build.Tables, move: int) -> list[list]:
    cols, rows = tables.rows["account_move_line"]
    return [r for r in rows if r[cols.index("move_id")] == move]


def _set(tables: build.Tables, row: list, **values) -> None:
    cols = tables.rows["account_move_line"][0]
    for name, value in values.items():
        row[cols.index(name)] = value


def _party_line(tables: build.Tables, move: int) -> list:
    cols = tables.rows["account_move_line"][0]
    return next(r for r in _lines_of(tables, move) if r[cols.index("account_id")] in (4, 5))


def _resize(tables: build.Tables, move: int, amount: Decimal) -> None:
    """Every line of a two-line base currency move moved to `amount` either way."""
    cols = tables.rows["account_move_line"][0]
    for r in _lines_of(tables, move):
        new = amount if D(r[cols.index("balance")]) > 0 else -amount
        _set(tables, r, balance=f"{new}", amount_currency=f"{new}", debit=f"{max(new, D(0))}",
             credit=f"{max(-new, D(0))}")


def _reconcile(tables: build.Tables, debit: int, credit: int, amount: str, *own: str) -> None:
    """One more partial reconciliation between two moves' customer or supplier lines."""
    rows = tables.rows["account_partial_reconcile"][1]
    rows.append([len(rows) + 90, _party_line(tables, debit)[0], _party_line(tables, credit)[0], 1, amount,
                 "2024-04-30", *(own or (amount, amount))])


def _statement_line(tables: build.Tables, move: int) -> None:
    """A move posted by a bank statement line matched straight to documents: no account.payment."""
    pcols, payments = tables.rows["account_payment"]
    payments[:] = [p for p in payments if p[pcols.index("move_id")] != move]
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == move)[mcols.index("payment_id")] = None
    for r in _lines_of(tables, move):
        _set(tables, r, payment_id=None)


def _book(tmp_path, tables: build.Tables):
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle
    docs = {d.source_external_id: (d.status, d.amount_outstanding) for d in bundle.documents}
    settled = {s.source_external_id: (s.amount, [(a.document_external_id, a.amount) for a in s.allocations])
               for s in bundle.settlements}
    return bundle, docs, settled


def test_a_bank_statement_line_matched_to_an_invoice_pays_it(tmp_path):
    """Odoo's bank reconciliation matches a statement line straight to the invoice."""
    tables = build.tables(17)
    _statement_line(tables, 6)
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00001"] == ("paid", D(0))
    assert settled["J:6"] == (D(345), [("S:INV/2024/00001", D(345))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:6").source_type == "Customer Payment"
    assert "J:6" not in {j.source_external_id for j in bundle.journals}


def test_one_payment_for_two_invoices_settles_each_by_what_was_applied(tmp_path):
    """Lakeshore pays INV/2024/00001 and 100.00 of INV/2024/00002 with one payment of 445.00."""
    tables = build.tables(17)
    mcols, moves = tables.rows["account_move"]
    second = next(m for m in moves if m[0] == 7)
    second[mcols.index("partner_id")] = second[mcols.index("commercial_partner_id")] = 7
    for r in _lines_of(tables, 7):
        _set(tables, r, partner_id=7)
    pcols, partials = tables.rows["account_partial_reconcile"]
    partials[:] = [p for p in partials if p[pcols.index("credit_move_id")] != _party_line(tables, 8)[0]]
    _set(tables, _party_line(tables, 8), amount_residual="-100.00")       # Pine's payment, applied to nothing
    _resize(tables, 6, D(445))
    _reconcile(tables, 7, 6, "100")
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00001"] == ("paid", D(0))
    assert docs["S:INV/2024/00002"] == ("awaiting_payment", D(130))
    assert settled["J:6"] == (D(445), [("S:INV/2024/00001", D(345)), ("S:INV/2024/00002", D(100))])
    assert "J:8" not in settled and "J:8" in {j.source_external_id for j in bundle.journals}


def test_an_overpayment_settles_the_invoice_and_leaves_the_rest_as_credit(tmp_path):
    """400.00 received against a 345.00 invoice: Odoo reconciles 345.00 and leaves 55.00 open
    credit, which stays on the customer's balance as the payment's journal."""
    tables = build.tables(17)
    _resize(tables, 6, D(400))
    _set(tables, _party_line(tables, 6), amount_residual="-55.00")
    pcols, payments = tables.rows["account_payment"]
    next(p for p in payments if p[pcols.index("move_id")] == 6)[pcols.index("amount")] = "400"
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00001"] == ("paid", D(0))
    assert settled["J:6"] == (D(345), [("S:INV/2024/00001", D(345))])
    credit = next(j for j in bundle.journals if j.source_external_id == "J:6:journal")
    assert credit.fallback is True
    assert {(ln.account_external_id, ln.contact_external_id, ln.debit - ln.credit) for ln in credit.lines} == {
        ("121000", "Lakeshore Trading Co", D(-55)), ("101401", None, D(55))}


def test_a_base_currency_payment_of_a_foreign_invoice_is_allocated_in_the_invoice_currency(tmp_path):
    """INV/2024/00004 is EUR 100 at 1.10. The customer pays USD 55 from a USD account and Odoo
    reconciles EUR 50 of it: Celerp allocates EUR 50 at 1.10 and leaves EUR 50 open."""
    tables = build.tables(17)
    for r in _lines_of(tables, 18):
        _set(tables, r, currency_id=build.USD)
    _resize(tables, 18, D(55))
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == 18)[mcols.index("currency_id")] = build.USD
    next(m for m in moves if m[0] == 17)[mcols.index("amount_residual")] = "50.00"
    pcols, payments = tables.rows["account_payment"]
    pay = next(p for p in payments if p[pcols.index("move_id")] == 18)
    pay[pcols.index("currency_id")], pay[pcols.index("amount")] = build.USD, "55"
    _set(tables, _party_line(tables, 17), amount_residual="55.00")
    rcols, partials = tables.rows["account_partial_reconcile"]
    partials[:] = [p for p in partials if p[rcols.index("amount")] not in ("110", "2")]
    tables.rows["account_move"][1][:] = [m for m in moves if m[0] != 19]            # no exchange difference
    tables.rows["account_move_line"][1][:] = [r for r in tables.rows["account_move_line"][1]
                                              if r not in _lines_of(tables, 19)]
    _reconcile(tables, 17, 18, "55", "50", "55")
    bundle, docs, settled = _book(tmp_path, tables)
    assert settled["J:18"] == (D(50), [("S:INV/2024/00004", D(50))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.1")
    assert docs["S:INV/2024/00004"] == ("awaiting_payment", D(50))


@pytest.mark.asyncio
async def test_a_statement_line_reconciled_invoice_migrates_paid(real_engine, monkeypatch, tmp_path):
    tables = build.tables(17)
    _statement_line(tables, 6)
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, tables))],
                                      {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)


# ── Stock and files ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_stock_moves_into_and_out_of_internal_locations(tmp_path, version):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    moved = [(a.source_type, a.source_external_id, a.kind, a.adjustment_date, a.item_external_id, a.quantity, a.value)
             for a in manifest.bundle.inventory_adjustments]
    # A move between two shelves changes nothing, and a move not yet done is not carried.
    assert moved == [
        ("Stock move", "stock:1", "adjustment", date(2024, 1, 10), "WID-1", D(10), D(300)),
        ("Stock move", "stock:2", "adjustment", date(2024, 1, 20), "WID-1", D(-4), D(-120)),
        ("Stock move", "stock:4", "adjustment", date(2024, 3, 1), "WID-1", D(1), D(30)),
        ("Stock valuation", "valuation:4", "adjustment", date(2024, 3, 15), "WID-1", D(0), D(10)),
    ]
    coverage = {r.source_type: (r.count, r.coverage_class) for r in manifest.coverage}
    assert coverage["Stock location"] == (2, CoverageClass.MAPPED_WITH_LOSS)
    assert coverage["Lot or serial number"] == (1, CoverageClass.UNSUPPORTED_NONFINANCIAL)
    assert coverage["Draft or cancelled journal entry"] == (1, CoverageClass.IGNORED_NON_BUSINESS)


def test_cutover_carries_each_items_position_then_the_moves_after(tmp_path):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(17)),
                                         MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER))
    assert [(a.kind, a.adjustment_date, a.quantity, a.value) for a in manifest.bundle.inventory_adjustments] == [
        ("opening", CUTOVER, D(6), D(180)), ("adjustment", date(2024, 3, 1), D(1), D(30)),
        ("adjustment", date(2024, 3, 15), D(0), D(10))]


@pytest.mark.parametrize("version", VERSIONS)
def test_files_on_documents_contacts_and_products_are_screened(tmp_path, version):
    files = _file(tmp_path, _backup(version))
    manifest = _adapter().build_manifest(files, FULL)
    attached = {a.source_external_id: (a.file_name, a.target_source_type, a.target_source_external_id)
                for a in manifest.bundle.attachments}
    assert attached == {
        "attachment:1": ("INV-2024-00001.pdf", "Customer Invoice", "S:INV/2024/00001"),
        "attachment:2": ("contract.txt", "Contact", "Lakeshore Trading Co"),
        "attachment:3": ("widget.png", "Item", "WID-1"),
        "attachment:9": ("terms.txt", "Contact", "Northwind Wholesale"),     # kept in the database
    }
    rejected = {r["source_external_id"]: r["reason"] for r in manifest.source_summary["attachments"]["rejected"]}
    assert set(rejected) == {"attachment:4", "attachment:5", "attachment:6", "attachment:10"}
    assert "isn't brought into Celerp" in rejected["attachment:4"]          # on a journal entry
    assert "contents aren't accepted" in rejected["attachment:5"]           # an executable
    assert "contents aren't accepted" in rejected["attachment:6"]           # not the JPEG it claims
    assert "missing from the backup" in rejected["attachment:10"]

    assert _adapter().read_attachment(files, "attachment:1") == build.PDF
    assert _adapter().read_attachment(files, "attachment:9") == b"Payment on delivery.\n"
    for key in ("attachment:4", "attachment:7", "attachment:99"):
        with pytest.raises(ScanError):
            _adapter().read_attachment(files, key)


# ── Reconciliation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_expectations_come_from_the_source(tmp_path, version):
    expectations = _adapter().source_expectations(_file(tmp_path, _backup(version)), FULL).expectations
    by = {(e.measure, e.key): e.expected for e in expectations}
    end = _source_balances()
    assert by[(M.DEBITS_EQUAL_CREDITS, "")] == 0
    for code, balance in end.items():
        assert by[(M.TRIAL_BALANCE, code)] == balance, code
    assert by[(M.TRIAL_BALANCE_AT, "121000@2024-02-29")] == _source_balances(date(2024, 2, 29))["121000"]
    assert by[(M.BANK_CASH, "101000")] == D(10345)
    assert by[(M.AR_CONTROL, "121000")] == D("446.00")
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co")] == D("136.50")
    assert by[(M.AR_BY_CUSTOMER, "Pine & Co")] == D("309.50")
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co [11]")] == 0
    assert by[(M.AP_BY_SUPPLIER, "Northwind Wholesale")] == D("-307.50")
    assert by[(M.AR_DOCUMENT_RESIDUAL, "")] == 0                  # open invoices less open credit = AR
    assert by[(M.AP_DOCUMENT_RESIDUAL, "")] == 0
    assert by[(M.INVENTORY_QUANTITY, "WID-1")] == D(7)           # what the quants hold, not the moves
    assert by[(M.INVENTORY_VALUE, "WID-1")] == D(220)
    assert by[(M.DOCUMENT_COUNT, "invoice")] == D(6)


@pytest.mark.parametrize("version", VERSIONS)
def test_reading_a_backup_twice_gives_the_same_manifest(tmp_path, version):
    files = _file(tmp_path, _backup(version))
    first = _adapter().build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    again = _adapter().build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    assert first == again


# ── End to end ────────────────────────────────────────────────────────────────

async def _events(engine, company_id) -> int:
    return await count(engine, "ledger", "company_id = :c", c=company_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": CUTOVER.isoformat()}])
@pytest.mark.parametrize("version", VERSIONS)
async def test_an_odoo_backup_migrates_and_reconciles(real_engine, monkeypatch, tmp_path, version, decisions):
    """The backup migrates through the real runner with nothing rejected and every
    reconciliation check passing; running the whole import again writes nothing new."""
    from celerp.services import migrations

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(version))], decisions, monkeypatch,
                                      tmp_path)
    assert rejected == []
    _passing(run)
    measures = {r["check"] for r in run.reconciliation["rows"]}
    assert {"debits_equal_credits", "trial_balance_at", "ar_by_customer", "ap_by_supplier", "bank_cash",
            "inventory_quantity", "inventory_value", "ar_document_residual"} <= measures

    events = await _events(real_engine, run.company_id)
    maps = await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=run.id)
    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET phase_state = '{}' WHERE id = :r"), {"r": run.id})
    await migrations.run_migration(run.id)
    again = await load_run(real_engine, run.id)
    _passing(again)
    assert all(entry["status"] == "done" for entry in again.phase_state.values())
    assert await _events(real_engine, run.company_id) == events
    assert await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=run.id) == maps
    async with maker(real_engine)() as s:
        report = await migrations.run_view(s, again)
    assert all(e["coverage_class"] != "unsupported_financial_blocker" for e in report["coverage"])
