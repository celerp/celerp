# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Global search bar: one aggregated call, and honest degradation of its answer.

The bar asks the API a single question through api.global_search and renders the
merged answer. A total failure shows a retry-able error rather than an empty
'no results'; a session-expiry sends the browser to login; a partial failure
shows the results it reached plus a plain notice; and only a clean search that
truly matched nothing shows the no-results message. The search input keeps its
own /search route and gains request de-duplication so a fast typist cannot pile
overlapping requests onto the pool.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

import ui.routes.search as search_route
import ui.components.shell as shell
from ui.api_client import APIError
from test_helpers import make_test_token


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _cookie():
    return {"celerp_token": make_test_token()}


async def _search(ui_client, monkeypatch, answer, q="widget"):
    """Drive GET /search with api.global_search returning (or raising) `answer`."""
    calls = {"global_search": 0, "list": 0}

    async def _global_search(token, query):
        calls["global_search"] += 1
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _list_forbidden(*a, **k):
        calls["list"] += 1
        raise AssertionError("the search route must not call a per-module list endpoint")

    monkeypatch.setattr(search_route.api, "global_search", _global_search)
    # Any direct per-module call is a regression to the old fan-out.
    for name in ("list_items", "list_contacts", "list_docs", "list_mfg_orders",
                 "list_subscriptions", "get_journal"):
        if hasattr(search_route.api, name):
            monkeypatch.setattr(search_route.api, name, _list_forbidden)

    r = await ui_client.get("/search", params={"q": q}, cookies=_cookie())
    return r, calls


@pytest.mark.asyncio
async def test_search_ui_uses_one_internal_api_call(ui_client, monkeypatch):
    answer = {
        "results": {
            "celerp-inventory": {"items": [{"id": "i1", "name": "Widget", "sku": "W-1", "status": "active"}]},
            "celerp-contacts": {"items": [{"id": "c1", "name": "Widget Corp"}]},
            "celerp-accounting": {"entries": [{"je_id": "j1", "memo": "widget purchase"}]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    assert calls["global_search"] == 1
    assert calls["list"] == 0
    body = r.text
    # Each module's row rendered from the one merged answer.
    assert "Widget" in body and "W-1" in body
    assert "Widget Corp" in body
    assert "widget purchase" in body
    assert "/inventory/i1" in body
    assert "/contacts/c1" in body


@pytest.mark.asyncio
async def test_search_input_keeps_ui_route_and_adds_hx_sync():
    # The topbar search input keeps its own /search route and de-duplicates
    # in-flight requests so overlapping keystrokes cannot pile onto the pool.
    markup = to_xml(shell._topbar([], "en"))
    assert 'hx-get="/search"' in markup
    assert 'hx-sync="this:replace"' in markup


@pytest.mark.asyncio
async def test_search_failure_is_not_no_results(ui_client, monkeypatch):
    # A total API failure is an honest error, never an empty result set.
    r, calls = await _search(ui_client, monkeypatch, APIError(503, "saturated"))
    assert r.status_code == 200
    assert calls["global_search"] == 1
    body = r.text
    assert "No results" not in body
    assert "unavailable" in body.lower()


@pytest.mark.asyncio
async def test_session_expiry_redirects_to_login(ui_client, monkeypatch):
    # A 401 belongs to login handling: send the browser to login, do not swap a
    # broken fragment into the results dropdown.
    r, calls = await _search(ui_client, monkeypatch, APIError(401, "expired"))
    assert r.headers.get("HX-Redirect") == "/login"
    assert "No results" not in r.text


@pytest.mark.asyncio
async def test_partial_degradation_shows_results_and_notice(ui_client, monkeypatch):
    answer = {
        "results": {
            "celerp-inventory": {"items": [{"id": "i1", "name": "Widget", "sku": "W-1", "status": "active"}]},
        },
        "degraded_modules": ["celerp-contacts"],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    # The reachable result is shown.
    assert "Widget" in body
    # A plain notice makes the partial list non-silent, and it is not the error box.
    assert "search-partial" in body
    assert "No results" not in body


@pytest.mark.asyncio
async def test_all_degraded_no_results_is_error_not_no_results(ui_client, monkeypatch):
    # Every module failed and nothing matched: that is a failure, not a genuine
    # empty result, so it shows the retry-able error rather than no-results.
    answer = {"results": {}, "degraded_modules": ["celerp-inventory", "celerp-contacts"]}
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "No results" not in body
    assert "unavailable" in body.lower()


@pytest.mark.asyncio
async def test_clean_zero_match_uses_no_results(ui_client, monkeypatch):
    # A clean search that truly matched nothing uses the no-results message.
    answer = {"results": {}, "degraded_modules": []}
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    assert "No results" in r.text
