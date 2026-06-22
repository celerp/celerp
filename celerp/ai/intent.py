# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Intent classification - detect file-through routing tasks.

Determines whether a query with files needs AI comprehension (read the file)
or is a routing/filing task (skip file content). Keyword-based and instant, so
classifying intent never costs a metered model call. Ambiguous cases default to
COMPREHENSION.

If no files are attached, always returns COMPREHENSION.
"""

from __future__ import annotations

import logging

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
    With files, a routing keyword marks a file-only task; otherwise COMPREHENSION.
    """
    if not has_files:
        return Intent.COMPREHENSION
    result = _keyword_match(query)
    return result if result is not None else Intent.COMPREHENSION
