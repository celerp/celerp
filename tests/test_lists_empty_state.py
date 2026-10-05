# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An empty Lists page says what a list is for, under "No lists yet.", in every locale."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.documents import _list_table

_LOCALES = Path(__file__).parent.parent / "ui" / "locales"


def test_empty_lists_card_says_what_a_list_is_for():
    i18n.set_lang("en")
    html = to_xml(_list_table([]))
    card = re.search(r'<div class="empty-state-cta">(.*?)</div>', html, re.S).group(1)
    paras = [re.sub(r"<[^>]+>", "", p).strip() for p in re.findall(r"<p\b.*?</p>", card, re.S)]
    assert paras == [i18n.t("label.no_lists_yet"), i18n.t("lists.empty_hint")]
    assert "quotation" in paras[1] and "transfer" in paras[1]


@pytest.mark.parametrize("path", sorted(_LOCALES.glob("*.json")), ids=lambda p: p.stem)
def test_every_locale_explains_lists(path):
    en = json.loads((_LOCALES / "en.json").read_text())
    cat = json.loads(path.read_text())
    assert cat.get("lists.empty_hint"), path.stem
    if path.stem != "en":
        assert cat["lists.empty_hint"] != en["lists.empty_hint"], path.stem
