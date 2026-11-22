# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The bank page's answer and split forms render without error.

Regression guard for the statement line forms raising a NameError on an undefined
party-search-URL constant (GitHub issue 284): the forms 500'd the moment a user
opened them on an imported statement line.
"""

from html import escape

from fasthtml.common import to_xml

from ui.components import bank_groups as BG
from ui.components.table import PARTY_SEARCH_URL

_BANK = {"id": "bank-1", "currency": "USD"}
_OPTIONS = [("post:5000", "5000 Expenses"), ("cash", "Cash")]


def test_answer_form_renders_with_party_search_url():
    assert escape(PARTY_SEARCH_URL) in to_xml(BG.answer_form(_BANK, _OPTIONS, line_id="line-1"))


def test_split_form_renders_with_party_search_url():
    html = to_xml(BG.split_form(_BANK, _OPTIONS, line_id="line-1"))
    assert html.count(escape(PARTY_SEARCH_URL)) == 3
