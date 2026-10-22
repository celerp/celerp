# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Sage 50 / Peachtree: its import/export templates and reports read as one company's books.

The fixture is a fictitious Ethiopian company's beginning balances and twenty-one months
of trading in Birr, written the way Sage 50's templates and Reports & Forms write them."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from functools import cache

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError, SourceRevisionError
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.sage50_export import build
from migration_support import real_engine  # noqa: F401
from test_export_set_adapters import _broken_workbook, _qbo, _set, _xero
from test_migration_e2e import _passing, migrate_set
from ui.i18n import t

# The books are in Birr, which Celerp does not keep books in yet; they are imported in the currency chosen.
USD = MigrationDecisions(mode=CIFMode.FULL_HISTORY, mappings={"currency": "USD"})
CUTOVER = date(2024, 6, 30)


@cache
def _books() -> build.Books:
    return build.simulate()


@cache
def _files() -> tuple[tuple[str, bytes], ...]:
    return tuple(build.files(_books()).items())


def _adapter():
    from celerp.importers.adapters.exports import Sage50Adapter

    return Sage50Adapter()


def _edited(name: str, old: str, new: str, count: int = 1) -> list[tuple[str, bytes]]:
    """The set with `old` written as `new` in one file, as damage or a hand edit would leave it."""
    out = []
    for n, data in _files():
        if n == name:
            text = data.decode("cp1252")
            assert old in text, old
            data = text.replace(old, new, count).encode("cp1252")
        out.append((n, data))
    return out


def _without(*names: str) -> list[tuple[str, bytes]]:
    return [(n, d) for n, d in _files() if n not in names]


def _zip(files) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files:
            z.writestr(f"Highland/{name}", data)
    return out.getvalue()


def _refusal(tmp_path, files) -> str:
    """What reading the set refuses with: a named refusal, never another error."""
    with pytest.raises((ScanError, SourceRevisionError)) as refused:
        _adapter().inspect(_set(tmp_path, files))
    return str(refused.value)


# ── Recognition and the checklist ────────────────────────────────────────────

def test_a_sage50_export_set_is_recognised_by_its_columns_alone(tmp_path):
    from celerp.importers.adapters.registry import detect_adapter

    files = _set(tmp_path, [(f"export-{n}{name[name.rfind('.'):]}", data) for n, (name, data) in enumerate(_files())])
    assert detect_adapter(files).key == "sage50"
    scan = _adapter().inspect(files)
    assert scan.company_name == build.COMPANY
    assert (scan.period_start, scan.period_end) == (build.OPENED, build.END)
    # The exports state no currency: the user chooses one Celerp keeps books in. Celerp keeps
    # none in Birr, the currency the reports' Br names, so none is suggested.
    from celerp.services.currencies import CURRENCIES
    assert scan.currencies == () and scan.base_currency is None
    [question] = scan.questions
    assert (question.key, question.suggested, question.options) == ("currency", None, CURRENCIES)
    # Neither of the other export-set readers claims a Sage 50 set, nor this one theirs.
    from test_export_set_adapters import _adapter as xero, _qbo_adapter as qbo
    assert xero().detect(files).matched is False and qbo().detect(files).matched is False
    assert _adapter().detect(_set(tmp_path, _xero())).matched is False
    assert _adapter().detect(_set(tmp_path, _qbo())).matched is False


def test_a_sage50_set_in_one_zip_reads_the_same(tmp_path):
    zipped = _adapter().inspect(_set(tmp_path, [("sage50.zip", _zip(_files()))]))
    assert zipped == _adapter().inspect(_set(tmp_path, _files()))


def test_the_checklist_lists_every_template_and_report_with_its_menu():
    view = _adapter().checklist().view()
    items = {i["kind"]: i for i in view["items"]}
    assert set(items) == {"chart", "customers", "vendors", "items", "employees", "general", "sales", "receipts",
                          "purchases", "payments", "trial_balance", "aged_receivables", "aged_payables"}
    assert items["sales"]["menu"] == "File > Select Import/Export > Accounts Receivable > Sales Journal > Export"
    assert items["trial_balance"]["menu"].startswith("Reports & Forms > General Ledger")
    assert {k for k, i in items.items() if i["status"] == "missing"} == {"chart", "trial_balance"}
    assert "Include Headings" in view["warning"]
    assert "Peachtree" in _adapter().accepts and "Include Headings" in _adapter().accepts


def test_older_column_names_and_four_digit_years_read_the_same(tmp_path):
    """Peachtree 2010 heads the invoice and check number columns differently, and a set may
    print four-digit years."""
    old = _adapter().build_manifest(_set(tmp_path, build.files(_books(), old=True, four=True).items()), USD)
    new = _adapter().build_manifest(_set(tmp_path, _files()), USD)
    assert old.bundle == new.bundle


def test_each_journal_not_uploaded_is_listed_with_what_it_means(tmp_path):
    scan = _adapter().inspect(_set(tmp_path, _without("PURCHASE.CSV", "PAYMENTS.CSV", "VENDOR.CSV",
                                                      "Aged Payables.xlsx")))
    rows = {r.source_type: r for r in scan.coverage}
    assert rows["Purchase Journal (not uploaded)"].note == t("migration.sage50_absent_purchases")
    assert rows["Cash Disbursements Journal (not uploaded)"].note == t("migration.sage50_absent_payments")
    assert rows["Purchase Journal (not uploaded)"].count == 0
    assert "Sales Journal (not uploaded)" not in rows


def test_a_journal_naming_customers_needs_the_customer_list_and_aged_report(tmp_path):
    message = _refusal(tmp_path, _without("CUSTOMER.CSV", "Aged Receivables.xlsx"))
    assert "Customer List is missing" in message and "Aged Receivables is missing" in message
    assert "SALES.CSV (Sales Journal)" in message


# ── What is carried ──────────────────────────────────────────────────────────

@cache
def _manifest():
    import tempfile
    from pathlib import Path

    return _adapter().build_manifest(_set(Path(tempfile.mkdtemp()), _files()), USD)


def test_sage50_masters_carry_into_cif():
    bundle = _manifest().bundle
    assert bundle.company.name == build.COMPANY and bundle.company.base_currency == "USD"
    accounts = {a.code: a for a in bundle.accounts}
    assert set(accounts) == {code for code, *_ in build.CHART}
    assert [accounts[c].control for c in ("1010", "1020", "1100", "1200", "2000", "2310", "3900")] == [
        "bank", "bank", "receivable", "inventory", "payable", "tax", "retained_earnings"]
    assert [accounts[c].account_type for c in ("1550", "2500", "3000", "4900", "5050", "6400")] == [
        "asset", "liability", "equity", "revenue", "cogs", "expense"]
    contacts = {c.name: c for c in bundle.contacts}
    # Two customers share a name: each keeps its own balance, told apart by its Sage 50 ID.
    assert {"Selam Cafe (SELA01)", "Selam Cafe (SELA02)", "Café Lumière"} <= set(contacts)
    assert contacts["Alem Stationery"].roles == ["customer"]
    assert contacts["Merkato Paper Supply"].roles == ["supplier"]
    assert contacts["Merkato Paper Supply"].tax_id == "0012345678"
    items = {i.sku: i for i in bundle.items}
    assert set(items) == {code for code, *_ in build.ITEMS}
    assert items["SUP-TONER"].description == 'Toner "XL" cartridge, black'
    assert items["SRV-PRINT"].description == "Printing service,\nper 100 pages"
    assert [(tc.name, tc.rate_percent, tc.account_external_id) for tc in bundle.tax_codes] == [
        ("VAT15", Decimal("15.0"), "2310")]


def test_sage50_documents_carry_with_their_lines_tax_and_settlements():
    manifest = _manifest()
    bundle = manifest.bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    source = {x.reference: x for x in _books().transactions if x.journal in ("sales", "purchases")}
    assert len(docs) == len(source)
    first = docs["S:10001"]
    assert (first.doc_type, first.contact_external_id, first.total, first.tax_total) == (
        "invoice", "Alem Stationery", Decimal("517.50"), Decimal("67.50"))
    assert [(ln.item_external_id, ln.quantity, ln.total_price, [x.amount for x in ln.taxes])
            for ln in first.line_items] == [("SUP-PAPER", 1, Decimal("450.00"), [Decimal("67.50")])]
    # The agency's tax line is spread over the lines it was charged on, adding up to it.
    for doc in docs.values():
        assert sum((x.amount for ln in doc.line_items for x in ln.taxes), Decimal(0)) == doc.tax_total
    # Customers charged no VAT have none.
    assert all(d.tax_total == 0 for d in docs.values() if d.contact_external_id == "Bethel School")
    notes = [d for d in docs.values() if d.doc_type == "credit_note"]
    assert notes and all(n.metadata["applications"] == {f"S:{source[n.ref].apply_to}": str(n.total)} for n in notes)
    assert {d.doc_type for d in docs.values()} == {"invoice", "credit_note", "bill", "debit_note"}
    # One receipt pays two invoices of a customer; each is settled by what was applied to it.
    assert any(len(s.allocations) == 2 for s in bundle.settlements if s.settlement_type == "receipt")
    assert any(len(s.allocations) == 2 for s in bundle.settlements if s.settlement_type == "payment")
    coverage = {r.source_type: r for r in manifest.coverage}
    assert coverage["Employee"].coverage_class == CoverageClass.UNSUPPORTED_NONFINANCIAL
    assert coverage["Employee"].note == t("migration.sage50_employee_note")
    assert (coverage["Stock item quantity on hand"].count, coverage["Stock item quantity on hand"].note) == (
        1, t("migration.sage50_stock_note"))
    # A discount taken on a receipt is booked by Celerp's rules, the rest as a fallback on it.
    assert coverage["Receipt (journal fallback)"].coverage_class == CoverageClass.MAPPED_WITH_LOSS
    assert all(r.coverage_class != CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER for r in manifest.coverage)


def test_the_beginning_balances_are_one_journal_on_the_opening_trial_balance_date():
    journals = {j.source_external_id: j for j in _manifest().bundle.journals}
    opening = journals["J:opening"]
    assert opening.entry_date == build.OPENED
    assert {ln.account_external_id: ln.debit - ln.credit for ln in opening.lines} == {
        code: value for code, value in build.OPENING.items() if value}
    assert sum((t.amount for t in _manifest().bundle.bank_transfers), Decimal(0)) == Decimal("700000.00")


def test_sage50_expectations_come_from_the_reports():
    import tempfile
    from pathlib import Path

    expectations = _adapter().source_expectations(_set(Path(tempfile.mkdtemp()), _files()), USD).expectations
    by = {(e.measure, e.key): e.expected for e in expectations}
    books = _books()
    end = books.balances(build.END)
    assert by[(M.TRIAL_BALANCE, "1020")] == end["1020"]
    assert by[(M.BANK_CASH, "1030")] == end["1030"]
    # Profit and loss from the first record: the trial balance's fiscal year to date and every earlier year.
    assert by[(M.TRIAL_BALANCE_AT, "6400@2024-06-30")] == books.balances(date(2024, 6, 30))["6400"]
    assert by[(M.TRIAL_BALANCE, "4000")] == end["4000"]
    assert by[(M.TRIAL_BALANCE, "3900")] == build.OPENING["3900"]
    assert by[(M.AR_BY_CUSTOMER, "Alem Stationery")] == books.owed(True)["ALEM01"]
    assert by[(M.AP_BY_SUPPLIER, "Merkato Paper Supply")] == -books.owed(False)["V100"]


def test_an_import_is_built_only_in_a_chosen_currency(tmp_path):
    files = _set(tmp_path, _files())
    with pytest.raises(ScanError, match="Choose the currency"):
        _adapter().build_manifest(files, MigrationDecisions(mode=CIFMode.FULL_HISTORY))
    assert _adapter().build_manifest(files, MigrationDecisions(
        mode=CIFMode.FULL_HISTORY, mappings={"currency": "EUR"})).bundle.company.base_currency == "EUR"


def _signed(sign: str) -> list[tuple[str, bytes]]:
    """The trial balances printing `sign` where they print Br, without the other reports."""
    return [(n, d.replace(b"Br ", sign.encode() + (b" " if sign else b"")) if n.endswith(".csv") else d)
            for n, d in _without("Aged Receivables.xlsx", "Aged Payables.xlsx", "SALES.CSV", "PURCHASE.CSV",
                                 "CASHRCPT.CSV", "PAYMENTS.CSV", "CUSTOMER.CSV", "VENDOR.CSV")]


@pytest.mark.parametrize("sign, suggested", [("$", "USD"), ("€", "EUR"), ("", None)])
def test_the_currency_the_reports_print_is_suggested_and_none_when_they_print_none(tmp_path, sign, suggested):
    scan = _adapter().inspect(_set(tmp_path, _signed(sign)))
    assert scan.questions[0].suggested == suggested and scan.base_currency == suggested


@pytest.mark.parametrize("sign", ["Br", "ETB", "ብር"])
def test_a_birr_sign_suggests_birr_once_celerp_keeps_books_in_it(tmp_path, monkeypatch, sign):
    from celerp.services import currencies

    assert _adapter().inspect(_set(tmp_path, _signed(sign))).questions[0].suggested is None
    monkeypatch.setattr(currencies, "CURRENCIES", (*currencies.CURRENCIES, "ETB"))
    monkeypatch.setattr(currencies, "CURRENCY_CODES", frozenset(currencies.CURRENCIES))
    scan = _adapter().inspect(_set(tmp_path, _signed(sign)))
    assert scan.questions[0].suggested == "ETB" and "ETB" in scan.questions[0].options


# ── Hostile and damaged input ─────────────────────────────────────────────────

AMOUNT = "-450.00,,,1"   # the first invoice line's amount, as SALES.CSV writes it


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-inf", "1e999999", "=1+1", "12..5"])
def test_an_amount_that_is_not_a_number_refuses_the_set_by_name(tmp_path, value):
    message = _refusal(tmp_path, _edited("SALES.CSV", AMOUNT, f'"{value}",,,1'))
    assert message == t("migration.err_export_number", value=value, where="SALES.CSV ALEM01 10001 07/02/23")


@pytest.mark.parametrize("value", ["13/45/23", "07-02", "2023/07/02x"])
def test_a_date_that_is_not_one_refuses_the_set_by_name(tmp_path, value):
    message = _refusal(tmp_path, _edited("GENERAL.CSV", "07/31/23", value, count=2))
    assert f'"{value}" is not a date' in message


def test_a_transaction_that_does_not_balance_is_refused(tmp_path):
    message = _refusal(tmp_path, _edited("SALES.CSV", AMOUNT, "-451.00,,,1"))
    assert message == t("migration.err_sage50_unbalanced", where="SALES.CSV ALEM01 10001 07/02/23",
                        difference="1.00", label="Sales Journal")


def test_a_transaction_short_of_its_lines_is_refused(tmp_path):
    message = _refusal(tmp_path, _edited("GENERAL.CSV", "07/31/23,DEP-202307,,2,", "07/31/23,DEP-202307,,3,", 2))
    assert message == t("migration.err_sage50_distributions", where="GENERAL.CSV 07/31/23 DEP-202307", count=3,
                        label="General Journal")


@pytest.mark.parametrize("count", ["two", "0", "1.5", "9999"])
def test_a_number_of_lines_that_is_not_one_is_refused(tmp_path, count):
    message = _refusal(tmp_path, _edited("GENERAL.CSV", "07/31/23,DEP-202307,,2,", f"07/31/23,DEP-202307,,{count},", 2))
    assert message == t("migration.err_export_number", value=count, where="GENERAL.CSV 07/31/23 DEP-202307")


def test_a_line_on_an_account_the_chart_does_not_list_is_refused(tmp_path):
    message = _refusal(tmp_path, _edited("GENERAL.CSV", ",6300,Depreciation", ",6399,Depreciation"))
    assert message == t("migration.err_export_account_missing", accounts="6399")


def test_a_customer_the_list_does_not_hold_is_refused(tmp_path):
    message = _refusal(tmp_path, _edited("CUSTOMER.CSV", "TENA01,Tena Clinic", "TENA09,Tena Clinic"))
    assert message == " ".join(t("migration.err_sage50_contact_missing", label=label, ids="TENA01",
                                 list="Customer List") for label in ("Cash Receipts Journal", "Sales Journal"))


def test_an_account_type_sage50_does_not_have_is_refused(tmp_path):
    message = _refusal(tmp_path, _edited("CHART.CSV", "Cash on Hand,FALSE,0", "Cash on Hand,FALSE,99"))
    assert message == t("migration.err_export_account_type", account="1010 Cash on Hand", type="99")


def test_a_formula_looking_name_is_kept_as_text_and_never_evaluated(tmp_path):
    files = _edited("CUSTOMER.CSV", "TENA01,Tena Clinic", 'TENA01,"=HYPERLINK(""http://x"",""y"")"')
    names = {c.name for c in _adapter().build_manifest(_set(tmp_path, files), USD).bundle.contacts}
    assert '=HYPERLINK("http://x","y")' in names


def test_text_that_is_neither_utf8_nor_windows_1252_is_named_unreadable(tmp_path):
    files = [(n, d if n != "VENDOR.CSV" else d + b"\x81\x8d\x8f\x90\x9d") for n, d in _files()]
    assert "VENDOR.CSV could not be read. Export it again and upload the new file." in _refusal(tmp_path, files)


def test_a_damaged_report_workbook_is_named_unreadable(tmp_path):
    files = [(n, d if n != "Aged Payables.xlsx" else _broken_workbook()) for n, d in _files()]
    assert t("migration.err_export_unreadable", name="Aged Payables.xlsx") in _refusal(tmp_path, files)


def test_a_cell_longer_than_the_reader_takes_is_named_unreadable(tmp_path):
    files = _edited("GENERAL.CSV", "Depreciation", "D" * (1024 * 1024))
    assert "GENERAL.CSV could not be read" in _refusal(tmp_path, files)


def test_a_trial_balance_short_of_the_last_transaction_is_refused(tmp_path):
    files = _without(f"Trial Balance {build.END.isoformat()}.csv")
    assert _refusal(tmp_path, files) == t("migration.err_sage50_tb_last", label="General Ledger Trial Balance",
                                          date="31 Mar 2025")


def test_any_value_the_reader_cannot_convert_ends_as_the_damaged_refusal(tmp_path, monkeypatch):
    from celerp.importers.adapters.exports import sage50

    def broken(*args, **kwargs):
        raise TypeError("unexpected value")

    monkeypatch.setattr(sage50._Reader, "read_items", broken)
    assert _refusal(tmp_path, _files()) == t("migration.err_export_damaged", source="Sage 50 / Peachtree")


# ── Through the routes ───────────────────────────────────────────────────────

def _route_cases() -> dict[str, tuple[list[tuple[str, bytes]], str]]:
    where = "SALES.CSV ALEM01 10001 07/02/23"
    return {
        "nan": (_edited("SALES.CSV", AMOUNT, '"NaN",,,1'), t("migration.err_export_number", value="NaN", where=where)),
        "exponent": (_edited("SALES.CSV", AMOUNT, '"1e999999",,,1'),
                     t("migration.err_export_number", value="1e999999", where=where)),
        "date": (_edited("GENERAL.CSV", "07/31/23", "13/45/23", 2), '"13/45/23" is not a date'),
        "unbalanced": (_edited("SALES.CSV", AMOUNT, "-451.00,,,1"),
                       t("migration.err_sage50_unbalanced", where=where, difference="1.00", label="Sales Journal")),
        "account": (_edited("GENERAL.CSV", ",6300,Depreciation", ",6399,Depreciation"),
                    t("migration.err_export_account_missing", accounts="6399")),
        "customer": (_edited("CUSTOMER.CSV", "TENA01,Tena Clinic", "TENA09,Tena Clinic"),
                     t("migration.err_sage50_contact_missing", label="Sales Journal", ids="TENA01",
                       list="Customer List")),
        "encoding": ([(n, d if n != "VENDOR.CSV" else d + b"\x81\x8d\x8f\x90\x9d") for n, d in _files()],
                     t("migration.err_export_unreadable", name="sage50.zip: VENDOR.CSV")),
        "workbook": ([(n, d if n != "Aged Payables.xlsx" else _broken_workbook()) for n, d in _files()],
                     t("migration.err_export_unreadable", name="sage50.zip: Aged Payables.xlsx")),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_route_cases()))
@pytest.mark.parametrize("source", [None, "sage50"])
async def test_each_damaged_set_posted_to_the_scan_route_is_refused_by_name(client, monkeypatch, tmp_path, case,
                                                                           source):
    """A damaged export set is never a server error. A set Celerp recognises is kept as a draft
    whose problems give the refusal, so the user can replace the one file; nothing is imported."""
    from celerp.config import settings
    from migration_support import scan_upload

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    files, refusal = _route_cases()[case]
    r = await scan_upload(client, _zip(files), name="sage50.zip", source=source)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "scan_token" not in body
    assert body["checklist"]["complete"] is False
    assert any(refusal in problem for problem in body["checklist"]["problems"]), body["checklist"]["problems"]


@pytest.mark.asyncio
async def test_a_file_that_is_no_sage50_export_is_refused_with_422_when_sage50_is_chosen(client, monkeypatch, tmp_path):
    from celerp.config import settings
    from migration_support import scan_upload

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    r = await scan_upload(client, b"just some notes\n", name="notes.csv", source="sage50")
    assert r.status_code == 422, r.text
    assert "is not a Sage 50 / Peachtree file" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_currency_that_is_not_one_of_the_choices_is_refused_with_422(client, monkeypatch, tmp_path):
    from celerp.config import settings
    from migration_support import save_decisions, scan_upload

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    r = await scan_upload(client, _zip(_files()), name="sage50.zip", source="sage50")
    assert r.status_code == 200 and "scan_token" in r.json(), r.text
    r = await save_decisions(client, r.json()["scan_token"], mappings={"currency": "Birr"})
    assert r.status_code == 422, r.text
    assert "currency" in r.json()["detail"]


# ── End to end ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.timeout(180)  # twenty-one months of trading through the real runner
@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": CUTOVER.isoformat()}])
async def test_a_sage50_set_migrates_with_every_check_passing(real_engine, monkeypatch, tmp_path, decisions):
    """The set migrates through the real runner with nothing rejected: the ledger ties out
    against every exported trial balance, and open receivables and payables against the aged
    reports, per customer, vendor and document."""
    run, rejected = await migrate_set(real_engine, list(_files()), {**decisions, "mappings": {"currency": "USD"}},
                                      monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    measures = {r["check"] for r in run.reconciliation["rows"]}
    assert {"trial_balance_at", "ar_by_customer", "ap_by_supplier", "bank_cash", "document_residual"} <= measures
