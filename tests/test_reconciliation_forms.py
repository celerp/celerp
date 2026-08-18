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
from ui.routes.reconciliation import _create_form, _split_form

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
