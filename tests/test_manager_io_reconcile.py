# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager.io source reconciliation: expectations from the source, and the foreign currency boundary."""

from __future__ import annotations

from datetime import date

import pytest

from celerp.importers import schema
from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io.support import (
    BASIC, CHECKPOINTS, FX, actual_rows, adapter, artifact, expected_rows,
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


def test_manager_multicurrency_requires_proven_fx_treatment():
    manager = adapter()
    art = artifact(FX)
    coverage = {row.source_type: row for row in manager.inspect([art]).coverage}

    # Every financial record in a foreign currency is an unsupported financial blocker with its reason.
    for label, count in CHECKPOINTS["fx"]["blocked"].items():
        row = coverage[label]
        assert (row.count, row.coverage_class) == (count, CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER), label
        assert "base currency records only" in row.note, label
    blocked = {row.source_type for row in coverage.values()
               if row.coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER}
    assert blocked == set(CHECKPOINTS["fx"]["blocked"])

    # The currency master is not a financial record and still maps; its rate table is not moved.
    assert coverage["ForeignCurrency"].coverage_class == CoverageClass.MAPPED
    assert coverage["ExchangeRate"].coverage_class == CoverageClass.UNSUPPORTED_NONFINANCIAL
    with pytest.raises(ScanError, match="cannot be migrated"):
        manager.build_manifest([art], FULL)
