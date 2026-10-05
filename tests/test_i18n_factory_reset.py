# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The factory reset modal in every language: the company name the owner must type sits
apart from the words around it, and the modal's copy carries no en or em dash."""

from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings import _factory_reset_card

_NAME = "Muster GmbH"
_NO_SPACES = {"ja"}  # written without spaces between words
_LANGS = i18n.available_langs()


@pytest.fixture
def lang(request):
    i18n.set_lang(request.param)
    yield request.param
    i18n.set_lang("en")


@pytest.mark.parametrize("lang", [x for x in _LANGS if x not in _NO_SPACES], indirect=True)
def test_the_company_name_to_type_is_set_apart_from_the_words_around_it(lang):
    html = to_xml(_factory_reset_card(_NAME))

    prompt = re.search(r"<p>([^<]*)<strong>" + _NAME + r"</strong>([^<]*)</p>", html)
    assert prompt, html
    before, after = prompt.groups()
    assert before.endswith(" "), (lang, before)
    assert after[:1] in (" ", "\u0964", "\u1362"), (lang, after)  # a space, or the language's own full stop


@pytest.mark.parametrize("lang", _LANGS, indirect=True)
def test_the_reset_modal_has_no_en_or_em_dash(lang):
    html = to_xml(_factory_reset_card(_NAME))

    assert not re.findall(r"[^<>]*[\u2013\u2014][^<>]*", html), lang
