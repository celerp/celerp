# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: a data table swapped out of the page leaves no script error behind.

The scroll cue watches each data table's size; an HTMX swap that replaces the table (a bulk void
on the journal re-draws it) takes the table out of its box, and the size watcher must let it go
instead of reading the style of a box it no longer has."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def test_removed_data_table_raises_no_error(page, ui_server):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{ui_server}/accounting")
    page.locator("table.data-table").first.wait_for()
    page.evaluate("""() => new Promise(done => {
      document.querySelectorAll('table.data-table').forEach(t => t.remove());
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 100)));
    })""")
    assert not errors, errors
