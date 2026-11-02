# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A translation keeps the English string's letter case: a label written in ordinary case in
English is not shouted in capitals in another language, where it would sit beside labels in
ordinary case (an address label in capitals next to a company label that is not)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIRS = [ROOT / "ui" / "locales", *sorted((ROOT / "default_modules").glob("*/*/locales"))]


def _letters(text: str) -> str:
    return re.sub(r"[^A-Za-zÀ-ɏ]", "", text)


def _all_caps(text: str) -> bool:
    # Latin-script strings only: other scripts have no letter case to compare.
    letters = _letters(text)
    return len(letters) >= 5 and letters.isupper() and not re.search(r"[^\x00-ɏ]", text)


@pytest.mark.parametrize("locales", CATALOG_DIRS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_translation_is_in_capitals_where_the_english_is_not(locales):
    english = json.loads((locales / "en.json").read_text(encoding="utf-8"))
    shouted = [
        f"{path.stem}:{key}={value!r}"
        for path in sorted(locales.glob("*.json")) if path.stem != "en"
        for key, value in json.loads(path.read_text(encoding="utf-8")).items()
        if isinstance(value, str) and _all_caps(value)
        and isinstance(english.get(key), str) and not _letters(english[key]).isupper()
    ]
    assert not shouted, shouted
