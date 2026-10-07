# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Dashboard quick links stay tellable apart in every language.

Each card's title and description are resolved through _DASH_LABEL_KEYS at
render time. One English word used for two different pages (or two English
words that one language translates the same) shows the reader two cards with
the same name.
"""

import pytest

from ui import i18n
from ui.routes.dashboard import _DEFAULT_CONFIG, _VERTICAL_CONFIGS, _dl

_CONFIGS = {"default": _DEFAULT_CONFIG, **_VERTICAL_CONFIGS}


@pytest.fixture(autouse=True)
def _reset_lang():
    yield
    i18n.set_lang("en")


@pytest.mark.parametrize("lang", i18n.available_langs())
@pytest.mark.parametrize("part", [1, 2], ids=["title", "description"])
def test_no_two_quick_links_read_the_same(lang, part):
    i18n.set_lang(lang)
    clashes = {}
    for name, cfg in _CONFIGS.items():
        seen: dict[str, str] = {}
        for link in cfg.get("quick_links", []):
            text = _dl(link[part])
            if text in seen:
                clashes[name] = (text, seen[text], link[0])
            seen[text] = link[0]
    assert not clashes, clashes


@pytest.mark.parametrize("lang", i18n.available_langs())
def test_accounting_link_is_named_after_the_page_it_opens(lang):
    i18n.set_lang(lang)
    titles = {_dl(label) for cfg in _CONFIGS.values()
              for href, label, _ in cfg.get("quick_links", []) if href == "/accounting"}
    assert titles == {i18n.t("page.accounting")}
