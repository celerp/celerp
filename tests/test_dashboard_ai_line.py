# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The getting-started card's Celerp AI line.

People who manage the company and may use the AI assistant see one button-styled
link to /ai with the line "Need help getting set up? Ask Celerp AI." and nothing
else: no question count and no eligibility terms.
"""

from __future__ import annotations

import re

import pytest

from test_dashboard_getting_started import _card, _dashboard, ui  # noqa: F401
from ui.i18n import t


def _ai_line(card: str) -> str:
    m = re.search(r'<p[^>]*class="getting-started-ai".*?</p>', card, re.S)
    return m.group(0) if m else ""


async def test_card_line_is_a_button_to_ai(ui):
    card = _card(await _dashboard(ui))
    line = _ai_line(card)
    assert re.search(r'<a [^>]*href="/ai"[^>]*class="btn btn--primary"[^>]*>'
                     r'Need help getting set up\? Ask Celerp AI\.</a>', line), line
    assert t("dashboard.getting_started_ai") == "Need help getting set up? Ask Celerp AI."
    assert card.index("getting-started-links") < card.index("getting-started-ai") \
        < card.index("getting-started-note")


async def test_card_line_names_no_count_or_terms(ui):
    text = re.sub(r"<[^>]+>", "", _ai_line(_card(await _dashboard(ui)))).strip()
    assert text == "Need help getting set up? Ask Celerp AI."
    assert not re.search(r"\d", text)
    assert "eligible" not in _card(await _dashboard(ui)).lower()


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_card_line_by_permission(ui, role):
    assert _ai_line(_card(await _dashboard(ui, role=role)))
    no_ai = {"role_grants": {"use_ai_assistant": ["operator"]}}
    card = _card(await _dashboard(ui, role=role, settings=no_ai))
    assert card and not _ai_line(card)
    assert _card(await _dashboard(ui, role="viewer")) == ""


def test_free_questions_text_is_the_plain_total():
    text = t("ai.free_questions_included", n=20)
    assert text == "Free accounts receive 20 free AI questions in total."
