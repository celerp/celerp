# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The star card's claim button says where it goes and what claiming does: it opens
GitHub to check the star, and a claimed badge lists the user on the public founders
wall. The user learns both before clicking, in every language."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from ui.components.shell import star_supporter_card

_LOCALES = sorted((Path(__file__).parents[1] / "ui" / "locales").glob("*.json"))


def test_claim_button_and_note_in_english():
    xml = to_xml(star_supporter_card())
    button = re.search(r'<a\b[^>]*id="star-card-claim"[^>]*>(.*?)</a>', xml, re.S).group(1)
    assert "GitHub" in button, button
    note = re.search(r'<p\b[^>]*id="star-card-claim-note"[^>]*>(.*?)</p>', xml, re.S)
    assert note, "a line under the button says what claiming does"
    assert "GitHub" in note.group(1) and "founders wall" in note.group(1), note.group(1)
    assert xml.index('id="star-card-claim"') < xml.index('id="star-card-claim-note"')


@pytest.mark.parametrize("path", _LOCALES, ids=lambda p: p.stem)
def test_claim_copy_names_github_in_every_locale(path):
    data = json.loads(path.read_text())
    assert "GitHub" in data["shell.claim_your_badge"], data["shell.claim_your_badge"]
    assert "GitHub" in data.get("shell.claim_badge_note", ""), path.stem
