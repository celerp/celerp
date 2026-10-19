# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Client access keys table fits a desktop page: a key that permits every record wraps its
Permitted records cell instead of pushing Status and the row actions off screen."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import to_xml

pytestmark = pytest.mark.browser

_CSS = Path(__file__).resolve().parents[2] / "ui" / "static" / "app.css"
_CONTENT_WIDTH = 1164  # the main column beside the sidebar at 1440px


def test_a_key_permitting_every_record_fits_the_page(browser):
    from ui.routes.settings_connectors import _grants_table

    grant = {"id": "g1", "label": "Demo Trading Co", "status": "active",
             "scopes": ["contacts", "documents", "inventory", "journals", "products"],
             "created_at": "2026-10-17T00:00:00+00:00", "expires_at": "2027-10-17T00:00:00+00:00"}
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(f"<style>{_CSS.read_text()}</style>"
                         f"<div id='box' style='width:{_CONTENT_WIDTH}px'>"
                         f"{to_xml(_grants_table([grant], 'en'))}</div>")
        overflow = page.evaluate("""() => document.querySelector('#accountant-access-grants table')
            .getBoundingClientRect().right - document.querySelector('#box').getBoundingClientRect().right""")
    finally:
        page.close()
    assert overflow <= 0, f"the table runs {overflow}px past the page"
