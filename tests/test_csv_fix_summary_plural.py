# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The CSV review summary agrees with its count: "1 cell needs fixing", "2 cells need
fixing", one whole sentence per form in every locale."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.csv_import import validation_result

_LOCALES = Path(__file__).parent.parent / "ui" / "locales"


@pytest.fixture(autouse=True)
def _en():
    i18n.set_lang("en")
    yield


def _summary(rows: list[dict]) -> str:
    html = to_xml(validation_result(
        csv_ref="imp_00000000000000000000000000000000", rows=rows, cols=["name", "sku"],
        validate=lambda col, val, row: not (col == "name" and val == ""),
        confirm_action="/import/confirm", error_report_action="/import/errors",
        revalidate_action="/import/revalidate", back_href="/items"))
    p = re.search(r'<p class="csv-fix-summary">(.*?)</p>', html, re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", "", p).split())


def test_one_cell_reads_singular():
    assert _summary([{"name": "", "sku": "A"}, {"name": "B", "sku": "B"}]) == \
        "1 cell needs fixing across 1 of 2 rows."


def test_two_cells_read_plural():
    assert _summary([{"name": "", "sku": "A"}, {"name": "", "sku": "B"}]) == \
        "2 cells need fixing across 2 of 2 rows."


@pytest.mark.parametrize("path", sorted(_LOCALES.glob("*.json")), ids=lambda p: p.stem)
def test_every_locale_has_both_forms_as_whole_sentences(path):
    cat = json.loads(path.read_text())
    for form in ("one", "many"):
        text = cat[f"import.cells_need_fixing_{form}"]
        assert all(f"{{{k}}}" in text for k in ("n", "rows", "total")), (path.stem, form, text)
    assert "import.cells_count" not in cat and "import.need_fixing_across" not in cat, path.stem
