# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The ask-a-client and move-request sections of the Move a company page stand apart, and a
request's system reads as a muted line under the client, not as a second client."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import Button, Div, Form, to_xml

pytestmark = pytest.mark.browser

_CSS = (Path(__file__).resolve().parents[2] / "ui" / "static" / "app.css").read_text()
_ROW = {"id": "r1", "client_name": "Demo Trading Co", "source": "xero", "created_at": "2026-10-17",
        "files": []}


def _render(browser, body: str):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.set_content(f"<style>{_CSS}</style><div class='auth-card' style='width:660px'>{body}</div>")
    return page


def test_the_system_under_a_client_is_muted(browser):
    from ui.routes.migrations import _client_cell

    page = _render(browser, f"<table class='data-table'><tr>{to_xml(_client_cell(_ROW, {'xero': 'Xero'}))}"
                            "</tr></table>")
    try:
        client, system = page.evaluate("""() => [...document.querySelectorAll('td > div')]
            .map(d => getComputedStyle(d).color)""")
    finally:
        page.close()
    assert system != client, "the system reads in the client's own colour"


def test_request_sections_stand_apart(browser):
    from ui.routes.migrations import COMPANY, _requests_table

    def section(id_: str) -> str:
        return to_xml(Div(Form(Button("Send", cls="btn btn--secondary btn--full"), cls="auth-form"),
                          _requests_table(COMPANY, [_ROW], {"xero": "Xero"}), id=id_, cls="export-requests"))

    page = _render(browser, section("export-requests") + section("move-requests"))
    try:
        gaps = page.evaluate("""() => {
            const [a, b] = document.querySelectorAll('.export-requests');
            const button = a.querySelector('button').getBoundingClientRect().bottom;
            const heading = a.querySelector('h4').getBoundingClientRect().top;
            return [heading - button, b.getBoundingClientRect().top - a.getBoundingClientRect().bottom];
        }""")
    finally:
        page.close()
    under_button, between_sections = gaps
    assert under_button >= 12, f"the table heading sits {under_button}px under the button"
    assert between_sections >= 24, f"the sections are {between_sections}px apart"


def test_the_sign_in_box_stands_off_the_fetch_button(browser):
    from starlette.requests import Request

    from ui.routes.migrations import _xero_attachments_panel

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    panel = _xero_attachments_panel(request, "run-1", {}, account=True).body.decode()
    page = _render(browser, panel)
    try:
        gap = page.evaluate("""() => document.querySelector('#xero-attachments-form').getBoundingClientRect().top
            - document.querySelector('#celerp-account-panel').getBoundingClientRect().bottom""")
    finally:
        page.close()
    assert gap >= 12, f"the fetch button sits {gap}px under the sign-in box"


def test_the_revoke_note_stands_off_send_company_copy(browser):
    from ui.routes.company_backup import _copies_table

    row = {"id": "c1", "recipient": "client@example.test", "firm_name": "Harbor Bookkeeping",
           "status": "sent", "opened": True, "downloaded": False, "company_opened": False,
           "sent_at": "2026-10-17", "expires_at": "2026-10-31T00:00:00+00:00"}
    page = _render(browser, to_xml(_copies_table([row])) + "<a class='btn btn--primary' id='send'>Send</a>")
    try:
        gap = page.evaluate("""() => document.querySelector('#send').getBoundingClientRect().top
            - document.querySelector('#copies-list').getBoundingClientRect().bottom""")
    finally:
        page.close()
    assert gap >= 12, f"Send company copy sits {gap}px under the revoke note"
