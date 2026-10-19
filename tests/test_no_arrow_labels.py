# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Buttons and links read as words alone: no label starts with an arrow glyph."""
from __future__ import annotations

import json
from pathlib import Path

from fasthtml.common import to_xml

from ui.routes.migrations import back_link

LOCALES = sorted((Path(__file__).parent.parent / "ui" / "locales").glob("*.json"))


def test_no_label_starts_with_an_arrow():
    leading = [f"{path.stem}:{key}" for path in LOCALES
               for key, value in json.loads(path.read_text(encoding="utf-8")).items()
               if isinstance(value, str) and value.lstrip()[:1] in ("←", "→")]
    assert leading == []


def test_back_to_setup_has_no_arrow():
    html = to_xml(back_link("/setup"))
    assert ">Back to setup</a>" in html
