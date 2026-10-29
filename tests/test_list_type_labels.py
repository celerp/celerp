# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""List types are shown in the user's language in the Lists Type select, while the raw
type stays the option value."""
from __future__ import annotations

import pytest

from ui import i18n
from fasthtml.common import to_xml

from ui.routes.documents import _list_type_options

_DE = {"quotation": "Angebot", "transfer": "Umlagerung", "audit": "Inventur",
       "writeoff": "Abschreibung", "shipping_doc": "Versandbeleg"}


@pytest.fixture
def de():
    i18n.set_lang("de")
    yield
    i18n.set_lang("en")


def test_list_type_options_are_translated(de):
    options = dict(_list_type_options("de"))
    assert options.pop("") == "Alle"
    assert options == _DE
    for english in ("Quotation", "Transfer", "Audit", "Write-off", "Shipping Document"):
        assert english not in options.values()


def test_list_type_select_renders_the_translated_options(de):
    from ui.components.list_page import select_field
    html = to_xml(select_field("type", "Typ", _list_type_options("de"), "audit"))
    for raw, label in _DE.items():
        assert f'value="{raw}"' in html and f">{label}</option>" in html, raw


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
@pytest.mark.parametrize("raw", ["transfer", "audit", "writeoff"])
def test_list_type_label_in_every_locale(lang, raw):
    assert f"enum.list_type.{raw}" in i18n._cached_load(lang)
