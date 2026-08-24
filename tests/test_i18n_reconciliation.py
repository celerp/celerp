# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the bank reconciliation workspace.

The reconciliation routes build every user-facing label, status badge, toolbar
button, stat, progress line, and CSV-mapper heading by calling ``t()`` at render
time, so a request in a non-English language gets translated output. These tests
register a sentinel language ``xx`` and assert the sentinel text reaches the
rendered output while ``xx`` is active. They are red against a tree that hardcodes
the English strings (module-level ``_STATUS_LABEL`` dict, f-string labels, and
bare button/heading text) instead of resolving them per request.

Each conversion MECHANISM used in the route is covered at least once:
- R3 enum display label for the row status badge (``display_enum``),
- render-time f-string interpolation (workspace title, stats, progress),
- bare button/heading text now resolved via ``t()`` (auto-match, CSV mapper),
- an ``add_new_option`` select label (create-expense form).
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.reconciliation import (
    _workspace_view,
    _row_status_badge,
    _mapper_fragment,
    _create_form,
)

# Sentinel catalog: one unmistakable value per key the reconciliation UI renders.
_XX = {
    "enum.recon_status.unmatched": "XX_UNMATCHED",
    "enum.recon_status.matched": "XX_MATCHED",
    "recon.title": "XX_TITLE {bank} {account}",
    "recon.stat_matched": "XX_STATMATCHED {n}",
    "recon.progress_resolved": "XX_RESOLVED {done}/{total}",
    "recon.auto_match_all": "XX_AUTOMATCH",
    "recon.map_csv_columns": "XX_MAPCSV",
    "recon.add_new_account": "XX_ADDACCT",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _sample_workspace() -> str:
    recon = {
        "statement_balance": 1000.0,
        "difference": 0.0,
        "tolerance": 1.0,
        "statement_date": "2026-08-01",
    }
    bank = {"bank_name": "Test Bank", "account_number": "123"}
    lines = [
        {"id": "L1", "status": "unmatched", "amount": -50.0,
         "line_date": "2026-08-01", "description": "Coffee", "reference": "R1"},
        {"id": "L2", "status": "matched", "amount": 100.0,
         "line_date": "2026-08-02", "description": "Sale", "matched_je_id": "J1"},
    ]
    book_entries = [{"je_id": "J9", "ts": "2026-08-03T00:00:00", "memo": "Entry", "amount": 25.0}]
    return to_xml(_workspace_view("S1", recon, bank, lines, book_entries, "USD"))


def test_row_status_badge_translates_enum_label():
    # R3 display layer: raw status stays canonical in the badge class, label is translated.
    html = to_xml(_row_status_badge("unmatched"))
    assert "XX_UNMATCHED" in html
    assert "badge--recon-unmatched" in html


def test_workspace_status_badges_translate():
    html = _sample_workspace()
    assert "XX_UNMATCHED" in html
    assert "XX_MATCHED" in html


def test_workspace_title_interpolates_translated():
    html = _sample_workspace()
    assert "XX_TITLE" in html
    # Interpolated operands survive the translated template.
    assert "Test Bank" in html


def test_workspace_stat_and_progress_translate():
    html = _sample_workspace()
    assert "XX_STATMATCHED" in html
    assert "XX_RESOLVED" in html


def test_workspace_toolbar_button_translates():
    html = _sample_workspace()
    assert "XX_AUTOMATCH" in html


def test_mapper_heading_translates():
    html = to_xml(_mapper_fragment("S1", ["Date", "Amount"], "", "upload.csv"))
    assert "XX_MAPCSV" in html


def test_create_form_add_new_option_translates():
    line = {"amount": -50.0, "description": "Coffee"}
    chart = [{"code": "5000", "name": "Expenses", "account_type": "expense"}]
    html = to_xml(_create_form("S1", "L1", line, chart, "USD", contacts=[]))
    assert "XX_ADDACCT" in html
