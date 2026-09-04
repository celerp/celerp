# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""list_detail / audit refresh must not make extra unbounded UI-to-API round trips.

The incident: rendering a list detail issued three backend round trips per view
(the bounded page, then a units read, then a bulk item-metadata read), and the
audit scan / set-scanned tbody swaps each re-fetched the WHOLE list through the
unbounded /lists/{id} endpoint and enriched every stored line, not the visible
page. On a large list that is a burst of avoidable authenticated requests into the
shared API pool.

These tests measure OBSERVABLE BEHAVIOR at the transport boundary: they install an
httpx.MockTransport at ui.api_client._get_transport (the single hook every UI-to-API
request is driven through) and record the method+path of every outbound round trip
the real api_client functions make while a route renders. The assertions are about
the wire traffic, not about which python helper was called.

Fix direction (green): the vendored server /lists/{id}/page returns enriched
`item_meta` in the same body, list_detail consumes it and drops the units +
items/metadata follow-ups, and the audit scan / set-scanned tbody uses the bounded
page path so the unbounded /lists/{id} is never requested.
"""

from __future__ import annotations

import json
import re
import threading

import httpx
import pytest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token


_PAGE_CAP = 100


@pytest.fixture
def ui_app():
    from ui.app import app as _app
    return _app


def _cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


class _RecordingBackend:
    """An httpx.MockTransport handler that records every outbound (method, path) the
    UI client drives and answers each known backend path with valid-enough JSON so the
    route proceeds through its whole render. Thread-safe: httpx may hand off requests
    across the event-loop thread pool.

    ``page_items`` is the slice served by /lists/{id}/page; ``full_lines`` is the whole
    stored array served by the unbounded /lists/{id}. ``page_has_item_meta`` controls
    whether /page embeds enriched item_meta (green shape) or omits it (HEAD shape, so
    the handler falls through to the units + metadata follow-ups)."""

    def __init__(self, list_header: dict, page_items: list[dict],
                 full_lines: list[dict], *, page_has_item_meta: bool):
        self._lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.page_request_limits: list[int] = []
        self._header = list_header
        self._page_items = page_items
        self._full_lines = full_lines
        self._page_has_item_meta = page_has_item_meta

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def count(self, method: str, path: str) -> int:
        with self._lock:
            return sum(1 for m, p in self.requests if m == method and p == path)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        with self._lock:
            self.requests.append((method, path))

        # --- list data endpoints (the ones under test) ---
        if method == "GET" and path.endswith("/page"):
            try:
                lim = int(request.url.params.get("limit", _PAGE_CAP))
            except (TypeError, ValueError):
                lim = _PAGE_CAP
            with self._lock:
                self.page_request_limits.append(lim)
            body: dict = {
                "list": dict(self._header),
                "items": self._page_items[:lim],
                "total": len(self._full_lines),
                "version": self._header.get("version", 1),
            }
            if self._page_has_item_meta:
                body["item_meta"] = {}
            return _json(body)

        if method == "GET" and re.fullmatch(r"/lists/[^/]+", path):
            # The UNBOUNDED full-list read. Green code must never request this from a
            # detail render or an audit tbody swap.
            return _json({**self._header, "line_items": self._full_lines})

        # --- side reads the render/tbody touches; all safe empties ---
        if method == "GET" and path == "/companies/me/units":
            return _json([])
        if method == "POST" and path == "/items/metadata":
            return _json({"items": {}})
        if method == "GET" and path == "/companies/me":
            return _json({"name": "Co", "settings": {}})
        if method == "GET" and path == "/companies/me/price-lists":
            return _json([])
        if method == "GET" and path == "/companies/me/taxes":
            return _json([])
        if method == "GET" and path == "/companies/me/locations":
            return _json({"items": [], "total": 0})
        if method == "GET" and path == "/accounting/chart":
            return _json({"items": [], "total": 0})
        if method == "GET" and re.fullmatch(r"/lists/[^/]+/notes", path):
            return _json([])
        if method == "GET" and re.fullmatch(r"/lists/[^/]+/share.*", path):
            return _json({})
        if method == "GET" and re.fullmatch(r"/crm/contacts.*", path):
            return _json({"items": [], "total": 0})
        if method == "GET" and path.startswith("/relay"):
            return _json({})

        # --- scan / set-scanned writes ---
        if method == "POST" and re.fullmatch(r"/lists/[^/]+/scan", path):
            return _json({"scanned": 1, "failed": []})
        if method == "POST" and re.fullmatch(r"/lists/[^/]+/set-scanned", path):
            return _json({"ok": True})

        # Anything else the render happens to touch: an empty object keeps the page
        # rendering rather than 500ing, and it is still RECORDED so an unexpected
        # extra round trip is never silently absorbed.
        return _json({})


def _json(payload) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode(),
                          headers={"content-type": "application/json"})


def _install(backend: _RecordingBackend):
    """Patch the shared-transport hook so every UI-to-API request runs through the
    recording MockTransport. Patching _get_transport (not the api.* functions) keeps
    the real api_client round-trip logic in the loop, which is what we are measuring."""
    return patch("ui.api_client._get_transport", return_value=backend.transport())


def _line(i: int) -> dict:
    return {"item_id": f"item:{i}", "entity_id": f"item:{i}",
            "sku": f"SKU{i}", "description": f"Item {i}",
            "quantity": 1, "unit_price": 1.0}


def _audit_header(status: str = "finalized") -> dict:
    return {
        "entity_id": "list:big", "id": "list:big",
        "list_type": "audit", "doc_type": "list",
        "status": status, "created_at": "2026-01-01", "version": 3,
    }


@pytest.mark.asyncio
async def test_list_detail_single_outbound_request(ui_app):
    """Rendering list_detail must issue exactly ONE list-data round trip - the bounded
    /lists/{id}/page - and ZERO round trips to the units and item-metadata endpoints.

    At HEAD the /page body carries no enriched item_meta, so list_detail follows up
    with GET /companies/me/units and POST /items/metadata: three list-render round
    trips instead of one. Observable signal: the recorded outbound requests."""
    full = [_line(i) for i in range(250)]
    backend = _RecordingBackend(_audit_header(), page_items=full[:_PAGE_CAP],
                                full_lines=full, page_has_item_meta=False)

    with _install(backend):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get("/lists/list:big", cookies=_cookies())

    assert r.status_code == 200, r.text

    page_calls = backend.count("GET", "/lists/list:big/page")
    units_calls = backend.count("GET", "/companies/me/units")
    meta_calls = backend.count("POST", "/items/metadata")

    assert page_calls == 1, f"expected exactly one bounded page fetch, got {page_calls}"
    assert units_calls == 0 and meta_calls == 0, (
        "list_detail must enrich from the page body, not extra round trips; recorded "
        f"{units_calls} units + {meta_calls} items/metadata requests: "
        f"{[rq for rq in backend.requests if rq[1] in ('/companies/me/units', '/items/metadata')]}")


@pytest.mark.asyncio
async def test_audit_scan_tbody_never_requests_unbounded_list(ui_app):
    """The finalized-audit scan tbody swap must NOT pull the whole list through the
    unbounded /lists/{id}, and no single list-data request may exceed the page cap.

    At HEAD list_scan reads api.get_list (unbounded) and _audit_line_tbody reads it
    again, enriching all 250 lines. Observable signal: recorded GET /lists/list:big
    (no /page suffix) requests."""
    full = [_line(i) for i in range(250)]
    backend = _RecordingBackend(_audit_header("finalized"), page_items=full[:_PAGE_CAP],
                                full_lines=full, page_has_item_meta=True)

    with _install(backend):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.post("/lists/list:big/scan",
                             data={"barcode": "CODE1"}, cookies=_cookies())

    assert r.status_code == 200, r.text

    unbounded = backend.count("GET", "/lists/list:big")
    assert unbounded == 0, (
        "the scan tbody swap must use the bounded page path, never the unbounded "
        f"full-list read; recorded {unbounded} GET /lists/list:big request(s)")
    over_cap = [n for n in backend.page_request_limits if n > _PAGE_CAP]
    assert not over_cap, f"a page request exceeded the cap: limits={backend.page_request_limits}"


@pytest.mark.asyncio
async def test_set_scanned_tbody_never_requests_unbounded_list(ui_app):
    """The set-scanned tbody swap must not pull the whole list through the unbounded
    /lists/{id} either, and no list-data request may exceed the page cap.

    At HEAD list_set_scanned renders _audit_line_tbody, which reads api.get_list
    (unbounded) and enriches all 250 lines. Observable signal: recorded
    GET /lists/list:big (no /page suffix) requests."""
    full = [_line(i) for i in range(250)]
    backend = _RecordingBackend(_audit_header("finalized"), page_items=full[:_PAGE_CAP],
                                full_lines=full, page_has_item_meta=True)

    with _install(backend):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.post("/lists/list:big/set-scanned",
                             data={"scanned": "1"}, cookies=_cookies())

    assert r.status_code == 200, r.text

    unbounded = backend.count("GET", "/lists/list:big")
    assert unbounded == 0, (
        "the set-scanned tbody swap must use the bounded page path, never the "
        f"unbounded full-list read; recorded {unbounded} GET /lists/list:big request(s)")
    over_cap = [n for n in backend.page_request_limits if n > _PAGE_CAP]
    assert not over_cap, f"a page request exceeded the cap: limits={backend.page_request_limits}"
