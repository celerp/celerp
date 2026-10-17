# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Client companies page: each company's sync, last sync and what needs attention,
opened in place, with filters carried in the URL and a way into each company."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fasthtml.common import to_xml
from fastapi import FastAPI

from ui import api_client
from ui.routes import firm_view as routes

pytestmark = pytest.mark.asyncio
COPY, PRACTICE, RUN = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def _rows():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    return [
        {"company_id": COPY, "company_name": "Demo Trading", "role": "owner", "sync": "failing",
         "last_sync": recent, "unavailable": [], "attention": [
             {"kind": "refused", "label": "INV-0005", "reason": "The period is locked through 2026-10-16.",
              "href": None, "at": recent},
             {"kind": "key_expiring", "label": "Demo Trading", "reason": "The key expires on 2026-10-21.",
              "href": "/settings/accountant-access", "at": None}]},
        {"company_id": PRACTICE, "company_name": "Firm Practice", "role": "operator", "sync": None,
         "last_sync": None, "attention": [],
         "unavailable": ["Only the company's owner sees its sync and what needs attention."]},
        {"company_id": str(uuid.uuid4()), "company_name": "Fake Co", "role": None, "sync": None,
         "last_sync": None, "unavailable": [], "attention": [
             {"kind": "migration", "label": "fake", "reason": "The move from fake is ready and has not finished.",
              "href": f"/migrations/{RUN}", "at": recent}]},
    ]


class Request:
    def __init__(self, query=None, hx=True):
        self.query_params = query or {}
        self.headers = {"HX-Request": "true"} if hx else {}
        self.cookies = {}


def _page(monkeypatch, rows=None, error=None):
    seen = []
    async def firm_view(token, params=None):
        seen.append(params)
        if error:
            raise api_client.APIError(422, error)
        return {"items": rows if rows is not None else _rows()}
    monkeypatch.setattr(api_client, "firm_view", firm_view)
    monkeypatch.setattr(routes, "_token", lambda request: "test-token")
    monkeypatch.setattr(routes, "get_lang", lambda request: "en")
    app = FastAPI()
    routes.setup_routes(app)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/companies")
    return endpoint, seen


async def test_rows_show_sync_last_sync_and_attention_in_place(monkeypatch):
    endpoint, _ = _page(monkeypatch)
    html = (await endpoint(Request())).body.decode()
    assert html.index("Demo Trading") < html.index("Firm Practice") < html.index("Fake Co")
    assert "<td>Failing</td>" in html and "<td>5m ago</td>" in html
    assert "<details>" in html and "2 to look at" in html
    assert "Not applied" in html and "INV-0005" in html and "locked through 2026-10-16" in html
    assert f'href="/switch-company/{COPY}?next=/settings/accountant-access"' in html
    assert f'href="/switch-company/{COPY}?next=/settings/company-handoff"' in html
    assert f'href="/switch-company/{COPY}?next=/settings/users/new"' in html
    assert f'href="/switch-company/{COPY}"' in html
    # Another role sees why sync is hidden, and empty cells read --.
    assert "owner sees its sync and what needs attention." in html
    assert "<td>--</td>" in html
    # A move that has not finished opens from its own page, without switching.
    assert html.count(f'href="/migrations/{RUN}"') == 2 and "Continue the move" in html


async def test_filters_round_trip_through_the_url(monkeypatch):
    endpoint, seen = _page(monkeypatch, rows=[])
    response = await endpoint(Request({"q": " demo ", "sync": "off"}))
    assert seen == [{"q": "demo", "sync": "off"}]
    assert response.headers["HX-Push-Url"] == "/companies?q=demo&sync=off"
    assert "No companies match this search." in response.body.decode()
    # A sync filter alone searched nothing, so it says so instead.
    html = (await endpoint(Request({"sync": "failing"}))).body.decode()
    assert "No companies with this sync status." in html and "match this search" not in html

    form = to_xml(routes._filters("demo", "off", "en"))
    assert 'value="demo"' in form and '<option value="off" selected>' in form
    assert 'hx-get="/companies"' in form


async def test_empty_state_and_refused_filter(monkeypatch):
    endpoint, _ = _page(monkeypatch, rows=[])
    html = (await endpoint(Request())).body.decode()
    assert "No client companies yet." in html and 'href="/setup/new-company/migrate"' in html

    endpoint, _ = _page(monkeypatch, error="Choose a sync status: On, Off, Failing or Key expiring.")
    html = (await endpoint(Request({"sync": "sometimes"}))).body.decode()
    assert "flash--warning" in html and "On, Off, Failing or Key expiring" in html
    assert "No client companies yet." not in html


async def test_switch_lands_on_the_accountant_key_page(monkeypatch):
    from ui.routes import auth
    async def switch(token, company_id):
        return "access", "refresh"
    monkeypatch.setattr(api_client, "switch_company", switch)
    monkeypatch.setattr(auth, "set_session_cookies", lambda *args: None)
    app = FastAPI()
    auth.setup_routes(app)
    endpoint = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/switch-company/{company_id}"
                    and "POST" in getattr(r, "methods", ()))
    request = Request({"next": "/settings/accountant-access"}, hx=False)
    request.cookies = {auth.COOKIE_NAME: "token"}
    response = await endpoint(request, COPY)
    assert response.headers["location"] == "/settings/accountant-access"


async def test_the_page_says_where_it_lives(monkeypatch):
    """Global Config is lit in the menu, and a breadcrumb leads back to Your companies
    there, which is where the page is opened from."""
    from ui.components import shell
    endpoint, _ = _page(monkeypatch)
    async def base_shell(*content, **kw):
        return shell.Div(*content, data_nav=kw.get("nav_active"))
    monkeypatch.setattr(shell, "base_shell", base_shell)
    html = to_xml(await endpoint(Request(hx=False)))
    assert 'data-nav="settings"' in html
    assert ('<a href="/settings/general?tab=company" class="breadcrumb-link">Your companies</a>'
            in html) and '<span class="breadcrumb-current">Client companies</span>' in html

