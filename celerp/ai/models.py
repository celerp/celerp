# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Model routing - select the right model for each task.

Single source of truth for model names and routing logic.
All models served via OpenRouter's OpenAI-compatible API.
"""

from __future__ import annotations

# OpenRouter model identifiers
BULK_EXTRACTION = "google/gemini-2.5-flash-lite"
SINGLE_FILE = "google/gemini-2.5-flash"
TEXT_QUERY = "anthropic/claude-haiku-4.5"
COMPLEX = "anthropic/claude-sonnet-4.6"
CLASSIFY = "openai/gpt-5-nano"

# Lookup keywords: these trigger the cheap text model; everything else uses complex
_LOOKUP_KEYWORDS = frozenset({
    "how many", "what is", "what are", "list", "show me", "count",
    "total", "balance", "status", "how much", "give me",
})


def select_model(query: str, file_count: int, is_batch: bool) -> str:
    """Return the OpenRouter model ID for a given query context.

    Rules:
      - Batch (2+ files) -> bulk extraction (cheapest vision)
      - Single file -> single-file vision model
      - No files, lookup keyword -> cheap text model
      - No files, complex query -> expensive reasoning model
    """
    if is_batch:
        return BULK_EXTRACTION
    if file_count == 1:
        return SINGLE_FILE
    lowered = query.lower()
    for kw in _LOOKUP_KEYWORDS:
        if kw in lowered:
            return TEXT_QUERY
    return COMPLEX
