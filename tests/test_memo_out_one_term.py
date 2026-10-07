# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods out on memo read with one term everywhere: the contact's On Memo card, the
inventory status chip, and the document line badge all show the same catalog entry,
so German reads "In Kommission" (consignment), never "Auf Vermerk" (a note)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.contacts import _financial_summary
from ui.routes.documents import _STATUS_BADGE

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


@pytest.fixture
def de():
    i18n.set_lang("de")
    yield
    i18n.set_lang("en")


def test_contact_on_memo_card_reads_consignment_in_german(de):
    html = to_xml(_financial_summary([], contact_id="contact:1", on_memo_total=5.0))
    assert "In Kommission" in html
    assert "Vermerk" not in html


def test_document_memo_out_badge_uses_the_inventory_term():
    assert _STATUS_BADGE["memo_out"][0] == "inventory.status_on_memo"


@pytest.mark.parametrize("path", sorted(_LOCALES.glob("*.json")), ids=lambda p: p.stem)
def test_no_second_copy_of_the_on_memo_term(path):
    cat = json.loads(path.read_text(encoding="utf-8"))
    assert "contacts.on_memo" not in cat
    assert "documents.status_memo_out" not in cat


_DE_MEMO_DOC_KEYS = ("documents.finalize_tip_memo", "documents.memo_ref", "documents.tip_convert_memo",
                     "documents.tip_convert_quote_memo", "documents.finalize_label_memo",
                     "event.memo.created", "event.memo.returned")


def test_german_consignment_and_memo_wording():
    """Consignment reads "Kommission", and the memo document is a "Memo", not a note."""
    cat = json.loads((_LOCALES / "de.json").read_text(encoding="utf-8"))
    assert not {k: v for k, v in cat.items() if "Konsignation" in v}
    for key in _DE_MEMO_DOC_KEYS:
        assert "Memo" in cat[key] and not any(w in cat[key] for w in ("Vermerk", "Notiz", "Mitteilung")), key
