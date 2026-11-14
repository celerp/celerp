# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Typed text as it would show on a page or a document."""
from __future__ import annotations


def visible_text(raw: str | None) -> str:
    """``raw`` trimmed, or "" when nothing in it would show: empty, spaces, or only
    invisible characters such as a zero-width space."""
    raw = raw or ""
    return raw.strip() if any(ch.isprintable() and not ch.isspace() for ch in raw) else ""
