# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""List types are shown in the user's language on the Lists tabs, while the raw
type stays in the tab links."""
from __future__ import annotations

import pytest

from ui import i18n
from fasthtml.common import to_xml

from ui.routes.documents import _list_type_tabs

_DE = {"quotation": "Angebot", "transfer": "Umlagerung", "audit": "Inventur",
       "writeoff": "Abschreibung", "shipping_doc": "Versandbeleg"}


@pytest.fixture
def de():
    i18n.set_lang("de")
    yield
    i18n.set_lang("en")


def test_list_type_tabs_are_translated(de):
    html = to_xml(_list_type_tabs("", {}))
    for raw, label in _DE.items():
        assert f">{label}</a>" in html, raw
        assert f"type={raw}" in html
    for english in ("Quotation", "Transfer", "Audit", "Write-off", "Shipping Document"):
        assert f">{english}</a>" not in html


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
@pytest.mark.parametrize("raw", ["transfer", "audit", "writeoff"])
def test_list_type_label_in_every_locale(lang, raw):
    assert f"enum.list_type.{raw}" in i18n._cached_load(lang)
