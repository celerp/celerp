# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The notice under an AI reply whose file held several bills is as tall as its text and button:
the markup's own line breaks never show as blank bands inside it, on a desktop or a phone."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import Div, to_xml

pytestmark = pytest.mark.browser

_CSS = (Path(__file__).resolve().parents[2] / "ui" / "static" / "app.css").read_text()


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("tier", ["free", "ai", None])
def test_the_many_bills_notice_has_no_blank_bands(browser, tier, width):
    from celerp_ai.ui_routes import _many_bills_notice, _msg_bubble

    notice, = _many_bills_notice({"files": [{"bill_count": 3}]}, tier, "en")
    body = to_xml(Div(_msg_bubble("ai", "stack.pdf seems to hold 3 bills."), notice, cls="ai-messages"))
    page = browser.new_page(viewport={"width": width, "height": 900})
    try:
        page.set_content(f"<style>{_CSS}</style>{body}")
        spare = page.evaluate("""() => {
            const box = document.querySelector('.ai-messages > :last-child');
            const s = getComputedStyle(box);
            const inner = box.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom);
            const kids = [...box.children].reduce((h, k) => {
                const m = getComputedStyle(k);
                return h + k.getBoundingClientRect().height + parseFloat(m.marginTop) + parseFloat(m.marginBottom);
            }, 0);
            return inner - kids;
        }""")
    finally:
        page.close()
    assert spare <= 2, f"the notice holds {spare}px of blank space beside its text and button"
