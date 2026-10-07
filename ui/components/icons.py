# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The spreadsheet icon on the left of every button that imports a spreadsheet: the
list pages' Import buttons, the CSV import steps, the settings CSV imports and the
dashboard's "Bring in your data" buttons all draw it from here."""

from fasthtml.common import NotStr, Span

_SPREADSHEET_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
    '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
    '<path d="M8 13h2"/><path d="M14 13h2"/><path d="M8 17h2"/><path d="M14 17h2"/></svg>'
)


def import_icon() -> Span:
    """The spreadsheet icon, placed as the first child of an import button."""
    return Span(NotStr(_SPREADSHEET_SVG), cls="import-icon", aria_hidden="true")
