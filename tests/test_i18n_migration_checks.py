# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every reconciliation check has a label in every shipped language.

The verify page names each check through ``migration.check.<measure>``. A measure
added without a catalog entry shows its raw key in the table, so the set of
measures and the set of labels are compared directly, in all 12 locales.
"""

import json
from pathlib import Path

import pytest

from celerp.importers.schema import ReconciliationMeasure
from ui.routes.migrations import _check_label

_LOCALES = sorted((Path(__file__).resolve().parents[1] / "ui" / "locales").glob("*.json"))
_LOCALES = [p for p in _LOCALES if p.stem != "i18n_source_identical_allowlist"]


def test_twelve_locales_are_checked():
    assert len(_LOCALES) == 12


@pytest.mark.parametrize("locale", _LOCALES, ids=lambda p: p.stem)
def test_check_every_measure_has_a_label(locale):
    catalog = json.loads(locale.read_text(encoding="utf-8"))
    missing = [m.value for m in ReconciliationMeasure
               if not str(catalog.get(f"migration.check.{m.value}", "")).strip()]
    assert missing == []


@pytest.mark.parametrize("measure", list(ReconciliationMeasure), ids=lambda m: m.value)
def test_check_verify_row_never_shows_a_raw_key(measure):
    label = _check_label({"check": measure.value, "key": "", "currency": None})
    assert "migration.check." not in label
