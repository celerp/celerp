# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Intent classification - detect file-through routing tasks.

Determines whether a query with files needs AI comprehension (read the file)
or is a routing/filing task (skip file content, zero credits).

Two-tier approach:
  1. Keyword match (free, instant) - catches "file this", "save this", etc.
  2. LLM fallback (GPT-5-nano, ~$0.0003) - for ambiguous cases

If no files are attached, always returns COMPREHENSION.
"""

from __future__ import annotations

import logging

from celerp.ai.llm import call_llm
from celerp.ai.models import CLASSIFY

log = logging.getLogger(__name__)


class Intent:
    COMPREHENSION = "comprehension"
    ROUTING = "routing"


_ROUTING_PATTERNS = (
    "file this",
    "save this",
    "attach this",
    "store this",
    "add this to",
    "put this in",
    "send this to",
    "move this to",
    "archive this",
)

_CLASSIFY_PROMPT = """\
Classify the user's intent for this query that includes file attachments.

COMPREHENSION: The user wants the AI to READ and ANALYZE the file content.
ROUTING: The user wants to FILE, SAVE, or MOVE the file without reading its content.

Reply with exactly one word: COMPREHENSION or ROUTING

Query: "{query}"
"""


def _keyword_match(query: str) -> str | None:
    """Check for routing keywords. Returns Intent or None if ambiguous."""
    lowered = query.lower()
    for pattern in _ROUTING_PATTERNS:
        if pattern in lowered:
            return Intent.ROUTING
    return None


async def classify_intent(query: str, has_files: bool) -> str:
    """Classify query intent. Returns Intent.COMPREHENSION or Intent.ROUTING.

    Without files, always returns COMPREHENSION (text queries always need AI).
    With files, checks keywords first, then falls back to cheap LLM classification.
    """
    if not has_files:
        return Intent.COMPREHENSION

    # Tier 1: keyword match
    result = _keyword_match(query)
    if result is not None:
        return result

    # Tier 2: LLM classification
    try:
        response = await call_llm(
            CLASSIFY,
            "You are an intent classifier. Reply with exactly one word.",
            _CLASSIFY_PROMPT.format(query=query),
            max_tokens=10,
        )
        cleaned = response.strip().upper()
        if "ROUTING" in cleaned:
            return Intent.ROUTING
        return Intent.COMPREHENSION
    except Exception:
        log.warning("Intent classification LLM failed, defaulting to COMPREHENSION", exc_info=True)
        return Intent.COMPREHENSION
