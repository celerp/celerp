# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager.io source reconciliation: expectations from the source, and foreign currency records."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from celerp.importers import schema
from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io.support import (
    BASIC, CHECKPOINTS, FX, actual_rows, adapter, artifact, expected_rows, ref,
)

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)


def _refuse_cif(monkeypatch) -> None:
    """Make every CIF bundle entity impossible to construct, so any path through CIF conversion fails."""
    def refuse(self, *args, **kwargs):
        raise AssertionError(f"source expectations built a {type(self).__name__}")

    for model in (schema.CIFImportBundle, schema.CIFImportManifest, schema.CIFDocument, schema.CIFSettlement,
                  schema.CIFJournalEntry, schema.CIFBankTransfer, schema.CIFAccount, schema.CIFContact,
                  schema.CIFItem, schema.CIFInventoryAdjustment):
        monkeypatch.setattr(model, "__init__", refuse)


def test_manager_source_expectations_are_independent_of_cif(monkeypatch):
    from celerp.importers.adapters.manager_io import mappings

    manager = adapter()
    art = artifact(BASIC)
    cutover = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=date.fromisoformat(
        CHECKPOINTS["basic"]["cutover"]["cutover_date"]))

    _refuse_cif(monkeypatch)
    for name in dir(mappings):
        value = getattr(mappings, name)
        if callable(value) and getattr(value, "__module__", None) == mappings.__name__:
            monkeypatch.setattr(mappings, name, lambda *a, _name=name, **k: pytest.fail(f"mappings.{_name} was called"))

    full = manager.source_expectations([art], FULL)
    assert actual_rows(full) == expected_rows(CHECKPOINTS["basic"]["full_history"], "USD")

    at_cutover = manager.source_expectations([art], cutover)
    assert actual_rows(at_cutover) == expected_rows(CHECKPOINTS["basic"]["cutover"], "USD")

    # Every measure the source can state is present: trial balance, AR/AP control and parties, bank and
    # cash, inventory quantity and value, tax, document count/total/status and settlement allocation.
    # A business file holds no month-end trial balance reports; only export sets state those.
    assert {row.measure for row in full.expectations} == (
        set(schema.ReconciliationMeasure) - {schema.ReconciliationMeasure.TRIAL_BALANCE_AT})

    # The manifest carries the same, independently computed expectations.
    monkeypatch.undo()
    manifest = manager.build_manifest([art], FULL)
    assert actual_rows(manifest.reconciliation_expectations) == actual_rows(full)


def test_manager_foreign_currency_records_convert_at_their_own_rates():
    manager = adapter()
    art = artifact(FX)
    fx = CHECKPOINTS["fx"]
    scan = manager.inspect([art])
    coverage = {row.source_type: row for row in scan.coverage}

    # Foreign invoices, bills, notes, receipts, payments, transfers and journals all migrate.
    assert not [row.source_type for row in coverage.values()
                if row.coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER]
    for label in ("SalesInvoice", "PurchaseInvoice", "CreditNote", "DebitNote", "Receipt", "Payment",
                  "InterAccountTransfer", "JournalEntry"):
        assert coverage[label].coverage_class in (CoverageClass.MAPPED, CoverageClass.MAPPED_WITH_LOSS), label
    # The rate table converts the records that store no rate; the table itself is not moved.
    assert coverage["ForeignCurrency"].coverage_class == CoverageClass.MAPPED
    assert coverage["ExchangeRate"].coverage_class == CoverageClass.MAPPED_WITH_LOSS
    assert "no rate of their own" in coverage["ExchangeRate"].note
    assert scan.lock_date == date.fromisoformat(fx["lock_date"])

    cutover = MigrationDecisions(mode=CIFMode.CUTOVER,
                                 cutover_date=date.fromisoformat(fx["cutover"]["cutover_date"]))
    for decisions, section in ((FULL, fx["full_history"]), (cutover, fx["cutover"])):
        assert actual_rows(manager.source_expectations([art], decisions)) == expected_rows(section, "USD"), decisions
        manifest = manager.build_manifest([art], decisions)
        assert actual_rows(manifest.reconciliation_expectations) == expected_rows(section, "USD")
        unrealized = fx["summary"]["unrealized"]
        assert manifest.source_summary["fx"] == {
            "realized": fx["summary"]["realized"],
            "unrealized": {**unrealized, "entries": {ref(label): v for label, v in unrealized["entries"].items()}},
        }

    # Documents and settlements keep their currency and rate; lines stay in the document's currency.
    bundle = manager.build_manifest([art], FULL).bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    assert (docs[ref("INVE2")].currency, docs[ref("INVE2")].exchange_rate) == ("EUR", Decimal("1.25"))
    assert [ln.total_price for ln in docs[ref("INVE2")].line_items] == [Decimal("10.01")] * 3
    assert docs[ref("BILLE1")].exchange_rate == Decimal("1.25")
    settlements = {s.source_external_id: s for s in bundle.settlements}
    assert (settlements[ref("PE1")].currency, settlements[ref("PE1")].exchange_rate) == ("EUR", Decimal("1.27"))
    # The euro journal carries its euro figures beside the base amounts; the transfer carries both sides.
    [journal] = [j for j in bundle.journals if j.source_external_id == ref("JEE1")]
    [bank_line] = [ln for ln in journal.lines if ln.account_external_id == ref("EBANK")]
    assert (bank_line.debit, bank_line.fx_debit, bank_line.fx_currency, bank_line.fx_rate) == (
        Decimal("12.00"), Decimal("10.00"), "EUR", Decimal("1.2"))
    [transfer] = bundle.bank_transfers
    assert (transfer.amount, transfer.to_amount) == (Decimal("20.00"), Decimal("26.00"))


def test_manager_foreign_record_without_any_rate_is_blocked(tmp_path):
    from fixtures.manager_io import specs
    from fixtures.manager_io.encoder import write_manager_file

    # Without the rate table, the euro journal has no rate to convert at; everything else stores its own.
    objects = [o for o in specs.fx_objects() if o.content_type != specs.T["ExchangeRate"]]
    source = write_manager_file(tmp_path / "no-rates.manager", objects)
    coverage = {row.source_type: row for row in adapter().inspect([artifact(source)]).coverage}
    blocked = {label: row for label, row in coverage.items()
               if row.coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER}
    assert set(blocked) == {"JournalEntry (no exchange rate)"}
    assert blocked["JournalEntry (no exchange rate)"].count == 1
    with pytest.raises(ScanError):
        adapter().build_manifest([artifact(source)], FULL)
