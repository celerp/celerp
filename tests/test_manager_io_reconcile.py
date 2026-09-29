# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager.io source reconciliation: expectations from the source, and proven FX treatment."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from celerp.importers import schema
from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.encoder import write_manager_file
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
    assert {row.measure for row in full.expectations} == set(schema.ReconciliationMeasure)

    # The manifest carries the same, independently computed expectations.
    monkeypatch.undo()
    manifest = manager.build_manifest([art], FULL)
    assert actual_rows(manifest.reconciliation_expectations) == actual_rows(full)


def test_manager_multicurrency_requires_proven_fx_treatment(tmp_path):
    manager = adapter()
    art = artifact(FX)
    figures = CHECKPOINTS["fx"]["full_history"]
    manifest = manager.build_manifest([art], FULL)
    bundle = manifest.bundle

    assert {c.code for c in bundle.currencies} >= {"USD", "EUR"}
    assert {(r.from_currency, r.to_currency, r.rate, r.effective_date) for r in bundle.exchange_rates} == {
        ("EUR", "USD", Decimal("1.2"), date(2026, 1, 1)), ("EUR", "USD", Decimal("1.3"), date(2026, 3, 31))}

    # Conversion follows the source's rate direction: an inverse rate of 0.8 is 1.25 USD per EUR.
    records = {r.source_external_id: r for r in [*bundle.documents, *bundle.settlements]}
    for label, rate in figures["fx"]["rates"].items():
        record = records[ref(label)]
        assert record.currency == "EUR", label
        assert Decimal(record.exchange_rate) == Decimal(rate), label
        amount = record.total if hasattr(record, "total") else record.amount
        base = (Decimal(amount) * Decimal(record.exchange_rate)).quantize(Decimal("0.01"))
        assert base == Decimal(figures["fx"]["base_amounts"][label]), label
    accounts = {a.source_external_id: a for a in bundle.accounts}
    assert accounts[ref("EBANK")].currency == "EUR"

    # Realized FX is posted and reconciles; unrealized FX is measured from the source and reported.
    fx = manifest.source_summary["fx"]
    assert Decimal(fx["realized"]) == Decimal(figures["fx"]["realized"])
    unrealized = figures["fx"]["unrealized"]
    assert fx["unrealized"]["as_of"] == unrealized["as_of"]
    assert {k: Decimal(v) for k, v in fx["unrealized"]["rates"].items()} == {
        k: Decimal(v) for k, v in unrealized["rates"].items()}
    assert {k: Decimal(v) for k, v in fx["unrealized"]["entries"].items()} == {
        ref(k): Decimal(v) for k, v in unrealized["entries"].items()}
    assert Decimal(fx["unrealized"]["net"]) == Decimal(unrealized["net"])
    gain_account = ref("@ProfitAndLossStatementAccountCurrencyGainsLosses")
    gain_lines = [l for j in bundle.journals for l in j.lines if l.account_external_id == gain_account]
    assert sum((Decimal(l.debit) - Decimal(l.credit) for l in gain_lines), Decimal(0)) == Decimal(figures["fx"]["realized"])
    assert actual_rows(manager.source_expectations([art], FULL)) == expected_rows(figures, "USD")

    # A foreign-currency financial type without a fixture proof of its FX treatment blocks Full history.
    unproven = write_manager_file(tmp_path / "unproven.manager", [*specs.fx_objects(), specs.fx_credit_note()])
    art = artifact(unproven)
    coverage = {row.source_type: row for row in manager.inspect([art]).coverage}
    blocker = coverage["CreditNote (foreign currency)"]
    assert (blocker.count, blocker.coverage_class) == (1, CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER)
    with pytest.raises(ScanError, match="cannot be migrated"):
        manager.build_manifest([art], FULL)
