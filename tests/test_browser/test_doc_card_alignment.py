# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""On a document's Bill to card a read-only value, such as Outstanding, starts in the same
column as the editable values above it."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import Div, Span, to_xml

pytestmark = pytest.mark.browser

_CSS = (Path(__file__).resolve().parents[2] / "ui" / "static" / "app.css").read_text()


def test_outstanding_lines_up_with_the_editable_values(browser):
    from ui.routes.documents import _doc_display_cell

    def row(label: str, value) -> Div:
        return Div(Div(label, cls="form-label", style="width:100px"), value, cls="form-group")

    card = Div(row("Currency:", _doc_display_cell("doc:inv-1", "currency", "USD", "invoice")),
               row("Outstanding:", Span("$70.00", cls="meta-value", id="owed")), cls="doc-section")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(f"<style>{_CSS}</style><div style='width:560px'>{to_xml(card)}</div>")
        editable, owed = page.evaluate("""() => {
            const textLeft = el => { const r = document.createRange(); r.selectNodeContents(el);
                                     return r.getBoundingClientRect().left; };
            return [textLeft(document.querySelector('.editable-cell')), textLeft(document.getElementById('owed'))];
        }""")
    finally:
        page.close()
    assert abs(editable - owed) <= 1, f"Outstanding starts {editable - owed}px left of the values above"
