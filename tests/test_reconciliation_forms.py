# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Reconciliation inline create/split forms render without error.

Regression guard for the reconcile create/split partials raising a NameError
on an undefined party-search-URL constant (GitHub issue 284): the forms 500'd
the moment a user opened Create or Split on an imported statement line.
"""

from html import escape

from fasthtml.common import to_xml

from ui.components.table import PARTY_SEARCH_URL
from ui.routes.reconciliation import (
    _create_form, _split_form, _stmt_line_row, _workspace_view,
)

_LINE = {"id": "line-1", "amount": -42.5, "description": "Office supplies"}
_CHART = [
    {"code": "5000", "name": "Expenses", "account_type": "expense"},
    {"code": "1000", "name": "Cash", "account_type": "asset"},
]
_CONTACTS = [{"id": "c1", "name": "Acme Ltd"}]


def _render(builder):
    return to_xml(builder("sess-1", "line-1", _LINE, _CHART, "USD", _CONTACTS))


def test_create_form_renders_with_party_search_url():
    html = _render(_create_form)
    assert escape(PARTY_SEARCH_URL) in html


def test_split_form_renders_with_party_search_url():
    html = _render(_split_form)
    assert escape(PARTY_SEARCH_URL) in html


# ── Select-to-match interaction (issue 289) ───────────────────────────────────

_RECON = {
    "statement_balance": 100.0,
    "difference": 100.0,
    "tolerance": 1.0,
    "statement_date": "2026-08-01",
    "unreconciled_entries": [],
}
_BANK = {"bank_name": "Test Bank", "account_number": "1234"}


def test_statement_line_is_selectable_not_a_match_picker():
    """An unmatched line selects for matching on click; the old picker link is gone."""
    html = to_xml(_stmt_line_row(
        {"id": "line-9", "status": "unmatched", "amount": -20.0,
         "description": "Coffee", "line_date": "2026-08-02"},
        "sess-1", "USD"))
    assert "reconSelectLine(this, 'line-9'" in html
    assert "recon-row--selectable" in html
    assert "match-picker" not in html


def test_book_entries_are_clickable_to_match():
    """Book entries render as clickable rows wired to the active statement line."""
    lines = [{"id": "line-9", "status": "unmatched", "amount": -20.0,
              "description": "Coffee", "line_date": "2026-08-02",
              "matched_je_id": None}]
    book_entries = [{"je_id": "je:abc", "ts": "2026-08-02T10:00:00",
                     "memo": "Coffee shop", "amount": -20.0}]
    html = to_xml(_workspace_view("sess-1", _RECON, _BANK, lines, book_entries, "USD"))
    assert 'id="book-entry-je:abc"' in html
    assert "reconMatchEntry(this, 'je:abc')" in html
    assert "recon-book-entry" in html
    assert 'data-session-id="sess-1"' in html
    assert "window.reconMatchEntry" in html  # the interaction script travels with the swap
