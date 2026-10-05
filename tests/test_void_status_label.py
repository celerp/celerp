# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A void document's status reads as a state ("Voided", de "Storniert"), never as the
Void button's verb (de "Stornieren"): on every document type's status tiles, the lists'
tiles, and the status badge, in every locale, from one catalog entry."""
from __future__ import annotations

import pytest

from ui import i18n
from ui.components.table import display_enum
from ui.routes.documents import _doc_status_cards, _list_status_cards

_DOC_TYPES = ["invoice", "memo", "credit_note", "bill", "consignment_in", "purchase_order",
              "production_order", "receipt"]


@pytest.fixture
def de():
    i18n.set_lang("de")
    yield
    i18n.set_lang("en")


@pytest.mark.parametrize("doc_type", _DOC_TYPES)
def test_doc_void_tile_is_a_status_not_a_verb(de, doc_type):
    html = str(_doc_status_cards([], "", {"count_by_status": {"void": 2}}, "USD",
                                 doc_type=doc_type, lang="de"))
    assert "Storniert" in html
    assert "Stornieren" not in html


def test_list_void_tile_is_a_status_not_a_verb(de):
    html = str(_list_status_cards({"count_by_status": {"void": 1}}))
    assert "Storniert" in html
    assert "Stornieren" not in html


def test_void_badge_is_translated(de):
    assert display_enum("void", "doc_status") == "Storniert"


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
def test_void_status_label_in_every_locale(lang):
    assert "enum.doc_status.void" in i18n._cached_load(lang), lang
