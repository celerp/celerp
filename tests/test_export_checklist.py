# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The export checklist: what each export-set source needs, where its menus export each
file and for which dates, and what an upload so far holds, lacks or should not hold.

An unfinished set is kept as a draft that later uploads add to, so a user can bring the
files in as they export them."""

from __future__ import annotations

import io
import uuid
from datetime import date
from functools import cache

import openpyxl
import pytest

from celerp.importers.adapters.base import ScanError
from fixtures.qbo_export import build as qbo
from fixtures.xero_export import build
from migration_support import _chunks
from test_export_set_adapters import _qbo, _qbo_adapter, _set, _without, _xero
from test_export_set_adapters import _adapter as _xero_adapter


def _items(view: dict) -> dict[str, dict]:
    return {item["kind"]: item for item in view["items"]}


def _report(title: str, heading: str, header: list[str], rows: list[list]) -> bytes:
    book = openpyxl.Workbook()
    for line in ([title], [build.COMPANY], [heading], [], header, *rows):
        book.active.append(line)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _profit_and_loss() -> bytes:
    return _report("Profit and Loss", "For the year ended 31 December 2025", ["Account", "2025"],
                   [["Sales", 1000], ["Net Profit", 1000]])


def _chart_without_type() -> bytes:
    return b"*Code,*Name,*Tax Code,Description\r\n200,Sales,20% (VAT on Income),\r\n"


@cache
def _books():
    return qbo.simulate()


def _folder(path):
    path.mkdir(exist_ok=True)
    return path


def _journal_years(*periods: tuple[date, date]) -> list[tuple[str, bytes]]:
    rest = [(n, d) for n, d in _qbo() if n != "Journal.xlsx"]
    return rest + [(f"Journal {start.isoformat()}.xlsx", qbo.journal(_books(), start, end)) for start, end in periods]


# ── The checklist each source shows ─────────────────────────────────────────

@pytest.mark.parametrize("adapter, warning, kinds", [
    (_xero_adapter, "Xero closes access", {"chart", "contacts", "items", "documents", "ledger", "trial_balance",
                                           "aged_receivable", "aged_payable"}),
    (_qbo_adapter, "read-only for a year", {"accounts", "customers", "vendors", "products", "journal", "sales",
                                            "purchases", "received", "applied", "trial_balance", "ar_aging",
                                            "ap_aging"}),
])
def test_each_source_lists_every_file_with_its_menu_and_dates(adapter, warning, kinds):
    view = adapter().checklist().view()
    assert warning in view["warning"]
    assert set(_items(view)) == kinds
    for item in view["items"]:
        assert item["label"] and item["menu"] and item["range"]
        assert item["files"] == []
    assert view["complete"] is False
    xero_items = _items(_xero_adapter().checklist().view())
    assert xero_items["ledger"]["menu"] == "Accounting > Reports > General Ledger > Export"
    assert xero_items["ledger"]["status"] == "missing"
    assert xero_items["documents"]["status"] == "pending"   # needed only if the ledger posts invoices or bills
    assert xero_items["items"]["status"] == "optional"
    assert xero_items["chart"]["range"] == "--"            # a list has no dates to choose


# ── An upload checked against the list ──────────────────────────────────────

def test_the_check_names_received_missing_and_wrong_files(tmp_path):
    files = [(n, d) for n, d in _without("General Ledger.txt", "ChartOfAccounts.csv")]
    files += [("notes.txt", b"call the bank\n"), ("Chart.csv", _chart_without_type()),
              ("P&L.xlsx", _profit_and_loss())]
    check = _xero_adapter().check(_set(tmp_path, files))
    view = check.view()
    items = _items(view)
    assert items["ledger"]["status"] == "missing"
    assert items["chart"]["status"] == "missing"
    assert items["contacts"]["status"] == "received"
    assert items["contacts"]["files"] == ["Contacts.csv"]
    assert items["trial_balance"]["files"] == [f"Trial Balance {y}.xlsx" for y in build.YEARS]
    assert check.complete is False
    problems = " ".join(view["problems"])
    assert "General Ledger" in problems and "Accounting > Reports > General Ledger > Export" in problems
    assert "Chart.csv looks like Chart of accounts but lacks the column type" in problems
    assert "Accounting > Chart of accounts > Export" in problems
    assert "P&L.xlsx is a Profit and Loss report" in problems
    assert "notes.txt is not one of the Xero files" in problems


def test_a_complete_set_checks_clean_and_a_set_without_the_ledger_leaves_conditional_files_pending(tmp_path):
    complete = _xero_adapter().check(_set(tmp_path, _xero()))
    assert complete.complete is True and complete.view()["problems"] == []
    assert _items(complete.view())["documents"]["status"] == "received"
    only_chart = _xero_adapter().check(_set(tmp_path, [("ChartOfAccounts.csv", dict(_xero())["ChartOfAccounts.csv"])]))
    items = _items(only_chart.view())
    assert items["chart"]["status"] == "received"
    assert items["aged_payable"]["status"] == "pending"
    assert items["ledger"]["status"] == "missing"


def test_the_refusal_still_names_what_was_recognised(tmp_path):
    files = _set(tmp_path, [*_without("General Ledger.txt"), ("notes.txt", b"call the bank\n")])
    with pytest.raises(ScanError) as caught:
        _xero_adapter().inspect(files)
    message = str(caught.value)
    assert "Accounting > Reports > General Ledger > Export" in message
    assert "notes.txt" in message
    assert "ChartOfAccounts.csv (Chart of accounts)" in message


# ── A QuickBooks Online report exported a year at a time ─────────────────────

YEARS = [(date(y, 1, 1), date(y, 12, 31)) for y in qbo.YEARS]


def test_a_journal_exported_per_year_joins_and_reads_the_same(tmp_path):
    whole = _qbo_adapter().inspect(_set(tmp_path, _qbo()))
    per_year = _set(_folder(tmp_path / "years"), _journal_years(*YEARS))
    assert _qbo_adapter().check(per_year).complete is True
    assert _items(_qbo_adapter().check(per_year).view())["journal"]["files"] == [
        f"Journal {start.isoformat()}.xlsx" for start, _ in YEARS]
    assert _qbo_adapter().inspect(per_year) == whole


def test_overlapping_or_gapped_years_are_named_with_the_dates_to_export(tmp_path):
    overlap = _qbo_adapter().check(_set(tmp_path, _journal_years(
        (date(2023, 1, 1), date(2024, 6, 30)), (date(2024, 1, 1), date(2025, 12, 31)))))
    problems = " ".join(overlap.view()["problems"])
    assert "Journal 2024-01-01.xlsx overlaps Journal 2023-01-01.xlsx from 1 Jan 2024" in problems
    assert "Export Journal 2024-01-01.xlsx again from 1 Jul 2024" in problems

    gap = _qbo_adapter().check(_set(_folder(tmp_path / "gap"), _journal_years(YEARS[0], YEARS[2])))
    problems = " ".join(gap.view()["problems"])
    assert "Journal: nothing covers 1 Jan 2024 to 31 Dec 2024" in problems
    with pytest.raises(ScanError, match="nothing covers 1 Jan 2024 to 31 Dec 2024"):
        _qbo_adapter().inspect(_set(_folder(tmp_path / "gap"), _journal_years(YEARS[0], YEARS[2])))


# ── Drafts: a set brought in a file at a time ────────────────────────────────

@pytest.fixture
def export_store(tmp_path, monkeypatch):
    from celerp.config import settings
    from celerp.importers.adapters import registry
    from celerp.importers.adapters.exports import QuickBooksOnlineAdapter, XeroAdapter

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(registry, "_ADAPTERS", (XeroAdapter(), QuickBooksOnlineAdapter()))
    from celerp.services import migration_scan_store

    return migration_scan_store


OWNER = ("user", uuid.uuid4())


async def _parts(files, *, source="xero", draft=None, remove=(), check=False):
    from celerp.services.migration_scan_store import UploadPart

    yield UploadPart("source", None, _chunks(source.encode()))
    if draft:
        yield UploadPart("draft", None, _chunks(draft.encode()))
    for name in remove:
        yield UploadPart("remove", None, _chunks(name.encode()))
    if check:
        yield UploadPart("check", None, _chunks(b"1"))
    for name, data in files:
        yield UploadPart("files", name, _chunks(data))


async def test_an_incomplete_set_is_kept_until_the_rest_arrives(export_store):
    files = dict(_xero())
    first = await export_store.create_scan(_parts(_without("General Ledger.txt")), owner=OWNER)
    assert isinstance(first, export_store.DraftSession)
    assert _items(first.check.view())["ledger"]["status"] == "missing"
    done = await export_store.create_scan(_parts([("General Ledger.txt", files["General Ledger.txt"])],
                                                 draft=first.token), owner=OWNER)
    assert isinstance(done, export_store.ScanSession)
    assert done.scan.company_name == build.COMPANY
    assert len(done.artifacts) == len(files)
    assert not (export_store._scans_root() / first.token).exists()   # the draft became the scan


async def test_a_file_is_taken_out_of_a_draft(export_store):
    draft = await export_store.create_scan(_parts([*_xero(), ("notes.txt", b"call the bank\n")]), owner=OWNER)
    assert isinstance(draft, export_store.DraftSession)
    notes = next(a.path.name for a in draft.artifacts if a.original_name == "notes.txt")
    uploads = {u["name"]: u["stored_name"] for u in draft.view()["uploads"]}
    assert uploads["notes.txt"] == notes
    scan = await export_store.create_scan(_parts([], draft=draft.token, remove=[notes]), owner=OWNER)
    assert isinstance(scan, export_store.ScanSession)
    assert "notes.txt" not in {a.original_name for a in scan.artifacts}


async def test_check_only_keeps_a_complete_set_as_a_draft(export_store):
    draft = await export_store.create_scan(_parts(_xero(), check=True), owner=OWNER)
    assert isinstance(draft, export_store.DraftSession)
    assert draft.check.complete is True
    scan = await export_store.create_scan(_parts([], draft=draft.token), owner=OWNER)
    assert isinstance(scan, export_store.ScanSession)


async def test_a_draft_belongs_to_its_owner(export_store):
    draft = await export_store.create_scan(_parts(_without("General Ledger.txt")), owner=OWNER)
    with pytest.raises(export_store.ScanStoreError) as caught:
        await export_store.create_scan(_parts([], draft=draft.token), owner=("user", uuid.uuid4()))
    assert caught.value.status_code == 410
    assert (export_store._scans_root() / draft.token).exists()


async def test_a_set_the_reader_refuses_stays_a_draft_with_the_reason(export_store):
    files = dict(_xero())
    stale = _report("Aged Receivables Detail", "As at 30 Jun 2025", ["Invoice Number", "Total"], [])
    draft = await export_store.create_scan(_parts([*((n, d) for n, d in files.items()
                                                     if n != "Aged Receivables Detail.xlsx"),
                                                   ("Aged Receivables Detail.xlsx", stale)]), owner=OWNER)
    assert isinstance(draft, export_store.DraftSession)
    assert "Aged Receivables Detail" in " ".join(draft.view()["problems"])


async def test_a_manager_file_is_still_refused_outright(export_store, monkeypatch):
    from celerp.importers.adapters import registry
    from celerp.importers.adapters.manager_io.adapter import ManagerIOAdapter

    monkeypatch.setattr(registry, "_ADAPTERS", (ManagerIOAdapter(),))
    with pytest.raises(export_store.ScanStoreError) as caught:
        await export_store.create_scan(_parts([("books.manager", b"not a manager file")], source="manager_io"),
                                       owner=OWNER)
    assert caught.value.status_code == 422
