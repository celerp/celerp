# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The client's From your accountant section: each entry the accountant posted, in the
order the books return them, with a reverse or undo naming the date it posts on, and a
refusal shown in place."""
from __future__ import annotations

import json

import pytest
from fasthtml.common import to_xml
from fastapi import FastAPI

from ui import api_client
from ui.routes import settings_connectors as routes

pytestmark = pytest.mark.asyncio

ITEMS = [
    {"id": "je-3", "memo": "Dated today", "ts": "2026-10-16", "status": "posted", "lines": "1200 Dr 30.00",
     "total": 30.0, "reverse_on": None, "locked_through": "2026-12-31", "reversal": None},
    {"id": "je-2", "memo": "Year-end accrual", "ts": "2026-06-30", "status": "posted", "lines": None,
     "total": 80.0, "reverse_on": "2026-06-30", "reversal": {"id": "je-2r", "ts": "2026-06-30",
                                                             "undo_on": "2026-10-16"}},
    {"id": "je-1", "memo": None, "ts": "2026-03-31", "status": "posted", "lines": "6100 Dr 10.00",
     "total": 10.0, "reverse_on": "2026-03-31", "reversal": None},
    {"id": "je-0", "memo": "Withdrawn", "ts": "2026-02-01", "status": "void", "lines": "", "total": 5.0,
     "reverse_on": None, "reversal": None},
]


class Request:
    headers = {"HX-Request": "true"}
    cookies: dict = {}
    query_params: dict = {}


def _endpoint(monkeypatch, *, items=ITEMS, refuse=None, already=False):
    posted = []

    async def journals_from_accountant(token):
        return items

    async def reverse_from_accountant(token, je_id):
        posted.append(je_id)
        if refuse:
            raise api_client.APIError(409, refuse)
        return {"je_id": "new", "ts": "2026-03-31", "already": already}

    monkeypatch.setattr(api_client, "journals_from_accountant", journals_from_accountant)
    monkeypatch.setattr(api_client, "reverse_from_accountant", reverse_from_accountant)
    monkeypatch.setattr(routes, "_token", lambda request: "test-token")
    monkeypatch.setattr(routes, "get_lang", lambda request: "en")
    app = FastAPI()
    routes.setup_routes(app)
    path = "/settings/connectors/celerp/from-accountant/{je_id}/reverse"
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == path), posted


async def test_section_lists_entries_with_what_each_can_do(monkeypatch):
    _endpoint(monkeypatch)
    html = to_xml(await routes._from_accountant_section("test-token", "en"))
    assert 'id="from-accountant"' in html and "From your accountant" in html
    order = [html.index(m) for m in ("Dated today", "Year-end accrual", "2026-03-31", "Withdrawn")]
    assert order == sorted(order)  # the books' newest-first order is kept
    # no open date: the lock date is named, with the way to move it
    assert "The books are locked through 2026-12-31, so no date is open for the reversal." in html
    assert 'href="/settings/accounting?tab=period-lock"' in html
    # the date shown is the reversal entry's own date, not the day someone reversed it
    assert "Reversal dated 2026-06-30" in html and "/from-accountant/je-2r/reverse" in html
    assert "<td>1200 Dr 30.00</td>" in html  # lines read in the table's own font
    assert "Undo the reversal of Year-end accrual? The undo posts on 2026-10-16." in html
    assert "Reverse --? The reversal posts on 2026-03-31." in html  # an empty memo shows --
    assert "/from-accountant/je-1/reverse" in html
    assert "Voided by your accountant" in html and "/from-accountant/je-0/" not in html
    assert ">--<" in html  # the empty lines cell


async def test_section_without_entries_says_so(monkeypatch):
    _endpoint(monkeypatch, items=[])
    html = to_xml(await routes._from_accountant_section("test-token", "en"))
    assert "Your accountant has not posted any journal entries here yet." in html
    assert "<table" not in html


async def test_reverse_posts_and_names_the_date(monkeypatch):
    endpoint, posted = _endpoint(monkeypatch)
    response = await endpoint(Request(), "je-1")
    assert posted == ["je-1"]
    body = response.body.decode()
    assert 'id="from-accountant"' in body
    toast = json.loads(response.headers["HX-Trigger"])["celerpToast"]
    assert toast == {"message": "Posted, dated 2026-03-31.", "type": "success"}


async def test_a_repeat_from_a_stale_tab_says_nothing_new_was_posted(monkeypatch):
    endpoint, _ = _endpoint(monkeypatch, already=True)
    response = await endpoint(Request(), "je-1")
    toast = json.loads(response.headers["HX-Trigger"])["celerpToast"]
    assert toast == {"message": "This was already done, dated 2026-03-31. Nothing new was posted.",
                     "type": "info"}


async def test_refused_reverse_is_shown_in_the_section(monkeypatch):
    endpoint, posted = _endpoint(monkeypatch, refuse="The period is locked through 2026-10-16.")
    html = to_xml(await endpoint(Request(), "je-1"))
    assert posted == ["je-1"]
    assert "The period is locked through 2026-10-16." in html and 'id="from-accountant"' in html
    assert "Dated today" in html  # the list stays in place
