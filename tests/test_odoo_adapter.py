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
    assert sorted(t.name for t in scan.bundle.tax_codes) == ["15%", "15% [2]"]

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
    }
    assert {(t.name, t.rate_percent, t.account_external_id) for t in bundle.tax_codes} == {
        ("15%", D(15), "251000"), ("15% [2]", D(15), "131000")}
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
    }
    invoice = docs["S:INV/2024/00001"]
    assert invoice.contact_external_id == "Lakeshore Trading Co"
    assert [(ln.item_external_id, ln.quantity, ln.unit_price, ln.total_price, ln.tax_amount, ln.account_external_id,
             ln.tax_code_external_id) for ln in invoice.line_items] == [
        ("WID-1", D(4), D(50), D(200), D(30), "400000", "15%"),
        ("SRV", D(1), D(100), D(100), D(15), "400000", "15%")]
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
    assert by[(M.AR_CONTROL, "121000")] == D("72.50")
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co")] == D("-57.50")
    assert by[(M.AR_BY_CUSTOMER, "Pine & Co")] == D("130.00")
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co [11]")] == 0
    assert by[(M.AP_BY_SUPPLIER, "Northwind Wholesale")] == D("-195.50")
    assert by[(M.AR_DOCUMENT_RESIDUAL, "")] == 0                  # open invoices less open credit = AR
    assert by[(M.AP_DOCUMENT_RESIDUAL, "")] == 0
    assert by[(M.INVENTORY_QUANTITY, "WID-1")] == D(7)           # what the quants hold, not the moves
    assert by[(M.INVENTORY_VALUE, "WID-1")] == D(220)
    assert by[(M.DOCUMENT_COUNT, "invoice")] == D(4)


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
