# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The getting-started card's Celerp AI line.

People who manage the company and may use the AI assistant see one line pointing
at /ai: with the free account's question count when the install is not on a paid
plan and the count is known, without a number otherwise. A failed ai-offer read
only drops the number; it never hides the card.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from test_dashboard_getting_started import _card, _dashboard, ui  # noqa: F401
from ui.api_client import APIError

_WITH_COUNT = "A free account includes 20 questions."


def _ai_line(card: str) -> str:
    import re
    m = re.search(r'<p[^>]*class="getting-started-ai".*?</p>', card, re.S)
    return m.group(0) if m else ""


async def test_card_line_with_count(ui):
    card = _card(await _dashboard(ui, ai_offer={"paid_ai": False, "free_ai_questions": 20}))
    line = _ai_line(card)
    assert line and 'href="/ai"' in line
    assert _WITH_COUNT in line
    assert card.index("getting-started-links") < card.index("getting-started-ai") \
        < card.index("getting-started-note")


@pytest.mark.parametrize("offer", [
    {"paid_ai": True, "free_ai_questions": 20},
    {"paid_ai": False, "free_ai_questions": None},
    {"paid_ai": False, "free_ai_questions": 0},
    {},
])
async def test_card_line_without_count_for_paid(ui, offer):
    line = _ai_line(_card(await _dashboard(ui, ai_offer=offer)))
    assert line and 'href="/ai"' in line
    assert "free account includes" not in line
    assert "Ask Celerp AI" in line


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_card_line_by_permission(ui, role):
    offer = {"paid_ai": False, "free_ai_questions": 20}
    assert _ai_line(_card(await _dashboard(ui, role=role, ai_offer=offer)))
    no_ai = {"role_grants": {"use_ai_assistant": ["operator"]}}
    card = _card(await _dashboard(ui, role=role, settings=no_ai, ai_offer=offer))
    assert card and not _ai_line(card)
    assert _card(await _dashboard(ui, role="viewer", ai_offer=offer)) == ""


async def test_card_line_ai_offer_failure_keeps_card(ui):
    failing = AsyncMock(side_effect=APIError(503, "relay down"))
    card = _card(await _dashboard(ui, ai_offer=failing))
    assert card
    line = _ai_line(card)
    assert line and "free account includes" not in line
