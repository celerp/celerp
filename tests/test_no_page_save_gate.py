# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pages have one save-before-action mechanism: an action that needs the page's line edits stored
saves them itself before it posts (Finalize, a draft agreement's Activate). No page-wide gate
holds requests back on what other fields did, so none of its pieces may come back."""
from __future__ import annotations

from pathlib import Path

import pytest
from shipped_sources import MODULE_ROOT, UI_ROOT

ROOT = Path(__file__).resolve().parents[1]

# The pieces of the page-wide gate: the gate itself, the saved mark its field saves answered with,
# its tracking of saves that were not stored, and the line state it compared before each request.
GATE_SYMBOLS = (
    "_save_first_gate", "_celerpSaveFirstBound", "X-Celerp-Saved", "SAVED_MARK", "_saved(",
    "fieldUnsaved", "fieldsSaved", ".celerpSaved", "celerpSaveThenPost", "save_then_post",
    "_celerpLineState", "_celerpMarkLinesStored", "_celerpSavedLines", "data_save_delay",
    "field_not_saved",
)


def _sources():
    for base in (UI_ROOT, MODULE_ROOT):
        for path in (ROOT / base).rglob("*"):
            if path.suffix in (".py", ".js", ".json") and path.is_file() and "/tests/" not in path.as_posix():
                yield path


@pytest.mark.parametrize("symbol", GATE_SYMBOLS)
def test_no_piece_of_the_page_save_gate_remains(symbol):
    found = [str(p.relative_to(ROOT)) for p in _sources() if symbol in p.read_text(encoding="utf-8", errors="ignore")]
    assert not found, f"{symbol} found in {found}"
