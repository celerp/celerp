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


# ── Generic third-party module rendering ──────────────────────────────────────

@pytest.mark.asyncio
async def test_third_party_module_renders_canonical_row(ui_client, monkeypatch):
    # A module the first-party descriptors do not know is rendered generically
    # from its canonical rows: a labelled link with the generic search icon.
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "t1", "label": "Acme Result", "href": "/acme/1", "subtitle": "widget"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Acme Result" in body
    assert "/acme/1" in body
    assert "🔎" in body
    assert "widget" in body


@pytest.mark.asyncio
async def test_third_party_unsafe_href_row_skipped(ui_client, monkeypatch):
    # Two canonical rows from an unknown module: the app-local one renders, the
    # off-site one is skipped (the UI never links a URL it did not build).
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "s1", "label": "Safe One", "href": "/acme/1"},
                {"id": "e1", "label": "Evil One", "href": "https://evil.example/x"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Safe One" in body
    assert "/acme/1" in body
    assert "Evil One" not in body
    assert "evil.example" not in body


@pytest.mark.asyncio
async def test_third_party_javascript_href_row_skipped(ui_client, monkeypatch):
    # A script-scheme href is rejected exactly like an off-site scheme: the UI
    # re-checks every third-party href itself before rendering a link it did
    # not build.
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "s1", "label": "Safe One", "href": "/acme/1"},
                {"id": "e1", "label": "Evil One", "href": "javascript:alert(1)"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Safe One" in body
    assert "/acme/1" in body
    assert "Evil One" not in body
    assert "javascript:" not in body


@pytest.mark.asyncio
async def test_third_party_scheme_relative_href_row_skipped(ui_client, monkeypatch):
    # A protocol-relative "//host" href takes the scheme of whatever page it is
    # rendered on, so it is an off-site link in disguise and must be rejected
    # the same as an explicit https:// href.
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "s1", "label": "Safe One", "href": "/acme/1"},
                {"id": "e1", "label": "Evil One", "href": "//evil.example/x"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Safe One" in body
    assert "/acme/1" in body
    assert "Evil One" not in body
    assert "evil.example" not in body


def test_is_app_local_path_rejects_backslash_and_control():
    # DEFECT B: the shared predicate must reject a backslash (browsers normalise
    # "\" to "/", turning "/\evil.example" into an off-site redirect) and any
    # ASCII control char, while still accepting a legitimate app-local path.
    from ui.security import is_app_local_path
    assert is_app_local_path("/inventory?x=1") is True
    assert is_app_local_path("/\\evil.example") is False
    assert is_app_local_path("//evil.example") is False
    assert is_app_local_path("/inv\x01entory") is False
    assert is_app_local_path("/tab\tpath") is False
    assert is_app_local_path("/del\x7fpath") is False
    assert is_app_local_path("https://evil.example") is False
    assert is_app_local_path("") is False


@pytest.mark.asyncio
async def test_third_party_backslash_href_row_skipped(ui_client, monkeypatch):
    # A backslash-bearing href resolves off-site in the browser, so the UI must
    # skip it exactly like an explicit off-site scheme.
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "s1", "label": "Safe One", "href": "/acme/1"},
                {"id": "e1", "label": "Evil One", "href": "/\\evil.example"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Safe One" in body
    assert "/acme/1" in body
    assert "Evil One" not in body
    assert "evil.example" not in body


@pytest.mark.asyncio
async def test_third_party_control_char_href_row_skipped(ui_client, monkeypatch):
    # An href carrying an ASCII control char is unsafe in a link and is skipped.
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "s1", "label": "Safe One", "href": "/acme/1"},
                {"id": "e1", "label": "Evil One", "href": "/inv\x01entory"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Safe One" in body
    assert "/acme/1" in body
    assert "Evil One" not in body


@pytest.mark.asyncio
async def test_third_party_label_is_escaped(ui_client, monkeypatch):
    # A generic label is rendered as escaped text, never raw markup.
    answer = {
        "results": {
            "acme-crm": {"items": [
                {"id": "x1", "label": "<script>alert(1)</script>", "href": "/acme/1"},
            ]},
        },
        "degraded_modules": [],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "&lt;script&gt;" in body
    assert "<script>alert(1)</script>" not in body


@pytest.mark.asyncio
async def test_known_and_generic_results_with_partial_notice(ui_client, monkeypatch):
    # A first-party bucket and a generic third-party bucket both render, and a
    # degraded sibling still adds the partial notice, all in one answer.
    answer = {
        "results": {
            "celerp-inventory": {"items": [{"id": "i1", "name": "Widget", "sku": "W-1", "status": "active"}]},
            "acme-crm": {"items": [{"id": "t1", "label": "Acme Lead", "href": "/acme/1"}]},
        },
        "degraded_modules": ["celerp-contacts"],
    }
    r, calls = await _search(ui_client, monkeypatch, answer)
    assert r.status_code == 200
    body = r.text
    assert "Widget" in body          # first-party renderer
    assert "Acme Lead" in body       # generic renderer
    assert "/acme/1" in body
    assert "search-partial" in body  # the degraded sibling's notice
    assert "No results" not in body


@pytest.mark.asyncio
async def test_over_long_query_shows_error_not_no_results(ui_client, monkeypatch):
    # The API answers an over-long query with 422; the UI shows the retry-able
    # error, never the no-results state.
    r, calls = await _search(ui_client, monkeypatch, APIError(422, "too long"))
    assert r.status_code == 200
    body = r.text
    assert "No results" not in body
    assert "unavailable" in body.lower()
