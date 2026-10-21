# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Odoo backups: one database manager zip read as one company's books.

The fixture is a fictitious company's first four months, written as Odoo 13, 14 and 17
lay their tables out (tests/fixtures/odoo_backup/build.py)."""

from __future__ import annotations

import io
import json
import tracemalloc
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from functools import cache

import pytest
from sqlalchemy import text

from celerp.importers.adapters.base import COMPANY_QUESTION, MigrationDecisions, ScanError, SourceRevisionError
from celerp.importers.adapters.odoo import attachments
from celerp.importers.adapters.odoo.adapter import OTHER_COMPANY
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.manager_io.support import BASIC, artifact
from fixtures.odoo_backup import build
from migration_support import count, load_run, maker, real_engine  # noqa: F401
from test_migration_e2e import _passing, migrate_set

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = date(2024, 2, 29)
VERSIONS = (13, 14, 15, 16, 17)
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
    """Each account's balance from the fixture's own posted entries, debit positive. Off-balance
    accounts are outside the books."""
    off_balance = {i for i, _, _, kind, _ in build.ACCOUNTS if kind == "off_balance"}
    out: dict[str, Decimal] = defaultdict(Decimal)
    for move in build.moves():
        if move.state == "posted" and (on is None or date.fromisoformat(move.date) <= on):
            for line in move.lines:
                if line.account not in off_balance:
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


def _rezipped(*folders: str) -> bytes:
    """The fixture backup extracted and zipped again into each of `folders`, the way Finder,
    Explorer or Archive Utility zip a folder, with the resource files macOS adds."""
    real = zipfile.ZipFile(io.BytesIO(_backup(17)))
    return _zip({**{f"{folder}/{name}": real.read(name) for folder in folders for name in real.namelist()},
                 "__MACOSX/harbour/._dump.sql": b"\x00\x05\x16\x07", "harbour/._manifest.json": b"\x00"})


def test_a_backup_zipped_again_inside_its_folder_is_read_whole(tmp_path):
    files = _file(tmp_path, _rezipped("harbour"))
    assert _adapter().detect(files).matched
    original = _adapter().build_manifest(_file(tmp_path, _backup(17), "original.zip"), FULL)
    manifest = _adapter().build_manifest(files, FULL)
    assert manifest.bundle.model_dump() == original.bundle.model_dump()     # the filestore is found too
    assert _adapter().read_attachment(files, FULL, "attachment:1") == build.PDF

    two = _adapter().detect(_file(tmp_path, _rezipped("harbour", "harbour copy"), "two.zip"))
    assert two.matched is False and "not an Odoo backup" in two.reason


def test_a_pg_dump_custom_format_backup_is_refused_with_its_own_message(tmp_path):
    from celerp.importers.adapters.registry import detect_adapter

    custom = b"PGDMP\x01\x0e\x00" + bytes(64)
    for label, data in {"bare": custom, "zipped": _zip({"harbour.dump": custom}),
                        "zipped in a folder": _zip({"backups/harbour.dump": custom, "backups/notes.txt": "x"})}.items():
        with pytest.raises(SourceRevisionError, match="pg_dump custom format.*choose the zip format"):
            detect_adapter(_file(tmp_path, data, "harbour.dump"))
    # Deeper than one folder it is just some zip, and not recognised.
    deeper = _adapter().detect(_file(tmp_path, _zip({"a/b/harbour.dump": custom}), "deeper.zip"))
    assert deeper.matched is False and "not an Odoo backup" in deeper.reason


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


def test_a_backup_without_accounting_is_refused(tmp_path):
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
    assert docs["P:RBILL/2024/03/0001"].metadata == {"applications": {"P:BILL/2024/03/0001": "34.50"}}
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


@pytest.mark.parametrize("version", VERSIONS)
def test_only_lines_on_off_balance_accounts_are_counted_off_balance(tmp_path, version):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    rows = {r.source_type: (r.count, r.coverage_class) for r in manifest.coverage}
    # The guarantee's two lines; the invoice's section and note lines are layout, not items.
    assert rows["Off-balance journal item"] == (2, CoverageClass.UNSUPPORTED_NONFINANCIAL)
    invoice = next(d for d in manifest.bundle.documents if d.source_external_id == "S:INV/2024/00001")
    assert len(invoice.line_items) == 2
    assert "J:26" not in {j.source_external_id for j in manifest.bundle.journals}


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


def test_a_base_currency_overpayment_of_a_foreign_invoice_settles_at_the_rate_odoo_applied(tmp_path):
    """The customer pays USD 66 from a USD account and Odoo reconciles EUR 50 / USD 55 of it
    with INV/2024/00004 (EUR 100 at 1.10). The settlement is EUR 50 at 1.10, the receipt books
    no exchange difference, and the USD 11 Odoo left open stays on the customer."""
    tables = build.tables(17)
    for r in _lines_of(tables, 18):
        _set(tables, r, currency_id=build.USD)
    _resize(tables, 18, D(66))
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == 18)[mcols.index("currency_id")] = build.USD
    next(m for m in moves if m[0] == 17)[mcols.index("amount_residual")] = "50.00"
    pcols, payments = tables.rows["account_payment"]
    pay = next(p for p in payments if p[pcols.index("move_id")] == 18)
    pay[pcols.index("currency_id")], pay[pcols.index("amount")] = build.USD, "66"
    _set(tables, _party_line(tables, 17), amount_residual="55.00")
    _set(tables, _party_line(tables, 18), amount_residual="-11.00")
    rcols, partials = tables.rows["account_partial_reconcile"]
    partials[:] = [p for p in partials if p[rcols.index("amount")] not in ("110", "2")]
    _reconcile(tables, 17, 18, "55", "50", "55")
    bundle, docs, settled = _book(tmp_path, tables)
    assert settled["J:18"] == (D(50), [("S:INV/2024/00004", D(50))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.1")
    assert docs["S:INV/2024/00004"] == ("awaiting_payment", D(50))
    credit = next(j for j in bundle.journals if j.source_external_id == "J:18:journal")
    assert {(ln.account_external_id, ln.contact_external_id, ln.debit - ln.credit) for ln in credit.lines} == {
        ("121000", "Lakeshore Trading Co", D(-11)), ("101401", None, D(11))}


@pytest.mark.asyncio
async def test_a_statement_line_reconciled_invoice_migrates_paid(real_engine, monkeypatch, tmp_path):
    tables = build.tables(17)
    _statement_line(tables, 6)
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, tables))],
                                      {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)

# ── Credit notes applied in part ──────────────────────────────────────────────

def _note_for_pine(applied: bool = True) -> build.Tables:
    """The open credit note RINV/2024/00001 (57.50) issued to Pine & Co, which Odoo shows
    applied 20.00 to Pine & Co's INV/2024/00002: the invoice owes 110.00, the note 37.50 open.
    Without `applied` the reconciliation is missing while Odoo still states those amounts."""
    tables = build.tables(17)
    mcols, moves = tables.rows["account_move"]
    note = next(m for m in moves if m[0] == 9)
    note[mcols.index("partner_id")] = note[mcols.index("commercial_partner_id")] = 8
    for r in _lines_of(tables, 9):
        _set(tables, r, partner_id=8)
    if applied:
        _reconcile(tables, 7, 9, "20.00")
    next(m for m in moves if m[0] == 7)[mcols.index("amount_residual")] = "110.00"
    next(m for m in moves if m[0] == 9)[mcols.index("amount_residual")] = "37.50"
    _set(tables, _party_line(tables, 7), amount_residual="110.00")
    _set(tables, _party_line(tables, 9), amount_residual="-37.50")
    return tables


def test_a_credit_note_applied_in_part_relieves_the_invoice_by_that_amount(tmp_path):
    bundle, docs, _ = _book(tmp_path, _note_for_pine())
    assert docs["S:INV/2024/00002"] == ("awaiting_payment", D(110))
    assert docs["S:RINV/2024/00001"] == ("awaiting_payment", D("37.50"))
    notes = {d.source_external_id: d.metadata for d in bundle.documents}
    assert notes["S:RINV/2024/00001"] == {"applications": {"S:INV/2024/00002": "20.00"}}
    assert notes["P:RBILL/2024/03/0001"] == {"applications": {"P:BILL/2024/03/0001": "34.50"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": CUTOVER.isoformat()}])
async def test_a_credit_note_applied_in_part_migrates_with_both_open_amounts(real_engine, monkeypatch, tmp_path,
                                                                             decisions):
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, _note_for_pine()))],
                                      decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    owed = {r["key"]: D(r["celerp"]) for r in run.reconciliation["rows"] if r["check"] == "document_residual"}
    assert (owed["S:INV/2024/00002"], owed["S:RINV/2024/00001"]) == (D(110), D("37.50"))


@pytest.mark.asyncio
async def test_a_wrong_open_amount_on_one_document_fails_its_own_check(real_engine, monkeypatch, tmp_path):
    """Odoo states the open amounts of an application the backup does not hold. The invoice
    (+20.00) and the note (-20.00) cancel in every total, so only each document's own check
    can notice, and it names both."""
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, _note_for_pine(False)))],
                                      {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    failing = {(r["check"], r["key"]) for r in run.reconciliation["rows"] if r["result"] not in ("pass", "rounding")}
    assert failing == {("document_residual", "S:INV/2024/00002"), ("document_residual", "S:RINV/2024/00001")}


# ── Analytic distributions ────────────────────────────────────────────────────

# The consulting line of INV/2024/00001 as tracking: from Odoo 16 by plan with each share,
# before as its one analytic account and its tags.
ANALYTIC = {16: {"Projects": "Project Alpha 60%, Project Beta 40%", "Regions": "North"},
            13: {"Analytic account": "Project Alpha", "Analytic tag": "North"}}


def _analytic(version: int) -> dict[str, str]:
    return ANALYTIC[16 if version >= 16 else 13]


@pytest.mark.parametrize("version", VERSIONS)
def test_analytic_distributions_ride_on_document_lines_and_are_reported(tmp_path, version):
    from celerp.importers.adapters.exports.bundle import TRACKING
    from celerp.importers.adapters.odoo.book import read_odoo
    from celerp.importers.adapters.odoo.dump import Backup

    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    docs = {d.source_external_id: d for d in manifest.bundle.documents}
    assert [ln.tracking for ln in docs["S:INV/2024/00001"].line_items] == [{}, _analytic(version)]
    assert all(not ln.tracking for d in docs.values() if d.source_external_id != "S:INV/2024/00001"
               for ln in d.line_items)
    row = next(r for r in manifest.coverage if r.source_type == TRACKING)
    assert (row.count, row.coverage_class) == (2, CoverageClass.UNSUPPORTED_NONFINANCIAL)
    assert "journal entry lines" in row.note
    with Backup(_file(tmp_path, _backup(version))[0].path) as backup:
        journal = next(j for j in read_odoo(backup).book.journals if j.key == "J:20")
    region = "Regions" if version >= 16 else "Analytic tag"
    assert [ln.tracking for ln in journal.lines] == [{}, {region: "North"}]


@pytest.mark.asyncio
async def test_each_migrated_line_keeps_its_analytic_distribution(real_engine, monkeypatch, tmp_path):
    from test_migration_e2e import _projections

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(17))], {"mode": "full_history"},
                                      monkeypatch, tmp_path)
    assert rejected == []
    docs = {d["doc_number"]: d for d in (await _projections(real_engine, run, "doc")).values()}
    assert [ln.get("tracking") for ln in docs["INV/2024/00001"]["line_items"]] == [None, _analytic(17)]


# ── Stock and files ───────────────────────────────────────────────────────────

LOT_1, LOT_2, SHELF = "WID-1#LOT-0001", "WID-1#LOT-0002", "WID-1#LOT-0001@WH/Stock/Shelf 2"


@pytest.mark.parametrize("version", VERSIONS)
def test_stock_moves_each_lot_at_each_location(tmp_path, version):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    moved = [(a.source_external_id, a.adjustment_date, a.item_external_id, a.quantity, a.value)
             for a in manifest.bundle.inventory_adjustments]
    # A receipt's value is shared by its lots, a move between shelves takes its share of the
    # lot's cost along, a cost with no move falls on the stock held then, and a move not yet
    # done is not carried.
    assert moved == [
        ("stock:1", date(2024, 1, 10), LOT_1, D(6), D(180)),
        ("stock:2", date(2024, 1, 10), LOT_2, D(4), D(120)),
        ("stock:3", date(2024, 1, 20), LOT_2, D(-4), D(-120)),
        ("stock:4:to", date(2024, 2, 1), SHELF, D(2), D(60)),
        ("stock:4:from", date(2024, 2, 1), LOT_1, D(-2), D(-60)),
        ("stock:5", date(2024, 3, 1), LOT_1, D(1), D(30)),
        (f"valuation:5:{LOT_1}", date(2024, 3, 15), LOT_1, D(0), D("7.14")),
        (f"valuation:5:{SHELF}", date(2024, 3, 15), SHELF, D(0), D("2.86")),
    ]
    assert sum(a.value for a in manifest.bundle.inventory_adjustments) == D(220)
    coverage = {r.source_type: (r.count, r.coverage_class) for r in manifest.coverage}
    assert coverage["Stock location"] == (2, CoverageClass.MAPPED)
    assert coverage["Lot or serial number"] == (2, CoverageClass.MAPPED)
    assert coverage["Lot or serial number never stocked"] == (1, CoverageClass.IGNORED_NON_BUSINESS)
    assert coverage["Item"] == (2, CoverageClass.MAPPED)
    assert coverage["Draft or cancelled journal entry"] == (1, CoverageClass.IGNORED_NON_BUSINESS)


@pytest.mark.parametrize("version", VERSIONS)
def test_each_lot_and_location_is_stock_of_its_product(tmp_path, version):
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    # The location the company's stock starts in comes first, so it is Celerp's default.
    assert [loc.name for loc in bundle.locations] == ["WH/Stock", "WH/Stock/Shelf 2"]
    items = [(i.source_external_id, i.sku, i.product_external_id, i.batch_no, i.location_name)
             for i in bundle.items]
    assert items == [
        ("SRV", "SRV", None, None, "WH/Stock"), ("WID-1", "WID-1", None, None, "WH/Stock"),
        (LOT_1, "WID-1", "WID-1", "LOT-0001", "WH/Stock"), (SHELF, "WID-1", "WID-1", "LOT-0001", "WH/Stock/Shelf 2"),
        (LOT_2, "WID-1", "WID-1", "LOT-0002", "WH/Stock")]


def test_cutover_carries_each_items_position_then_the_moves_after(tmp_path):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(17)),
                                         MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER))
    assert [(a.kind, a.adjustment_date, a.item_external_id, a.quantity, a.value)
            for a in manifest.bundle.inventory_adjustments] == [
        ("opening", CUTOVER, LOT_1, D(4), D(120)), ("opening", CUTOVER, SHELF, D(2), D(60)),
        ("adjustment", date(2024, 3, 1), LOT_1, D(1), D(30)),
        ("adjustment", date(2024, 3, 15), LOT_1, D(0), D("7.14")),
        ("adjustment", date(2024, 3, 15), SHELF, D(0), D("2.86"))]


@pytest.mark.parametrize("version", VERSIONS)
def test_files_on_documents_contacts_and_products_are_screened(tmp_path, version):
    files = _file(tmp_path, _backup(version))
    manifest = _adapter().build_manifest(files, FULL)
    attached = {a.source_external_id: (a.file_name, a.target_source_type, a.target_source_external_id, a.item_image)
                for a in manifest.bundle.attachments}
    assert attached == {
        "attachment:1": ("INV-2024-00001.pdf", "Customer Invoice", "S:INV/2024/00001", False),
        "attachment:2": ("contract.txt", "Contact", "Lakeshore Trading Co", False),
        "attachment:3": ("widget.png", "Item", "WID-1", False),
        "attachment:7": ("WID-1.png", "Item", "WID-1", True),               # the product's own image
        "attachment:9": ("terms.txt", "Contact", "Northwind Wholesale", False),     # kept in the database
    }
    rejected = {r["source_external_id"]: r["reason"] for r in manifest.source_summary["attachments"]["rejected"]}
    assert set(rejected) == {"attachment:4", "attachment:5", "attachment:6", "attachment:10", "attachment:12"}
    assert "isn't brought into Celerp" in rejected["attachment:4"]          # on a journal entry
    assert "isn't brought into Celerp" in rejected["attachment:12"]         # on a quotation
    assert "contents aren't accepted" in rejected["attachment:5"]           # an executable
    assert "contents aren't accepted" in rejected["attachment:6"]           # not the JPEG it claims
    assert "missing from the backup" in rejected["attachment:10"]

    assert _adapter().read_attachment(files, FULL, "attachment:1") == build.PDF
    assert _adapter().read_attachment(files, FULL, "attachment:9") == b"Payment on delivery.\n"
    assert _adapter().read_attachment(files, FULL, "attachment:7") == build.PNG
    for key in ("attachment:4", "attachment:8", "attachment:11", "attachment:99"):
        with pytest.raises(ScanError):
            _adapter().read_attachment(files, FULL, key)
    with pytest.raises(ScanError, match="This attachment is not in the uploaded file."):
        _adapter().read_attachment(files, FULL, "attachment:99")


@pytest.mark.parametrize("version", VERSIONS)
def test_every_file_in_the_backup_is_carried_or_counted(tmp_path, version):
    """Odoo's interface files and the smaller copies it keeps of each picture are counted as
    not business files; every other file is carried or listed as left out."""
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    rows = {r.source_type: r for r in manifest.coverage}
    assert (rows["Attachment"].count, rows[attachments.REJECTED].count) == (5, 5)
    ignored = rows[attachments.IGNORED]
    assert (ignored.count, ignored.coverage_class) == (2, CoverageClass.IGNORED_NON_BUSINESS)
    assert rows["Attachment"].count + rows[attachments.REJECTED].count + ignored.count == len(build.ATTACHMENTS)


@pytest.mark.asyncio
async def test_the_odoo_product_image_becomes_the_items_picture(real_engine, monkeypatch, tmp_path):
    from test_migration_e2e import _projections

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(17))], {"mode": "full_history"},
                                      monkeypatch, tmp_path)
    assert rejected == []
    [item] = [i for i in (await _projections(real_engine, run, "item")).values()
              if i.get("sku") == "WID-1" and not i.get("catalog_item_id")]
    assert sorted((f["filename"], f["is_hero"], f["document_tag"]) for f in item["files"]) == [
        ("WID-1.png", True, "product_images"), ("widget.png", False, "product_images")]


@pytest.mark.asyncio
async def test_each_lot_migrates_as_stock_of_its_product_where_odoo_keeps_it(real_engine, monkeypatch, tmp_path):
    from test_migration_e2e import _projections

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(17))], {"mode": "full_history"},
                                      monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    async with real_engine.connect() as conn:
        places = dict((await conn.execute(text("SELECT id::text, name FROM locations WHERE company_id = :c"),
                                          {"c": run.company_id})).all())
    items = await _projections(real_engine, run, "item")
    [product] = [k for k, i in items.items() if i.get("sku") == "WID-1" and not i.get("catalog_item_id")]
    stock = sorted((i.get("batch_no"), places[i["location_id"]], i["quantity"], i["catalog_item_id"])
                   for i in items.values() if i.get("catalog_item_id"))
    assert stock == [("LOT-0001", "WH/Stock", 5, product), ("LOT-0001", "WH/Stock/Shelf 2", 2, product),
                     ("LOT-0002", "WH/Stock", 0, product)]
    assert items[product]["quantity"] == 0


# ── Several companies in one backup ───────────────────────────────────────────

GROUP = "Harbour Group Holdings"


def _second_company() -> build.Tables:
    """The fixture with a second company, Harbour Group Holdings, whose only books are the
    capital entry MISC/2024/01/0001, posted to its own copies of the two accounts it uses."""
    tables = build.tables(17)
    tables.rows["res_company"][1].append([2, GROUP, 900, build.USD, None, None, "12"])
    p_cols, partners = tables.rows["res_partner"]
    partners.append([{"id": 900, "name": GROUP, "commercial_partner_id": 900, "active": "t",
                      "is_company": "t", "customer_rank": 0, "supplier_rank": 0}.get(c) for c in p_cols])
    a_cols, accounts = tables.rows["account_account"]
    used = {r[tables.rows["account_move_line"][0].index("account_id")] for r in _lines_of(tables, 1)}
    for row in [r for r in accounts if r[a_cols.index("id")] in used]:
        copy = list(row)
        copy[a_cols.index("id")] += 1000
        copy[a_cols.index("company_id")] = 2
        accounts.append(copy)
    m_cols, moves = tables.rows["account_move"]
    next(r for r in moves if r[0] == 1)[m_cols.index("company_id")] = 2
    for row in _lines_of(tables, 1):
        _set(tables, row, company_id=2, account_id=row[tables.rows["account_move_line"][0].index("account_id")] + 1000)
    return tables


def _as(company: str) -> MigrationDecisions:
    return MigrationDecisions(mode=CIFMode.FULL_HISTORY, mappings={COMPANY_QUESTION: company})


def test_a_backup_of_two_companies_asks_which_one_to_import(tmp_path):
    files = _file(tmp_path, build.backup(17, _second_company()))
    scan = _adapter().inspect(files)
    (question,) = scan.questions
    assert (question.key, question.options, question.suggested) == (COMPANY_QUESTION, (build.COMPANY, GROUP),
                                                                    build.COMPANY)
    assert scan.company_name == build.COMPANY
    other = {r.source_type: r for r in scan.coverage}[OTHER_COMPANY]
    assert (other.count, other.coverage_class) == (1, CoverageClass.IGNORED_NON_BUSINESS)

    group = _adapter().inspect(files, GROUP)
    assert group.company_name == GROUP
    assert group.questions[0].suggested == GROUP                # the scan describes the company chosen
    assert group.object_counts["Journal entry"] == 1 and group.object_counts["Invoice or bill"] == 0
    assert {r.source_type: r.count for r in group.coverage}[OTHER_COMPANY] == len(build.moves()) - 1

    with pytest.raises(ScanError):
        _adapter().inspect(files, "Another Co")


def test_a_backup_of_one_company_asks_nothing(tmp_path):
    scan = _adapter().inspect(_file(tmp_path, _backup(17)))
    assert scan.questions == ()
    assert OTHER_COMPANY not in {r.source_type for r in scan.coverage}


def test_the_chosen_company_is_the_only_one_imported(tmp_path):
    files = _file(tmp_path, build.backup(17, _second_company()))
    manifest = _adapter().build_manifest(files, _as(GROUP))
    assert manifest.source == GROUP
    assert [j.source_external_id for j in manifest.bundle.journals] == ["J:1"]
    assert manifest.bundle.documents == [] and manifest.bundle.attachments == []
    with pytest.raises(ScanError):                              # a file on the other company's records
        _adapter().read_attachment(files, _as(GROUP), "attachment:1")

    harbour = _adapter().build_manifest(files, _as(build.COMPANY))
    assert "J:1" not in {j.source_external_id for j in harbour.bundle.journals}
    assert _adapter().read_attachment(files, _as(build.COMPANY), "attachment:1") == build.PDF


@pytest.mark.asyncio
async def test_each_company_of_a_backup_migrates_into_its_own_company(real_engine, monkeypatch, tmp_path):
    from celerp.models.company import Company

    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, _second_company()))],
                                      {"mode": "full_history", "mappings": {COMPANY_QUESTION: GROUP}},
                                      monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    async with maker(real_engine)() as s:
        assert (await s.get(Company, run.company_id)).name == GROUP     # the review proposed the chosen company


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
    # Each lot at each location holds what its quants hold; Odoo values stock per product only.
    assert {k: v for (m, k), v in by.items() if m == M.INVENTORY_QUANTITY and k != "WID-1"} == {
        LOT_1: D(5), SHELF: D(2), LOT_2: D(0)}
    assert {k for m, k in by if m == M.INVENTORY_VALUE} == {"WID-1"}
    assert by[(M.DOCUMENT_COUNT, "invoice")] == D(6)
    assert by[(M.DOCUMENT_RESIDUAL, "S:INV/2024/00002")] == D(130)
    assert by[(M.DOCUMENT_RESIDUAL, "S:RINV/2024/00001")] == D("57.50")
    assert by[(M.DOCUMENT_RESIDUAL, "S:INV/2024/00001")] == 0


@pytest.mark.parametrize("version", VERSIONS)
def test_reading_a_backup_twice_gives_the_same_manifest(tmp_path, version):
    files = _file(tmp_path, _backup(version))
    first = _adapter().build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    again = _adapter().build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    assert first == again


# ── A large backup ────────────────────────────────────────────────────────────

BULK = 10_000
LINE_BYTES = 900        # the bound per move line read; a row as a dict of every kept column took about 1,950


def _peak(tmp_path, lines: int) -> tuple[int, object]:
    """The most memory reading the books of a backup with an entry of `lines` lines takes."""
    from celerp.importers.adapters.odoo.book import read_odoo
    from celerp.importers.adapters.odoo.dump import Backup

    path = tmp_path / f"bulk-{lines}.zip"
    path.write_bytes(build.backup(14, build.tables(14, lines)))
    with Backup(path) as backup:
        tracemalloc.start()
        try:
            odoo = read_odoo(backup)
            return tracemalloc.get_traced_memory()[1], odoo
        finally:
            tracemalloc.stop()


def test_the_memory_of_reading_the_books_grows_by_a_bounded_size_per_move_line(tmp_path):
    """A row keeps only the columns read and a value its table repeats is held once, and of
    ir_model_data, where Odoo names each record, only the account types are kept."""
    small, _ = _peak(tmp_path, 2_000)
    large, odoo = _peak(tmp_path, 2_000 + BULK)
    assert max(len(j.lines) for j in odoo.book.journals) == 2_000 + BULK
    assert (large - small) / BULK < LINE_BYTES


def test_a_run_reads_the_dump_once_for_every_step(tmp_path, monkeypatch):
    from celerp.importers.adapters.odoo import adapter as odoo
    from celerp.importers.adapters.odoo.dump import Backup

    reads, tables = [], Backup.tables
    monkeypatch.setattr(Backup, "tables", lambda self, *a: reads.append(1) or tables(self, *a))
    odoo._books.cache_clear()
    files, cutover = _file(tmp_path, _backup(17)), MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER)
    adapter = _adapter()
    adapter.inspect(files)
    full = adapter.build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    at_cutover = adapter.build_manifest(files, cutover).model_dump(exclude={"exported_at"})
    adapter.source_expectations(files, FULL)
    assert adapter.read_attachment(files, FULL, "attachment:9") == b"Payment on delivery.\n"
    assert len(reads) == 1
    # A file kept in the database is held only when it is attached to a record Celerp holds.
    refs = odoo._books(files[0].path, files[0].sha256, None).attachments
    assert {k for k, r in refs.items() if r.stored is not None} == {"attachment:9"}

    # Each step builds on the book it shares as on one read afresh.
    assert adapter.build_manifest(files, FULL).model_dump(exclude={"exported_at"}) == full
    odoo._books.cache_clear()
    assert adapter.build_manifest(files, cutover).model_dump(exclude={"exported_at"}) == at_cutover
    assert len(reads) == 2


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
