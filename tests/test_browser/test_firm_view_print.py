# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A printed Client companies list is the list: no search box, no filter dropdown and no
Actions column, whose buttons do not print."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import to_xml

pytestmark = pytest.mark.browser

_CSS = (Path(__file__).resolve().parents[2] / "ui" / "static" / "app.css").read_text()


def test_print_drops_the_filters_and_the_actions_column(browser):
    from ui.routes.firm_view import _filters, _table

    row = {"company_id": "c1", "company_name": "Demo Trading Co", "role": "owner", "sync": "off",
           "last_sync": None, "attention": [], "unavailable": []}
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(f"<style>{_CSS}</style>{to_xml(_filters('', '', 'en'))}"
                         f"{to_xml(_table([row], 'en', {}))}")
        page.emulate_media(media="print")
        shown = page.evaluate("""() => ({
            filters: document.querySelector('.firm-view-filters').offsetParent !== null,
            headers: [...document.querySelectorAll('#firm-view th')]
                .filter(th => th.offsetParent !== null).map(th => th.textContent.trim()),
        })""")
    finally:
        page.close()
    assert not shown["filters"], "the search box and filter print"
    assert shown["headers"] == ["Company", "Sync", "Last sync", "Needs attention"], shown["headers"]
