# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The CSV review summary and its column badge agree with their count: "1 cell needs
fixing", "2 cells need fixing", "Name (1 error)", "Name (2 errors)", one whole phrase
per form in every locale."""
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


def _html(rows: list[dict]) -> str:
    return to_xml(validation_result(
        csv_ref="imp_00000000000000000000000000000000", rows=rows, cols=["name", "sku"],
        validate=lambda col, val, row: not (col == "name" and val == ""),
        confirm_action="/import/confirm", error_report_action="/import/errors",
        revalidate_action="/import/revalidate", back_href="/items"))


def _summary(rows: list[dict]) -> str:
    html = _html(rows)
    p = re.search(r'<p class="csv-fix-summary">(.*?)</p>', html, re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", "", p).split())


def test_one_cell_reads_singular():
    assert _summary([{"name": "", "sku": "A"}, {"name": "B", "sku": "B"}]) == \
        "1 cell needs fixing across 1 of 2 rows."


def test_two_cells_read_plural():
    assert _summary([{"name": "", "sku": "A"}, {"name": "", "sku": "B"}]) == \
        "2 cells need fixing across 2 of 2 rows."


def _name_header(rows: list[dict]) -> str:
    cells = [" ".join(re.sub(r"<[^>]+>", "", th).split())
             for th in re.findall(r"<th\b.*?</th>", _html(rows), re.S)]
    return next(c for c in cells if c.startswith("Name"))


def test_one_error_badge_reads_singular():
    assert _name_header([{"name": "", "sku": "A"}, {"name": "B", "sku": "B"}]) == "Name (1 error)"


def test_two_errors_badge_reads_plural():
    assert _name_header([{"name": "", "sku": "A"}, {"name": "", "sku": "B"}]) == "Name (2 errors)"


@pytest.mark.parametrize("path", sorted(_LOCALES.glob("*.json")), ids=lambda p: p.stem)
def test_every_locale_has_both_forms_as_whole_sentences(path):
    cat = json.loads(path.read_text())
    for form in ("one", "many"):
        text = cat[f"import.cells_need_fixing_{form}"]
        assert all(f"{{{k}}}" in text for k in ("n", "rows", "total")), (path.stem, form, text)
        badge = cat[f"import.n_errors_paren_{form}"]
        assert "{n}" in badge and badge == badge.strip(), (path.stem, form, badge)
    for old in ("import.cells_count", "import.need_fixing_across", "import.n_errors_paren"):
        assert old not in cat, (path.stem, old)
