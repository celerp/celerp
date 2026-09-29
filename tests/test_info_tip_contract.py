# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Info tooltips have one mechanism: the shared info_tip icon and the shell's body-level bubble.
No CSS pseudo-element bubble remains to overlap it, and cards keep clipping their own content."""

from __future__ import annotations

import re
from pathlib import Path

_CSS = Path("ui/static/app.css").read_text()


def test_no_pseudo_element_tooltip_remains():
    assert not re.search(r"\.info-tip[^{]*::?(after|before)", _CSS)


def test_detail_card_still_clips_its_content():
    rule = re.search(r"^\.detail-card \{[^}]*\}", _CSS, re.M).group(0)
    assert "overflow: hidden" in rule


def test_every_info_icon_comes_from_the_shared_helper():
    from fasthtml.common import to_xml
    from ui.components.shell import info_tip

    html = to_xml(info_tip("Why this matters"))
    assert 'class="info-tip"' in html and 'data-tip="Why this matters"' in html and 'tabindex="0"' in html
    for path in Path("ui").rglob("*.py"):
        if path.name != "shell.py":
            assert 'cls="info-tip"' not in path.read_text(), path
