# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The icon helper: inline Lucide SVG, sized in em, and the import icon it also draws."""
from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from ui.components.icons import icon, icon_names, import_icon

FIRST_REPLACEMENT = {
    "building", "truck", "package", "file-text", "message-square", "notebook-pen", "handshake", "user",
    "history", "eye", "trash", "camera", "scan", "sparkles", "pencil", "x", "menu", "globe", "bell",
    "settings", "star", "check", "alert-triangle", "info",
}


def test_icon_is_an_inline_svg_that_takes_the_text_colour():
    svg = str(icon("truck"))
    assert svg.startswith("<svg ") and svg.endswith("</svg>")
    assert 'viewBox="0 0 24 24"' in svg
    assert 'stroke="currentColor"' in svg and 'fill="none"' in svg
    assert 'width="1em"' in svg and 'height="1em"' in svg
    assert 'aria-hidden="true"' in svg


def test_size_is_in_em_or_any_css_length():
    assert 'width="1.5em" height="1.5em"' in str(icon("eye", 1.5))
    assert 'width="2" height="2"' not in str(icon("eye", 2))
    assert 'width="2em" height="2em"' in str(icon("eye", 2))
    assert 'width="18px" height="18px"' in str(icon("eye", "18px"))


def test_icon_can_leave_hiding_to_its_wrapper():
    assert "aria-hidden" not in str(icon("eye", hidden=False))


def test_an_unknown_name_is_an_error_naming_the_icons():
    with pytest.raises(ValueError, match=r"Unknown icon 'trcuk'.*truck"):
        icon("trcuk")


def test_every_icon_the_first_replacement_needs_is_there():
    assert FIRST_REPLACEMENT <= set(icon_names())


def test_every_icon_draws_something_with_balanced_markup():
    for name in icon_names():
        svg = str(icon(name))
        assert re.search(r"<(path|circle|rect|line|polyline|polygon|ellipse)\b", svg), name
        assert svg.count("<svg") == 1 and svg.count("</svg>") == 1, name


def test_import_icon_is_still_the_spreadsheet_icon_in_its_wrapper():
    html = to_xml(import_icon())
    assert html.startswith('<span aria-hidden="true" class="import-icon"><svg ')
    assert str(icon("file-spreadsheet", hidden=False)) in html
