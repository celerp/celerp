# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Export-set sources: a set of report and list exports read as one company's books.

The fixture is a fictitious company's three years of trading, written the way the
source's export menus write it."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from functools import cache

import openpyxl
import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.manager_io.support import BASIC, artifact
from fixtures.xero_export import build
from migration_support import maker, real_engine  # noqa: F401
from test_migration_e2e import _maps, _passing, migrate_set

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = date(2025, 6, 30)


@cache
def _xero() -> tuple[tuple[str, bytes], ...]:
    return tuple(build.files().items())


def _set(tmp_path, files) -> list:
    folder = tmp_path / "upload"
    folder.mkdir(exist_ok=True)
    out = []
    for name, data in files:
        (folder / name).write_bytes(data)
        out.append(artifact(folder / name))
    return out


def _adapter():
    from celerp.importers.adapters.exports import XeroAdapter

    return XeroAdapter()


def _without(*names: str) -> list[tuple[str, bytes]]:
    return [(n, d) for n, d in _xero() if n not in names]


def _dated(data: bytes, heading: str) -> bytes:
    book = openpyxl.load_workbook(io.BytesIO(data))
    book.active["A3"] = heading
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


# ── Recognition ───────────────────────────────────────────────────────────────

def test_xero_export_set_is_recognised_by_content_alone(tmp_path):
    from celerp.importers.adapters.registry import detect_adapter

    files = _set(tmp_path, [(f"export-{n}{name[name.rfind('.'):]}", data) for n, (name, data) in enumerate(_xero())])
    assert detect_adapter(files).key == "xero"
    scan = _adapter().inspect(files)
    assert scan.company_name == build.COMPANY
    assert scan.base_currency == "GBP"
    assert scan.currencies == ("GBP", "USD")
    assert (scan.period_start, scan.period_end) == (date(2023, 1, 2), date(2025, 12, 31))
    assert "foreign_currency" in scan.features

    # The Manager adapter does not claim a set, and the export adapter does not claim a Manager file.
    assert _adapter().detect([artifact(BASIC)]).matched is False
    assert detect_adapter([artifact(BASIC)]).key == "manager_io"


def test_xero_export_set_in_one_zip_reads_the_same(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in _xero():
            z.writestr(f"Harbour/{name}", data)
    zipped = _adapter().inspect(_set(tmp_path, [("xero-export.zip", archive.getvalue())]))
    loose = _adapter().inspect(_set(tmp_path, _xero()))
    assert zipped == loose


def test_a_set_missing_a_file_or_holding_a_stranger_names_each_file(tmp_path):
    files = _set(tmp_path, [*_without("General Ledger.txt"), ("notes.txt", b"call the bank\n")])
    assert _adapter().detect(files).matched is True
    with pytest.raises(ScanError) as caught:
        _adapter().inspect(files)
    message = str(caught.value)
    assert "General Ledger" in message                     # missing
    assert "notes.txt" in message                          # not recognised
    assert "ChartOfAccounts.csv (Chart of accounts)" in message   # what was recognised


def test_overlapping_batches_are_refused(tmp_path):
    first = dict(_xero())["Invoices 1.csv"]
    with pytest.raises(ScanError, match="Invoices 1.csv.*Invoices 9.csv"):
        _adapter().inspect(_set(tmp_path, [*_xero(), ("Invoices 9.csv", first)]))


def test_a_year_without_its_trial_balance_is_refused(tmp_path):
    with pytest.raises(ScanError, match="Trial Balance.*2024"):
        _adapter().inspect(_set(tmp_path, _without("Trial Balance 2024.xlsx")))


def test_an_aged_report_older_than_the_ledger_is_refused(tmp_path):
    files = dict(_xero())
    files["Aged Receivables Detail.xlsx"] = _dated(files["Aged Receivables Detail.xlsx"], "As at 30 Nov 2025")
    with pytest.raises(ScanError, match="Aged Receivables Detail.*31 Dec 2025"):
        _adapter().inspect(_set(tmp_path, files.items()))


def test_slashed_dates_are_read_in_the_one_order_the_set_allows():
    from celerp.importers.adapters.exports.files import DateReader

    assert DateReader(["13/02/2024", "01/03/2024"])("01/03/2024") == date(2024, 3, 1)
    assert DateReader(["02/13/2024", "01/03/2024"])("01/03/2024") == date(2024, 1, 3)
    assert DateReader(["2024-03-01"])("2024-03-01") == date(2024, 3, 1)
    with pytest.raises(ScanError, match="day or the month"):
        DateReader(["01/02/2024", "03/04/2024"])
    with pytest.raises(ScanError, match="both"):
        DateReader(["13/02/2024", "02/13/2024"])


# ── Masters and documents ─────────────────────────────────────────────────────

def test_xero_masters_and_documents_carry_into_cif(tmp_path):
    manifest = _adapter().build_manifest(_set(tmp_path, _xero()), FULL)
    bundle = manifest.bundle
    assert bundle.company.name == build.COMPANY and bundle.company.base_currency == "GBP"
    assert [c.code for c in bundle.currencies] == ["GBP", "USD"]

    accounts = {a.code: a for a in bundle.accounts}
    assert set(accounts) == {code for code, *_ in build.CHART}
    assert (accounts["091"].control, accounts["091"].currency) == ("bank", "USD")
    assert (accounts["610"].control, accounts["800"].control, accounts["820"].control) == (
        "receivable", "payable", "tax")
    assert accounts["499"].control == "exchange_difference"
    assert accounts["960"].control == "retained_earnings"
    assert accounts["430"].is_active is False
    assert (accounts["310"].account_type, accounts["416"].account_type, accounts["720"].account_type) == (
        "cogs", "expense", "asset")

    taxes = {tc.name: tc.rate_percent for tc in bundle.tax_codes}
    assert taxes["20% (VAT on Income)"] == Decimal(20) and taxes["Zero Rated Income"] == 0

    contacts = {c.name: c for c in bundle.contacts}
    assert set(contacts) >= {name for name, *_ in build.CUSTOMERS} | {name for name, _ in build.SUPPLIERS}
    assert contacts["Keystone Imports Inc"].currency == "USD"
    assert contacts["Keystone Imports Inc"].roles == ["customer"]
    assert contacts["Mill Timber Supply"].roles == ["supplier"]
    assert {i.sku for i in bundle.items} == {code for code, *_ in build.ITEMS}

    docs = {d.source_external_id: d for d in bundle.documents}
    source = {d.number: d for d in build.simulate().documents}
    assert len(docs) == len([d for d in source.values() if d.status != "Deleted"])
    statuses = {d.ref: d.status for d in docs.values()}
    assert [statuses[n] for n, d in source.items() if d.status == "Voided"] == ["void"] * 6
    assert [statuses[n] for n, d in source.items() if d.status == "Draft"] == ["draft"]
    usd = next(d for d in docs.values() if d.currency == "USD" and d.status == "paid")
    assert usd.exchange_rate is not None and usd.line_items[0].tax_code_external_id
    notes = [d for d in docs.values() if d.doc_type == "credit_note"]
    assert len(notes) == 3 and all(n.metadata["applies_to"] == f"S:{source[n.ref].reference}" for n in notes)
    assert docs["S:INV-0001"].line_items[0].item_external_id is not None
    assert docs["S:INV-0001"].metadata["tracking"] == [{"Region": "North", "Project": "Beta"}]

    coverage = {row.source_type: (row.count, row.coverage_class) for row in manifest.coverage}
    assert coverage["Deleted invoice or bill"] == (3, CoverageClass.IGNORED_NON_BUSINESS)
    assert coverage["Tracking category"][1] == CoverageClass.UNSUPPORTED_NONFINANCIAL
    assert all(row.coverage_class != CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER for row in manifest.coverage)


def test_xero_expectations_come_from_the_reports_not_the_conversion(tmp_path):
    expectations = _adapter().source_expectations(_set(tmp_path, _xero()), FULL).expectations
    by = {(e.measure, e.key): e.expected for e in expectations}
    books = build.simulate()
    end = books.balances(build.END)
    accounts = {a: a for a, *_ in build.CHART}
    # The trial balance at the last month end, and at every month end before it.
    assert by[(M.TRIAL_BALANCE, accounts["090"])] == end["090"]
    assert by[(M.BANK_CASH, accounts["091"])] == end["091"]
    june = books.balances(date(2024, 6, 30))
    assert by[(M.TRIAL_BALANCE_AT, "090@2024-06-30")] == june["090"]
    assert len({k for m, k in by if m == M.TRIAL_BALANCE_AT and k.endswith("@2023-01-31")}) > 5
    # Customer balances from the aged receivables report.
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co")] == sum(
        (p.amount for j in books.journals if j.contact == "Lakeshore Trading Co" for p in j.postings
         if p.account == "610"), Decimal(0))


# ── End to end ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": CUTOVER.isoformat()}])
async def test_quickbooks_and_xero_adapters(real_engine, monkeypatch, tmp_path, decisions):
    """An export set migrates through the real runner with nothing rejected and every
    reconciliation check passing, month-end trial balances included; every fallback is
    listed as mapped with loss."""
    from celerp.services import migrations

    run, rejected = await migrate_set(real_engine, list(_xero()), decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    measures = {r["measure"] for r in run.reconciliation["rows"]}
    assert {"trial_balance_at", "ar_by_customer", "ap_by_supplier", "bank_cash"} <= measures
    async with maker(real_engine)() as s:
        report = await migrations.run_view(s, run)
    coverage = {(e["source_type"], e["coverage_class"]) for e in report["coverage"]}
    for m in await _maps(real_engine, run):
        if m.meta.get("representation") == "journal_fallback":
            assert ((m.source_type, "mapped_with_loss") in coverage
                    or (f"{m.source_type} (journal fallback)", "mapped_with_loss") in coverage), m.source_type
