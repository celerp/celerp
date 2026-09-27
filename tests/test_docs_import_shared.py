# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Importing a document another Celerp shared, from inside the app.

The recipient pastes the share link (or uploads the downloaded .celerp file)
on the documents import page; the UI calls the API with the user's session and
opens the received document.
"""

import httpx
import pytest
from fasthtml.common import Div, Span, to_xml

import ui.api_client as api
from ui.api_client import APIError
from ui.routes import docs_import as di


class _CaptureApp:
    def __init__(self):
        self.handlers: dict = {}

    def get(self, path):
        def deco(fn):
            self.handlers[("GET", path)] = fn
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.handlers[("POST", path)] = fn
            return fn
        return deco


def _routes():
    app = _CaptureApp()
    di.setup_routes(app)
    return app.handlers


class _FormReq:
    def __init__(self, form: dict):
        self._form = form
        self.cookies: dict = {}

    async def form(self):
        return self._form


class _Upload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


async def _fake_base_shell(*content, title="", **kwargs):
    return Div(Span(title), Div(*content))


@pytest.fixture
def ui_routes(monkeypatch):
    monkeypatch.setattr(di, "base_shell", _fake_base_shell)
    monkeypatch.setattr(di, "_token", lambda request: "tok")
    return _routes()


@pytest.mark.parametrize("link, expected", [
    ("https://www.celerp.com/accept?src=https%3A%2F%2Fshop.example.com&token=abc123",
     ("https://shop.example.com", "abc123")),
    ("https://shop.example.com/share/abc123", ("https://shop.example.com", "abc123")),
    ("https://example.com/celerp/share/abc123/", ("https://example.com/celerp", "abc123")),
    ("  https://shop.example.com/share/abc123  ", ("https://shop.example.com", "abc123")),
])
def test_parse_share_link_accepts_both_link_forms(link, expected):
    assert di.parse_share_link(link) == expected


@pytest.mark.parametrize("link", [
    "",
    "abc123",
    "ftp://shop.example.com/share/abc123",
    "https://shop.example.com/docs/abc123",
    "https://shop.example.com/share/",
    "https://shop.example.com/share/abc123/bundle",
    "https://www.celerp.com/accept?token=abc123",
])
def test_parse_share_link_rejects_other_links(link):
    assert di.parse_share_link(link) is None


@pytest.mark.asyncio
async def test_import_page_offers_shared_import(ui_routes):
    html = to_xml(await ui_routes[("GET", "/docs/import")](_FormReq({})))
    assert 'action="/docs/import/shared"' in html
    assert 'action="/docs/import/shared-file"' in html
    assert 'name="link"' in html
    assert 'name="bundle"' in html


@pytest.mark.asyncio
async def test_shared_link_imports_and_opens_the_received_doc(ui_routes, monkeypatch):
    calls = []

    async def _fake_import(token, src, share_token):
        calls.append((token, src, share_token))
        return "/docs/doc:rcv:abc"

    monkeypatch.setattr(di.api, "import_shared_doc", _fake_import)
    handler = ui_routes[("POST", "/docs/import/shared")]
    resp = await handler(_FormReq({"link": "https://shop.example.com/share/abc123"}))

    assert calls == [("tok", "https://shop.example.com", "abc123")]
    assert resp.status_code == 303
    assert resp.headers["location"] == "/docs/doc:rcv:abc"


@pytest.mark.asyncio
async def test_invalid_link_explains_and_keeps_the_input(ui_routes, monkeypatch):
    async def _never(*a, **k):
        raise AssertionError("API must not be called for an invalid link")

    monkeypatch.setattr(di.api, "import_shared_doc", _never)
    handler = ui_routes[("POST", "/docs/import/shared")]
    html = to_xml(await handler(_FormReq({"link": "https://shop.example.com/docs/1"})))

    assert "not a Celerp share link" in html
    assert 'value="https://shop.example.com/docs/1"' in html


@pytest.mark.asyncio
async def test_api_failure_is_shown_on_the_page(ui_routes, monkeypatch):
    async def _fail(*a, **k):
        raise APIError(502, "Could not reach sender's Celerp instance")

    monkeypatch.setattr(di.api, "import_shared_doc", _fail)
    handler = ui_routes[("POST", "/docs/import/shared")]
    html = to_xml(await handler(_FormReq({"link": "https://shop.example.com/share/abc123"})))

    assert "Could not reach sender" in html


@pytest.mark.asyncio
async def test_celerp_file_imports_and_opens_the_received_doc(ui_routes, monkeypatch):
    calls = []

    async def _fake_bundle(token, filename, content):
        calls.append((token, filename, content))
        return "/docs/doc:rcv:def"

    monkeypatch.setattr(di.api, "import_doc_bundle", _fake_bundle)
    handler = ui_routes[("POST", "/docs/import/shared-file")]
    resp = await handler(_FormReq({"bundle": _Upload("INV-1.celerp", b'{"doc": {}}')}))

    assert calls == [("tok", "INV-1.celerp", b'{"doc": {}}')]
    assert resp.status_code == 303
    assert resp.headers["location"] == "/docs/doc:rcv:def"


@pytest.mark.asyncio
async def test_missing_celerp_file_explains(ui_routes, monkeypatch):
    async def _never(*a, **k):
        raise AssertionError("API must not be called without a file")

    monkeypatch.setattr(di.api, "import_doc_bundle", _never)
    handler = ui_routes[("POST", "/docs/import/shared-file")]
    html = to_xml(await handler(_FormReq({})))

    assert "Choose a .celerp file" in html


def _mock_api(monkeypatch, handler):
    seen = []

    def _factory(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        def _record(request):
            seen.append(request)
            return handler(request)
        return httpx.AsyncClient(
            base_url="http://api.test",
            headers={"Authorization": f"Bearer {token}"},
            transport=httpx.MockTransport(_record),
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(api, "_local_client", _factory)
    return seen


@pytest.mark.asyncio
async def test_api_client_returns_the_received_doc_path(monkeypatch):
    def _handler(request):
        if request.url.path == "/docs/import":
            return httpx.Response(302, headers={"location": "/docs/doc:rcv:abc"})
        return httpx.Response(200, json={"id": "doc:rcv:abc"})

    seen = _mock_api(monkeypatch, _handler)
    path = await api.import_shared_doc("tok", "https://shop.example.com", "abc123")

    assert path == "/docs/doc:rcv:abc"
    assert [r.url.path for r in seen] == ["/docs/import"]
    assert seen[0].url.params["src"] == "https://shop.example.com"
    assert seen[0].url.params["token"] == "abc123"
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_api_client_uploads_the_bundle(monkeypatch):
    def _handler(request):
        return httpx.Response(302, headers={"location": "/docs/doc:rcv:def"})

    seen = _mock_api(monkeypatch, _handler)
    path = await api.import_doc_bundle("tok", "INV-1.celerp", b'{"doc": {"doc_type": "invoice"}}')

    assert path == "/docs/doc:rcv:def"
    assert seen[0].method == "POST" and seen[0].url.path == "/docs/import-bundle"
    assert b'name="bundle"' in seen[0].content


@pytest.mark.asyncio
async def test_api_client_raises_the_api_error(monkeypatch):
    _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Share link not found on sender's instance"}))

    with pytest.raises(APIError) as exc:
        await api.import_shared_doc("tok", "https://shop.example.com", "gone")
    assert exc.value.status == 404
    assert "not found" in exc.value.detail
