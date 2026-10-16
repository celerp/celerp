# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager books that start from starting balances, and the accounts Manager keeps a
balance for under a control account, migrate in Full history.

Capital accounts (and their sub-accounts), special accounts, employees, expense claims
payers, fixed and intangible assets and investments each arrive as a Celerp account under
the account Manager reports them in. Starting balances arrive as one opening journal,
offset to retained earnings the way Manager's own balance sheet does, dated the day before
the first dated record. Figures are worked out in specs.starting_balance_objects()."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.manager_io import specs
from fixtures.manager_io.support import CHECKPOINTS, STARTING, adapter, artifact, ref
from migration_support import real_client, real_engine  # noqa: F401 - fixtures
from test_migration_inventory_provenance import _doc, _migrated

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=specs.STARTING_CUTOVER)
STOPPING = (CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER, CoverageClass.UNCLASSIFIED)
_FULL = CHECKPOINTS["starting"]["full_history"]

SUB = "CAP:DRAW"


def _key(label: str) -> str:
    """The source id of a checkpoint label; after a colon, a sub-account label or the
    suffix of a written-off account."""
    holder, _, part = label.partition(":")
    if not part:
        return ref(label)
    return f"{ref(holder)}:{ref(part) if part.isupper() else part}"


def _figures(section: dict) -> dict[str, D]:
    return {_key(label): D(amount) for label, amount in section.items()}


# Debit positive, after every record in the book.
BALANCES = _figures(_FULL["trial_balance"])


def _source(tmp_path, extra: tuple = (), objects: list | None = None, name: str = "starting.manager"):
    if objects is None and not extra:
        return artifact(STARTING)
    path = tmp_path / name
    if objects is None:
        specs.build_starting_balances(path, extra)
    else:
        specs.write_manager_file(path, objects)
    return artifact(path)


def _rows(source) -> dict:
    return {r.source_type: r for r in adapter().inspect([source]).coverage}


def _replaced(label: str, changes: dict) -> list:
    return [dataclasses.replace(o, fields={**o.fields, **changes}) if o.key == specs.k(label) else o
            for o in specs.starting_balance_objects()]


def _trial_balance(source, decisions=FULL) -> dict[str, D]:
    rows = adapter().source_expectations([source], decisions).expectations
    return {r.key: D(r.expected) for r in rows if r.measure == M.TRIAL_BALANCE}


def test_starting_balances_and_holders_scan_without_blockers(tmp_path):
    """RED before the change: every starting balance type, every holder type and the
    custom control accounts stop Full history as unsupported."""
    rows = _rows(_source(tmp_path))
    assert {label: r.note for label, r in rows.items() if r.coverage_class in STOPPING} == {}
    for label in ("CapitalAccount", "SubAccount", "SpecialAccount", "Employee", "ExpenseClaimsPayer", "FixedAsset",
                  "IntangibleAsset", "Investment", "ControlAccountForCapitalAccounts",
                  "ControlAccountForBankAccounts", "BalanceSheetAccountStartingBalance",
                  "BankOrCashAccountStartingBalance", "CapitalAccountStartingBalance",
                  "SpecialAccountStartingBalance", "EmployeeStartingBalance", "FixedAssetStartingBalance",
                  "IntangibleAssetStartingBalance", "InventoryItemStartingBalance", "InvestmentStartingBalance",
                  "SalesInvoiceStartingBalance"):
        assert rows[label].coverage_class in (CoverageClass.MAPPED, CoverageClass.MAPPED_WITH_LOSS), label


def test_holders_arrive_as_accounts_under_their_control_account(tmp_path):
    """RED before the change: the scan stops, and no holder becomes an account."""
    bundle = adapter().build_manifest([_source(tmp_path)], FULL).bundle
    accounts = {a.source_external_id: a for a in bundle.accounts}
    expected = {
        # label: (name, code, type, parent)
        "CAP": ("Owner capital", "3001", "equity", "@BalanceSheetCapitalAccountsAccount"),
        SUB: ("Owner capital - Drawings", None, "equity", "@BalanceSheetCapitalAccountsAccount"),
        "CAP2": ("Second partner", "3101", "equity", "PCTRL"),
        "PCTRL": ("Partner capital", "3100", "equity", None),
        "SPEC": ("Director loan", "2601", "asset", "@BalanceSheetSpecialAccountsAccount"),
        "EMP": ("Staff One", "E1", "liability", "@BalanceSheetEmployeeClearingAccount"),
        "ECP": ("Owner", None, "liability", "@BalanceSheetExpenseClaimsAccount"),
        "VAN": ("Van", "FA1", "asset", "@BalanceSheetFixedAssetsAtCostAccount"),
        "VAN:depreciation": ("Van - accumulated depreciation", None, "asset",
                             "@BalanceSheetFixedAssetsAccumulatedDepreciationAccount"),
        "PAT": ("Patent", "IA1", "asset", "@BalanceSheetIntangibleAssetsAtCostAccount"),
        "PAT:amortization": ("Patent - accumulated amortization", None, "asset",
                             "@BalanceSheetIntangibleAssetsAccumulatedAmortizationAccount"),
        "FUND": ("Index fund", "INV1", "asset", "@BalanceSheetInvestmentsAccount"),
        "OPB": ("Operating bank", "1010", "asset", "BCTRL"),
        "BCTRL": ("Bank accounts", "1000", "asset", None),
        "@BalanceSheetCapitalAccountsAccount": ("Capital accounts", None, "equity", None),
        "@BalanceSheetEmployeeClearingAccount": ("Employee clearing account", None, "liability", None),
    }
    actual = {label: (a.name, a.code, a.account_type.value,
                      a.parent_external_id)
              for label in expected for a in [accounts[_key(label)]]}
    assert actual == {label: (n, c, t, _key(p) if p else None) for label, (n, c, t, p) in expected.items()}
    # A holder's built-in account is carried only when a holder of its kind is.
    assert str(specs.T["BalanceSheetCapitalAccountsAccount"]) in accounts
    plain = adapter().build_manifest([artifact(specs.build_basic(tmp_path / "basic.manager"))], FULL).bundle
    assert str(specs.T["BalanceSheetCapitalAccountsAccount"]) not in {a.source_external_id for a in plain.accounts}


def test_starting_balances_post_one_opening_journal_against_retained_earnings(tmp_path):
    """RED before the change: the scan stops on the starting balances."""
    bundle = adapter().build_manifest([_source(tmp_path)], FULL).bundle
    (opening,) = [j for j in bundle.journals if j.source_type == "StartingBalances"]
    # The day before the first dated record, invoice INV-0 of 2025-12-20.
    assert opening.entry_date == date.fromisoformat(_FULL["starting_date"])
    net: dict[str, D] = {}
    for line in opening.lines:
        net[line.account_external_id] = net.get(line.account_external_id, D(0)) + line.debit - line.credit
    assert net == _figures(_FULL["starting_journal"])
    for control, contact in (("@BalanceSheetAccountsReceivableAccount", "CA"),
                             ("@BalanceSheetAccountsPayableAccount", "SA")):
        line = next(ln for ln in opening.lines if ln.account_external_id == ref(control))
        assert line.contact_external_id == ref(contact)
    # A document's starting balance is what was paid on it before the books began.
    documents = {d.source_external_id: d for d in bundle.documents}
    assert {key: D(documents[key].metadata["paid_at_start"]) for key in _figures(_FULL["starting_paid"])} == \
        _figures(_FULL["starting_paid"])
    invoice = documents[ref("INVSB")]
    assert (invoice.amount_paid, invoice.amount_outstanding, invoice.status) == (D("100"), D("0"), "paid")
    bill = documents[ref("BILLSB")]
    assert (bill.amount_paid, bill.amount_outstanding, bill.status) == (D("40"), D("0"), "paid")
    # The starting stock arrives as an opening position at the average cost.
    (stock,) = [a for a in bundle.inventory_adjustments if a.source_type == "StartingBalances"]
    assert (stock.kind, stock.item_external_id, stock.quantity, stock.value) == (
        "opening", ref("WID"), D(_FULL["inventory_quantity"]["WID"]), D(_FULL["inventory_value"]["WID"]))


def test_holder_lines_post_to_the_holder_account(tmp_path):
    """RED before the change: the scan stops on the holder fields of the lines."""
    assert _trial_balance(_source(tmp_path)).items() >= BALANCES.items()


def test_cutover_folds_starting_balances_into_the_opening(tmp_path):
    """The balances at the end are the same whichever mode: starting balances are history
    a cutover folds into its opening. RED before the change: both scans stop."""
    source = _source(tmp_path)
    assert _trial_balance(source, CUTOVER) == _trial_balance(source, FULL)
    bundle = adapter().build_manifest([source], CUTOVER).bundle
    assert not [j for j in bundle.journals if j.source_type == "StartingBalances"]


def test_holder_on_a_line_of_another_control_account_stops_the_record(tmp_path):
    """A capital account named on a line posted to the special accounts account is a
    reference Celerp cannot place. RED before the change: the field stops the scan as an
    unsupported feature rather than an unknown reference."""
    objects = [*specs.starting_balance_objects(),
               specs.obj("JournalEntry", "HJX", {1: date(2026, 1, 24), 2: "HJ-X", 14: [
                   {1: specs.SPECIAL, 11: specs.k("CAP"), 13: D("5")},
                   {1: specs.CASH_AT_BANK, 29: specs.k("OPB"), 14: D("5")}]})]
    rows = _rows(_source(tmp_path, objects=objects))
    assert rows["JournalEntry (unknown reference)"].count == 1


def test_disposed_fixed_asset_stops_the_scan(tmp_path):
    """Disposal posts a gain or loss Celerp does not rebuild yet; it is named, never dropped."""
    rows = _rows(_source(tmp_path, objects=_replaced("VAN", {6: True, 7: date(2026, 1, 25)})))
    assert rows["FixedAsset (unsupported feature)"].coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER
    assert "dispos" in rows["FixedAsset (unsupported feature)"].note


def test_foreign_currency_starting_balance_stops_the_scan(tmp_path):
    """A starting balance on a foreign currency bank has no rate to post at."""
    extra = (specs.obj("ForeignCurrency", "EUR", {1: "Euro", 2: "EUR", 4: 2}),
             specs.obj("BankOrCashAccount", "EBANK", {1: "Euro bank", 3: specs.k("EUR")}),
             specs.obj("BankOrCashAccountStartingBalance", "SBEB", {1: specs.k("EBANK"), 2: D("10")}))
    rows = _rows(_source(tmp_path, extra))
    blocked = rows["BankOrCashAccountStartingBalance (foreign currency starting balance)"]
    assert blocked.coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER


def test_starting_balances_without_a_dated_record(tmp_path):
    """With nothing dated, Full history has no day to start on and says so; a cutover
    starts the balances on its cutover date. RED before the change: both stop on the types."""
    objects = [o for o in specs.starting_balance_objects()
               if o.content_type not in (specs.T["JournalEntry"], specs.T["Receipt"], specs.T["Payment"],
                                         specs.T["SalesInvoice"], specs.T["SalesInvoiceStartingBalance"],
                                         specs.T["PurchaseInvoice"], specs.T["PurchaseInvoiceStartingBalance"])]
    source = _source(tmp_path, objects=objects)
    with pytest.raises(ScanError, match="starting balances"):
        adapter().build_manifest([source], FULL)
    trial = _trial_balance(source, CUTOVER)
    assert trial[ref("@BalanceSheetRetainedEarningsAccount")] == D("-1170")


def test_customer_control_account_merges_into_receivables(tmp_path):
    """A custom control account for customers is reported under the receivable control,
    which is where Celerp keeps every customer balance. RED before the change: it blocks."""
    objects = [*specs.starting_balance_objects(),
               specs.obj("ControlAccountForCustomers", "CCTRL", {1: "Trade debtors", 17: "1150", 3: specs.T["Assets"]})]
    objects = [dataclasses.replace(o, fields={**o.fields, 19: specs.k("CCTRL")}) if o.key == specs.k("CA") else o
               for o in objects]
    source = _source(tmp_path, objects=objects)
    rows = _rows(source)
    assert rows["Customer (custom control account)"].coverage_class == CoverageClass.MAPPED_WITH_LOSS
    assert _trial_balance(source) == _trial_balance(_source(tmp_path, name="plain.manager"))


async def test_starting_balances_migrate_end_to_end(real_engine, real_client, monkeypatch, tmp_path):
    """The migrated books hold Manager's balance sheet. RED before the change: the scan stops."""
    books = await _migrated(real_engine, monkeypatch, tmp_path, {"mode": "full_history"}, source=STARTING)
    code = {m: t for (_, m), t in books.maps.items()}
    trial = (await real_client.get("/accounting/trial-balance", headers=books.headers)).json()
    assert trial["balanced"] is True
    net = {line["code"]: D(str(line["net"])) for line in trial["lines"]}
    for key, amount in BALANCES.items():
        if amount:
            assert net[code[key]] == amount, key
    # What was paid before the books began settles the document without moving money.
    for label in ("INVSB", "BILLSB"):
        doc = await _doc(books, label)
        assert (doc["status"], D(str(doc["amount_outstanding"]))) == ("paid", D(0)), label
    sheet = (await real_client.get("/accounting/balance-sheet", headers=books.headers)).json()
    assert sheet["balanced"] is True
