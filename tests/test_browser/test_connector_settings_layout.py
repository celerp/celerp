# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Connector settings keep their rows apart: the per-record sync direction rows under Sync now and Disconnect,
and the pairing-link form between the instructions above it and the keys heading below."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import H3, Button, Div, P, to_xml

pytestmark = pytest.mark.browser

_CSS = (Path(__file__).resolve().parents[2] / "ui" / "static" / "app.css").read_text()


def _gaps(browser, body: str, script: str) -> list[float]:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(f"<style>{_CSS}</style><div style='width:1164px'>{body}</div>")
        return page.evaluate(script)
    finally:
        page.close()


@pytest.mark.parametrize("cid", ["celerp", "shopify"])
def test_sync_direction_stands_off_sync_now(browser, cid):
    """Each kind of record has its own direction row: the rows sit under Sync now, apart
    from it and from one another."""
    from ui.routes.settings_connectors import _entity_direction_rows, get_connector

    body = to_xml(Div(Div(Button("Sync now", cls="btn btn--sm"), cls="connector-action-area"), Div(),
                      Div(_entity_direction_rows(cid, {}, "en", detail=True),
                          cls="connector-connected-details"), cls="settings-card"))
    gap, *between = _gaps(browser, body, """() => {
        const rows = [...document.querySelectorAll('.connector-direction-toggle')].map(r => r.getBoundingClientRect());
        const above = document.querySelector('.connector-action-area').getBoundingClientRect();
        return [rows[0].top - above.bottom, ...rows.slice(1).map((r, i) => r.top - rows[i].bottom)];
    }""")
    assert len(between) == len(get_connector(cid).entity_flows) - 1
    assert gap >= 8, f"the first direction row sits {gap}px under Sync now"
    assert min(between) >= 0, f"direction rows overlap: {between}"


def test_the_pairing_form_stands_off_its_neighbours(browser):
    from ui.routes.settings_connectors import _grant_form

    body = to_xml(Div(P("Ask your client for the pairing code.", id="above"), _grant_form("en", ""),
                      Div(id="accountant-access-result"), H3("Client access keys", id="below")))
    above, below = _gaps(browser, body, """() => {
        const box = s => document.querySelector(s).getBoundingClientRect();
        return [box('form').top - box('#above').bottom, box('#below').top - box('form').bottom];
    }""")
    assert above >= 12, f"the form sits {above}px under the instructions"
    assert below >= 16, f"the keys heading sits {below}px under the form"
