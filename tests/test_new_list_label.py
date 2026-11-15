# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The New button and the heading on a lists page name the type the page shows and makes."""

from __future__ import annotations

import pytest

from ui.i18n import t
from ui.routes.documents import _LIST_TYPES, _list_type_label, _lists_page_heading, _new_list_label


@pytest.mark.parametrize("list_type", [lt for lt in _LIST_TYPES if lt not in ("quotation", "audit", "shipping_doc")])
def test_a_typed_lists_page_offers_new_of_that_type(list_type):
    label = _new_list_label(list_type, "en")
    assert label == t("table.new_doc_type", "en", type=_list_type_label(list_type))
    assert label != t("page.new_list", "en")


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("list_type,key", [("", "page.new_list"), ("quotation", "page.new_list"),
                                           ("audit", "documents.new_audit"), ("shipping_doc", "page.new_shipping_doc")])
def test_untyped_quotation_audit_and_delivery_buttons_in_any_language(list_type, key, lang):
    assert _new_list_label(list_type, lang) == t(key, lang)


@pytest.mark.parametrize("list_type,expected", [("", lambda: t("page.lists")), ("quotation", lambda: t("page.lists")),
                                                ("audit", lambda: t("nav.audits")),
                                                ("transfer", lambda: _list_type_label("transfer"))])
def test_the_lists_page_heading_names_the_type_it_is_filtered_to(list_type, expected):
    assert _lists_page_heading(list_type) == expected()
