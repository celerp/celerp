# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The canonical Celerp Import Format, version 2: strict Decimal money, balanced
journals, unambiguous dates, provenance on every entity, one version everywhere,
and explicit precision-aware reconciliation tolerances."""

from __future__ import annotations

import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from celerp.importers.adapters.base import MigrationDecisions
from celerp.importers.schema import (
    CIF_VERSION,
    CIFAccount,
    CIFBatch,
    CIFCompanyProfile,
    CIFDocument,
    CIFExchangeRate,
    CIFImportBundle,
    CIFImportManifest,
    CIFInventoryAdjustment,
    CIFJournalEntry,
    CIFMode,
    CIFSourceRecord,
    CIFTolerance,
    ReconciliationExpectation,
    ReconciliationMeasure,
)
from fixtures.manager_io.support import BASIC, adapter, artifact

REPO = Path(__file__).resolve().parents[1]
FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
PROVENANCE = {"source_system": "manager_io", "source_type": "Journal", "source_external_id": "j-1"}


def _journal(**overrides) -> dict:
    return {**PROVENANCE, "entry_date": "2026-01-31", "lines": [
        {"account_external_id": "cash", "debit": "10.00"},
        {"account_external_id": "sales", "credit": "10.00"},
    ], **overrides}


def _manifest(**overrides) -> dict:
    return {"source": "books", "source_system": "manager_io", "adapter_version": "1",
            "exported_at": "2026-04-01T00:00:00+00:00", "bundle": {}, **overrides}


def _bundle_entity_models() -> list[type[BaseModel]]:
    models = []
    for name, field in CIFImportBundle.model_fields.items():
        annotation = field.annotation
        args = getattr(annotation, "__args__", ())
        model = next((a for a in args if isinstance(a, type) and issubclass(a, BaseModel)), annotation)
        models.append(model)
    return models


def test_cif_v2_decimal_and_balanced_journal_validation():
    entry = CIFJournalEntry.model_validate(_journal())
    assert entry.lines[0].debit == Decimal("10.00")
    assert isinstance(entry.lines[0].debit, Decimal)

    for bad in (float("nan"), float("inf"), "NaN", "Infinity", "-Infinity", 10.0, True):
        with pytest.raises(ValidationError):
            CIFJournalEntry.model_validate(_journal(lines=[
                {"account_external_id": "cash", "debit": bad},
                {"account_external_id": "sales", "credit": "10.00"},
            ]))
    with pytest.raises(ValidationError, match="binary floats"):
        CIFExchangeRate.model_validate({**PROVENANCE, "from_currency": "EUR", "to_currency": "USD",
                                        "rate": 1.1, "effective_date": "2026-01-31"})
    rate = CIFExchangeRate.model_validate({**PROVENANCE, "from_currency": "EUR", "to_currency": "USD",
                                           "rate": "1.1", "effective_date": "2026-01-31"})
    assert rate.rate == Decimal("1.1")

    with pytest.raises(ValidationError, match="unbalanced"):
        CIFJournalEntry.model_validate(_journal(lines=[
            {"account_external_id": "cash", "debit": "10.00"},
            {"account_external_id": "sales", "credit": "9.99"},
        ]))
    with pytest.raises(ValidationError, match="exactly one of debit or credit"):
        CIFJournalEntry.model_validate(_journal(lines=[
            {"account_external_id": "cash", "debit": "10.00", "credit": "10.00"},
            {"account_external_id": "sales", "credit": "0"},
        ]))

    for ambiguous in ("01/02/2026", "2026/01/31", "31-01-2026", "Jan 31 2026", "2026-01-31T10:00"):
        with pytest.raises(ValidationError):
            CIFJournalEntry.model_validate(_journal(entry_date=ambiguous))
    with pytest.raises(ValidationError):
        CIFInventoryAdjustment.model_validate({**PROVENANCE, "kind": "opening", "adjustment_date": "02/01/2026",
                                               "item_external_id": "i-1", "quantity": "1"})
    with pytest.raises(ValidationError):
        CIFImportManifest.model_validate(_manifest(exported_at="04/01/2026"))

    models = _bundle_entity_models()
    assert CIFCompanyProfile in models and CIFDocument in models and CIFJournalEntry in models
    for model in models:
        assert issubclass(model, CIFSourceRecord), model
        for name in ("source_system", "source_type", "source_external_id"):
            assert model.model_fields[name].is_required(), (model, name)
    for name in ("source_system", "source_type", "source_external_id"):
        with pytest.raises(ValidationError):
            CIFAccount.model_validate({**{k: v for k, v in PROVENANCE.items() if k != name},
                                       "name": "Cash", "account_type": "asset"})
        with pytest.raises(ValidationError):
            CIFAccount.model_validate({**PROVENANCE, name: "", "name": "Cash", "account_type": "asset"})


def _python_sources() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in listed.stdout.split() if "/migrations/versions/" not in p]


def test_cif_v2_replaces_v1_everywhere(tmp_path, capsys):
    assert CIF_VERSION == "2"
    for model in (CIFImportManifest, CIFBatch):
        assert model.model_fields["cif_version"].default == "2"
    with pytest.raises(ValidationError, match="Unsupported CIF version '1'"):
        CIFImportManifest.model_validate(_manifest(cif_version="1"))
    with pytest.raises(ValidationError, match="Unsupported CIF version '1'"):
        CIFBatch.model_validate({"cif_version": "1", "source": "s", "source_system": "x"})

    from celerp.importers.importer import load_manifest
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps(_manifest(cif_version="1")))
    with pytest.raises(SystemExit):
        load_manifest(v1)
    assert "Unsupported CIF version '1'" in capsys.readouterr().err
    v2 = tmp_path / "v2.json"
    v2.write_text(json.dumps(_manifest()))
    assert load_manifest(v2).cif_version == "2"

    manifest = adapter().build_manifest([artifact(BASIC)], FULL)
    assert manifest.cif_version == "2"
    assert CIFImportManifest.model_validate(manifest.model_dump()).bundle == manifest.bundle

    # The v1 bundle entities keyed themselves by a bare `external_id`; v2 has none.
    for model in _bundle_entity_models():
        assert "external_id" not in model.model_fields, model

    definitions, v1_literals = [], []
    version_def = re.compile(r"^\s*CIF_VERSION\s*=", re.M)
    v1_literal = re.compile(r"""cif_version["']?\s*[:=]\s*["']1["']""")
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        if version_def.search(text):
            definitions.append(path.relative_to(REPO).as_posix())
        if v1_literal.search(text):
            v1_literals.append(path.relative_to(REPO).as_posix())
    assert definitions == ["celerp/importers/schema.py"]
    assert v1_literals == []


def test_reconciliation_requires_explicit_tolerance_rule():
    from celerp.services.migrations import _row

    with pytest.raises(ValidationError):
        ReconciliationExpectation(measure="ar_control", expected=Decimal("1"))
    for generic in ({"kind": "percent", "percent": "0.5"}, {"kind": "exact", "percent": "0.5"},
                    {"kind": "currency_rounding", "currency": "USD", "precision": 2, "percent": "1"}):
        with pytest.raises(ValidationError):
            CIFTolerance.model_validate(generic)
    with pytest.raises(ValidationError, match="names its currency and precision"):
        CIFTolerance(kind="currency_rounding", max_units=1)
    with pytest.raises(ValidationError, match="names its currency and precision"):
        CIFTolerance(kind="currency_rounding", currency="USD", max_units=1)
    with pytest.raises(ValidationError, match="takes no currency, precision or allowance"):
        CIFTolerance(kind="exact", max_units=1)

    rounding = CIFTolerance(kind="currency_rounding", currency="JPY", precision=0, max_units=1)

    def expectation(expected: str, tolerance: CIFTolerance) -> ReconciliationExpectation:
        return ReconciliationExpectation(measure=ReconciliationMeasure.BANK_CASH, key="bank-1",
                                         currency="JPY", expected=Decimal(expected), tolerance=tolerance)

    accepted = _row(expectation("1000", rounding), Decimal("1001"))
    assert accepted["result"] == "rounding"
    assert accepted["rule"] == "within 1 JPY"
    assert accepted["difference"] == "1"
    assert _row(expectation("1000", rounding), Decimal("1002"))["result"] == "fail"
    assert _row(expectation("1000", rounding), Decimal("1000"))["result"] == "pass"
    cents = CIFTolerance(kind="currency_rounding", currency="USD", precision=2, max_units=1)
    row = _row(expectation("10.00", cents), Decimal("10.01"))
    assert (row["result"], row["rule"], row["difference"]) == ("rounding", "within 0.01 USD", "0.01")
    exact = _row(expectation("10.00", CIFTolerance(kind="exact")), Decimal("10.01"))
    assert (exact["result"], exact["rule"]) == ("fail", "exact")

    rows = adapter().source_expectations([artifact(BASIC)], FULL).expectations
    assert rows
    for row in rows:
        tol = row.tolerance
        assert tol.kind in ("exact", "currency_rounding"), row
        if tol.kind == "currency_rounding":
            assert tol.currency == row.currency and tol.precision is not None, row
