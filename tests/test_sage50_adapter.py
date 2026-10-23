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

ETB = MigrationDecisions(mode=CIFMode.FULL_HISTORY, mappings={"currency": "ETB"})
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
    # The exports state no currency: the user chooses one Celerp keeps books in, and the Br the
    # reports print suggests Birr.
    from celerp.services.currencies import CURRENCIES
    assert scan.currencies == ("ETB",) and scan.base_currency == "ETB"
    [question] = scan.questions
    assert (question.key, question.suggested, question.options) == ("currency", "ETB", CURRENCIES)
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


def test_other_column_names_filled_applied_lines_and_four_digit_years_read_the_same(tmp_path):
    """Other releases and templates head the tax agency, invoice and check number columns
    differently and fill the G/L Account of a line applied to an invoice; a set may print
    four-digit years."""
    old = _adapter().build_manifest(_set(tmp_path, build.files(_books(), alternate=True, four=True).items()), ETB)
    new = _adapter().build_manifest(_set(tmp_path, _files()), ETB)
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

    return _adapter().build_manifest(_set(Path(tempfile.mkdtemp()), _files()), ETB)


def test_sage50_masters_carry_into_cif():
    bundle = _manifest().bundle
    assert bundle.company.name == build.COMPANY and bundle.company.base_currency == "ETB"
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
    # An invoice selling goods (Tax Type 1) and services (Tax Type 2) keeps its VAT as one tax line:
    # which tax types are taxable is not exported, so no line is given a share it may not owe.
    mixed = [d for d in docs.values() if d.doc_type == "invoice" and d.tax_total
             and len({ln.account_external_id for ln in d.line_items if ln.total_price}) > 1]
    assert mixed and all([ln.taxes for ln in d.line_items if ln.total_price] == [[]] * sum(
        1 for ln in d.line_items if ln.total_price) for d in mixed)
    assert all(sum(1 for ln in d.line_items if not ln.total_price and ln.taxes) == 1 for d in mixed)
    assert (coverage["Invoice with its sales tax on one line"].count,
            coverage["Invoice with its sales tax on one line"].coverage_class,
            coverage["Invoice with its sales tax on one line"].note) == (
        len(mixed), CoverageClass.MAPPED_WITH_LOSS, t("migration.sage50_tax_unassigned_note"))


# ── Layouts as Sage 50's field definitions name them ─────────────────────────
#
# Written here by hand from the field names Sage 50's "Import/Export Field Definition Lists"
# give (Sales Journal: Sales Tax Agency, Tax Type 0 for a tax distribution; Purchases Journal:
# Invoice #, Apply to Invoice #; Cash Receipts and Payments Journals: a line applied to an
# invoice leaves its G/L Account empty), not from the fixture above, so the reader is checked
# against the documented layout rather than against a writer made to match it.

PUBLISHED = {
    "CHART.CSV": "Account ID,Account Description,Inactive,Account Type\r\n"
                 "1020,Bank,FALSE,0\r\n1100,Receivables,FALSE,1\r\n2000,Payables,FALSE,10\r\n"
                 "2310,VAT Payable,FALSE,12\r\n3000,Capital,FALSE,16\r\n3900,Retained Earnings,FALSE,18\r\n"
                 "4000,Goods Sales,FALSE,21\r\n4100,Service Sales,FALSE,21\r\n6000,Rent,FALSE,24\r\n",
    "CUSTOMER.CSV": "Customer ID,Customer Name,Inactive\r\nC1,First Customer,FALSE\r\n",
    "VENDOR.CSV": "Vendor ID,Vendor Name,Inactive\r\nV1,First Vendor,FALSE\r\n",
    "SALES.CSV": (
        "Customer ID,Customer Name,Invoice/CM #,Apply to Invoice Number,Credit Memo,Date,Date Due,"
        "Accounts Receivable Account,Sales Tax ID,Number of Distributions,Invoice/CM Distribution,Quantity,"
        "Item ID,Description,G/L Account,Unit Price,Tax Type,Amount,Sales Tax Agency\r\n"
        # Goods taxable, a service exempt: 100.00 + 100.00 and 10.00 tax on the goods alone. Every
        # distribution names the invoice's tax agency; only Tax Type 0 is the tax.
        "C1,First Customer,1001,,FALSE,01/10/25,02/09/25,1100,ST,3,1,1,,Goods,4000,100.00,1,-100.00,AGENCY\r\n"
        "C1,First Customer,1001,,FALSE,01/10/25,02/09/25,1100,ST,3,2,1,,Service,4100,100.00,2,-100.00,AGENCY\r\n"
        "C1,First Customer,1001,,FALSE,01/10/25,02/09/25,1100,ST,3,0,,,Sales tax,2310,,0,-10.00,AGENCY\r\n"
        # Two goods lines of one tax type: the tax is theirs, in proportion.
        "C1,First Customer,1002,,FALSE,01/12/25,02/11/25,1100,ST,3,1,1,,Goods,4000,300.00,1,-300.00,AGENCY\r\n"
        "C1,First Customer,1002,,FALSE,01/12/25,02/11/25,1100,ST,3,2,1,,Goods,4000,100.00,1,-100.00,\r\n"
        "C1,First Customer,1002,,FALSE,01/12/25,02/11/25,1100,ST,3,0,,,Sales tax,2310,,0,-40.00,AGENCY\r\n"),
    "RECEIPTS.CSV": (
        "Customer ID,Customer Name,Reference,Receipt Number,Date,Cash Account,Cash Amount,Number of Distributions,"
        "Invoice Paid,Description,G/L Account,Amount\r\n"
        "C1,First Customer,R1,R1,01/20/25,1020,210.00,1,1001,Payment,,-210.00\r\n"),
    "PURCHASE.CSV": (
        "Vendor ID,Vendor Name,Invoice #,Apply to Invoice #,Credit Memo,Date,Date Due,Accounts Payable Account,"
        "Number of Distributions,Invoice/CM Distribution,Quantity,Item ID,Description,G/L Account,Unit Price,Amount\r\n"
        "V1,First Vendor,B-1,,FALSE,01/05/25,02/04/25,2000,1,1,,,January rent,6000,,500.00\r\n"),
    "PAYMENTS.CSV": (
        "Vendor ID,Vendor Name,Check Number,Date,Cash Account,Cash Amount,Number of Distributions,Invoice Paid,"
        "Description,G/L Account,Amount\r\n"
        "V1,First Vendor,101,01/25/25,1020,-500.00,1,B-1,Rent,,500.00\r\n"),
    "TB.CSV": (
        "First Company\r\nGeneral Ledger Trial Balance\r\n\"As of Jan 31, 2025\"\r\n\r\n"
        "Account ID,Account Description,Debit Amt,Credit Amt\r\n"
        "1020,Bank,\"Br 9,710.00\",\r\n1100,Receivables,440.00,\r\n2310,VAT Payable,,50.00\r\n"
        "3000,Capital,,\"10,000.00\"\r\n4000,Goods Sales,,500.00\r\n4100,Service Sales,,100.00\r\n"
        "6000,Rent,500.00,\r\n"),
    "TB-OPENING.CSV": (
        "First Company\r\nGeneral Ledger Trial Balance\r\n\"As of Dec 31, 2024\"\r\n\r\n"
        "Account ID,Account Description,Debit Amt,Credit Amt\r\n1020,Bank,\"10,000.00\",\r\n"
        "3000,Capital,,\"10,000.00\"\r\n"),
    "AR.CSV": (
        "First Company\r\nAged Receivables\r\n\"As of Jan 31, 2025\"\r\n\r\n"
        "Customer ID,Customer,Invoice/CM #,Date,Amount Due\r\nC1,First Customer,1002,01/12/25,440.00\r\n"),
    "AP.CSV": (
        "First Company\r\nAged Payables\r\n\"As of Jan 31, 2025\"\r\n\r\nVendor ID,Vendor,Invoice/CM #,Date,Amount Due\r\n"),
}


@cache
def _published():
    import tempfile
    from pathlib import Path

    files = _set(Path(tempfile.mkdtemp()), [(n, d.encode("cp1252")) for n, d in PUBLISHED.items()])
    adapter = _adapter()
    return adapter.inspect(files), adapter.build_manifest(files, ETB), adapter.source_expectations(files, ETB)


def test_the_documented_sales_tax_agency_heading_keeps_sales_tax():
    _, manifest, _ = _published()
    docs = {d.ref: d for d in manifest.bundle.documents}
    assert (docs["1001"].tax_total, docs["1002"].tax_total) == (Decimal("10.00"), Decimal("40.00"))
    # A sold line naming the agency stays a sold line on its revenue account: only Tax Type 0 is tax,
    # so Celerp books each invoice as Sage 50 did, with no fallback journal to make up a difference.
    assert [(ln.account_external_id, ln.total_price) for ln in docs["1001"].line_items if ln.total_price] == [
        ("4000", Decimal("100.00")), ("4100", Decimal("100.00"))]
    assert docs["1001"].total == Decimal("210.00")
    assert not [j for j in manifest.bundle.journals if j.fallback]
    assert [(tc.name, tc.account_external_id) for tc in manifest.bundle.tax_codes] == [("AGENCY", "2310")]
    assert {a.code: a.control for a in manifest.bundle.accounts}["2310"] == "tax"


def test_a_sales_journal_without_tax_types_is_refused_rather_than_guessing_tax_from_freight(tmp_path):
    """Sage 50 writes freight and sales tax both as Invoice/CM Distribution 0; only the Tax Type
    tells them apart, so a Sales Journal exported without it is refused by name."""
    from celerp.importers.adapters.exports import sage50

    sales = (
        "Customer ID,Customer Name,Invoice/CM #,Credit Memo,Date,Accounts Receivable Account,"
        "Number of Distributions,Invoice/CM Distribution,Description,G/L Account,Amount,Sales Tax Agency\r\n"
        "C1,First Customer,1001,FALSE,01/10/25,1100,3,1,Goods,4000,-100.00,AGENCY\r\n"
        "C1,First Customer,1001,FALSE,01/10/25,1100,3,0,Freight,4100,-15.00,AGENCY\r\n"
        "C1,First Customer,1001,FALSE,01/10/25,1100,3,0,Sales tax,2310,-10.00,AGENCY\r\n")
    files = [(n, (sales if n == "SALES.CSV" else d).encode("cp1252")) for n, d in PUBLISHED.items()]
    message = _refusal(tmp_path, files)
    assert t("migration.err_export_lacks_column", name="SALES.CSV", label="Sales Journal", columns="tax type",
             menu=sage50.SALES.menu) in message


def test_taxable_and_exempt_lines_keep_their_tax_unassigned_and_one_tax_type_shares_it():
    scan, manifest, _ = _published()
    docs = {d.ref: d for d in manifest.bundle.documents}
    # 100.00 taxable and 100.00 exempt with 10.00 tax: never 5.00 on each, nor a 5% rate.
    assert [(ln.total_price, [x.amount for x in ln.taxes]) for ln in docs["1001"].line_items] == [
        (Decimal("100.00"), []), (Decimal("100.00"), []), (Decimal("0"), [Decimal("10.00")])]
    assert [(ln.total_price, [x.amount for x in ln.taxes]) for ln in docs["1002"].line_items] == [
        (Decimal("300.00"), [Decimal("30.00")]), (Decimal("100.00"), [Decimal("10.00")])]
    assert [tc.rate_percent for tc in manifest.bundle.tax_codes] == [Decimal("10.0")]
    assert {r.source_type: r.count for r in scan.coverage}["Invoice with its sales tax on one line"] == 1


def test_receipts_and_payments_applied_with_an_empty_g_l_account_settle_their_documents():
    _, manifest, expectations = _published()
    settled = {(s.settlement_type, s.bank_account_external_id, tuple((a.document_external_id, a.amount)
                                                                     for a in s.allocations))
               for s in manifest.bundle.settlements}
    assert settled == {("receipt", "1020", (("S:1001", Decimal("210.00")),)),
                       ("payment", "1020", (("P:B-1", Decimal("500.00")),))}
    by = {(e.measure, e.key): e.expected for e in expectations.expectations}
    assert by[(M.DOCUMENT_RESIDUAL, "S:1002")] == Decimal("440.00")
    assert by[(M.AR_BY_CUSTOMER, "First Customer")] == Decimal("440.00")


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

    expectations = _adapter().source_expectations(_set(Path(tempfile.mkdtemp()), _files()), ETB).expectations
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
                                 "RECEIPTS.CSV", "PAYMENTS.CSV", "CUSTOMER.CSV", "VENDOR.CSV")]


@pytest.mark.parametrize("sign, suggested", [("Br", "ETB"), ("ETB", "ETB"), ("ብር", "ETB"), ("$", "USD"),
                                             ("€", "EUR"), ("", None)])
def test_the_currency_the_reports_print_is_suggested_and_none_when_they_print_none(tmp_path, sign, suggested):
    scan = _adapter().inspect(_set(tmp_path, _signed(sign)))
    assert scan.questions[0].suggested == suggested and scan.base_currency == suggested


# ── Hostile and damaged input ─────────────────────────────────────────────────

AMOUNT = "-450.00,VAT15,,1"   # the first invoice line's amount, as SALES.CSV writes it


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-inf", "1e999999", "=1+1", "12..5"])
def test_an_amount_that_is_not_a_number_refuses_the_set_by_name(tmp_path, value):
    message = _refusal(tmp_path, _edited("SALES.CSV", AMOUNT, f'"{value}",VAT15,,1'))
    assert message == t("migration.err_export_number", value=value, where="SALES.CSV ALEM01 10001 07/02/23")


@pytest.mark.parametrize("value", ["13/45/23", "07-02", "2023/07/02x"])
def test_a_date_that_is_not_one_refuses_the_set_by_name(tmp_path, value):
    message = _refusal(tmp_path, _edited("GENERAL.CSV", "07/31/23", value, count=2))
    assert f'"{value}" is not a date' in message


def test_a_transaction_that_does_not_balance_is_refused(tmp_path):
    message = _refusal(tmp_path, _edited("SALES.CSV", AMOUNT, "-451.00,VAT15,,1"))
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
    names = {c.name for c in _adapter().build_manifest(_set(tmp_path, files), ETB).bundle.contacts}
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
        "nan": (_edited("SALES.CSV", AMOUNT, '"NaN",VAT15,,1'), t("migration.err_export_number", value="NaN", where=where)),
        "exponent": (_edited("SALES.CSV", AMOUNT, '"1e999999",VAT15,,1'),
                     t("migration.err_export_number", value="1e999999", where=where)),
        "date": (_edited("GENERAL.CSV", "07/31/23", "13/45/23", 2), '"13/45/23" is not a date'),
        "unbalanced": (_edited("SALES.CSV", AMOUNT, "-451.00,VAT15,,1"),
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
    run, rejected = await migrate_set(real_engine, list(_files()), {**decisions, "mappings": {"currency": "ETB"}},
                                      monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    measures = {r["check"] for r in run.reconciliation["rows"]}
    assert {"trial_balance_at", "ar_by_customer", "ap_by_supplier", "bank_cash", "document_residual"} <= measures
