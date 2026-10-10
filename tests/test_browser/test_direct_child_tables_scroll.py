"""A table placed straight in the page body scrolls inside its own box on a phone or tablet:
the payments list (every tab) and received documents never make the page wider than the screen."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_PAGES = ("/payments", "/payments?tab=all", "/payments?tab=received", "/payments?tab=sent",
          "/payments?tab=voided", "/docs/received")


@pytest.mark.parametrize("width", [320, 390, 820])
def test_payments_and_received_fit_a_narrow_screen(page, ui_server, fresh_company, width):
    page.set_viewport_size({"width": width, "height": 740})
    wide = []
    for url in _PAGES:
        page.goto(f"{ui_server}{url}", wait_until="load")
        page.wait_for_timeout(500)
        doc_w, client_w = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        if doc_w != client_w:
            wide.append(f"{url}: {doc_w} > {client_w}")
    assert not wide, f"at {width}px: {wide}"


def test_no_data_table_sits_straight_in_main_content(page, ui_server, fresh_company):
    """The narrow-screen rule scrolls a table's parent; a table whose parent is the page body
    itself has no parent the rule reaches, so it must sit in a .table-scroll-wrap."""
    bare = []
    for url in _PAGES:
        page.goto(f"{ui_server}{url}", wait_until="load")
        if page.locator(".main-content > .data-table").count():
            bare.append(url)
    assert not bare, bare
