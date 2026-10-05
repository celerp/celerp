# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Importing a document another Celerp shared, from inside the app.

The recipient pastes the share link (or uploads the downloaded .celerp file)
on the documents import page; the UI calls the API with the user's session and
opens the received document.
"""

import io

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
    def __init__(self, form: dict, method: str = "POST"):
        self._form = form
        self.method = method
        self.cookies: dict = {}
        self.query_params: dict = {}

    async def form(self):
        return self._form


class _Upload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.file = io.BytesIO(content)
        self.size = len(content)


async def _fake_base_shell(*content, title="", **kwargs):
    return Div(Span(title), Div(*content))


@pytest.fixture
def ui_routes(monkeypatch):
    monkeypatch.setattr(di, "base_shell", _fake_base_shell)
    monkeypatch.setattr(di, "_token", lambda request: "tok")
    return _routes()


@pytest.mark.parametrize("link, expected", [
    ("https://www.celerp.com/accept?src=https%3A%2F%2Fshop.example.com&token=abc123",
     "https://shop.example.com/share/abc123"),
    ("https://www.celerp.com/accept?src=https%3A%2F%2Fshop.example.com%2F&token=abc123",
     "https://shop.example.com/share/abc123"),
    ("https://shop.example.com/share/abc123", "https://shop.example.com/share/abc123"),
    ("https://example.com/celerp/share/abc123/", "https://example.com/celerp/share/abc123"),
    ("  https://shop.example.com/share/abc123  ", "https://shop.example.com/share/abc123"),
    ("https://share.celerp.com/eyJTWU5USEVUSUMtbGluayJ9", "https://share.celerp.com/eyJTWU5USEVUSUMtbGluayJ9"),
    ("https://share.celerp.com/eyJTWU5USEVUSUMtbGluayJ9/", "https://share.celerp.com/eyJTWU5USEVUSUMtbGluayJ9"),
    ("https://www.celerp.com/accept?link=https%3A%2F%2Fshop.example.com%2Fshare%2Fabc123",
     "https://shop.example.com/share/abc123"),
    ("https://www.celerp.com/accept?link=https%3A%2F%2Fshare.celerp.com%2FeyJTWU5USEVUSUMtbGluayJ9",
     "https://share.celerp.com/eyJTWU5USEVUSUMtbGluayJ9"),
])
def test_parse_share_link_accepts_every_link_form(link, expected):
    assert di.parse_share_link(link) == expected


@pytest.mark.parametrize("link", [
    "",
    "abc123",
    "ftp://shop.example.com/share/abc123",
    "https://shop.example.com/docs/abc123",
    "https://shop.example.com/share/",
    "https://shop.example.com/share/abc123/bundle",
    "https://www.celerp.com/accept?token=abc123",
    "https://share.celerp.com/",
    "https://share.celerp.com/abc/def",
    "https://shop.example.com/abc123",
    "https://www.celerp.com/accept?link=https%3A%2F%2Fshop.example.com%2Fdocs%2F1",
    "https://www.celerp.com/accept?link=javascript%3Aalert(1)",
    "https://user:pass@shop.example.com/share/abc123",
    "https://www.celerp.com/accept?link=https%3A%2F%2Fuser%3Apass%40shop.example.com%2Fshare%2Fabc",
    "https://www.celerp.com/accept?src=javascript%3Aalert(1)&token=abc",
    "https://www.celerp.com/accept?src=https%3A%2F%2Fuser%3Apass%40shop.example.com&token=abc",
    "https://www.celerp.com/accept?link=https%3A%2F%2Fshop.example.com%2Fshare%2Fabc&src=https%3A%2F%2Fx.example.com&token=t",
    "https://www.celerp.com/accept?link=https%3A%2F%2Fwww.celerp.com%2Faccept%3Flink%3Dhttps%253A%252F%252Fshop.example.com%252Fshare%252Fabc",
])
def test_parse_share_link_rejects_other_links(link):
    assert di.parse_share_link(link) is None


@pytest.mark.asyncio
async def test_import_page_offers_shared_import(ui_routes):
    page = await ui_routes[("GET", "/docs/import")](_FormReq({}, "GET"))
    html = to_xml(page)
    assert 'action="/docs/import/shared"' in html
    assert 'action="/docs/import/shared-file"' in html
    assert 'name="link"' in html
    assert 'name="bundle"' in html


@pytest.mark.asyncio
async def test_import_page_fills_the_link_it_was_handed(ui_routes, monkeypatch):
    async def _never(*a, **k):
        raise AssertionError("Opening the page must not import anything")

    monkeypatch.setattr(di.api, "import_shared_doc", _never)
    page = await ui_routes[("GET", "/docs/import")](_FormReq({}, "GET"), link="https://shop.example.com/share/abc123")
    html = to_xml(page)
    assert 'value="https://shop.example.com/share/abc123"' in html


@pytest.mark.asyncio
async def test_shared_link_imports_and_opens_the_received_doc(ui_routes, monkeypatch):
    calls = []

    async def _fake_import(token, share_page):
        calls.append((token, share_page))
        return "/docs/received/rcv:abc"

    monkeypatch.setattr(di.api, "import_shared_doc", _fake_import)
    handler = ui_routes[("POST", "/docs/import/shared")]
    resp = await handler(_FormReq({"link": "https://shop.example.com/share/abc123"}))

    assert calls == [("tok", "https://shop.example.com/share/abc123")]
    assert resp.status_code == 303
    assert resp.headers["location"] == "/docs/received/rcv:abc"


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
        calls.append((token, filename, content.read()))
        return "/docs/received/rcv:def"

    monkeypatch.setattr(di.api, "import_doc_bundle", _fake_bundle)
    handler = ui_routes[("POST", "/docs/import/shared-file")]
    resp = await handler(_FormReq({"bundle": _Upload("INV-1.celerp", b'{"doc": {}}')}))

    assert calls == [("tok", "INV-1.celerp", b'{"doc": {}}')]
    assert resp.status_code == 303
    assert resp.headers["location"] == "/docs/received/rcv:def"


@pytest.mark.asyncio
async def test_missing_celerp_file_explains(ui_routes, monkeypatch):
    async def _never(*a, **k):
        raise AssertionError("API must not be called without a file")

    monkeypatch.setattr(di.api, "import_doc_bundle", _never)
    handler = ui_routes[("POST", "/docs/import/shared-file")]
    html = to_xml(await handler(_FormReq({})))
    assert "Choose a .celerp file" in html

    html = to_xml(await handler(_FormReq({"bundle": _Upload("empty.celerp", b"")})))
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
            return httpx.Response(302, headers={"location": "/docs/received/rcv:abc"})
        return httpx.Response(200, json={"id": "doc:rcv:abc"})

    seen = _mock_api(monkeypatch, _handler)
    path = await api.import_shared_doc("tok", "https://shop.example.com/share/abc123")

    assert path == "/docs/received/rcv:abc"
    assert [r.url.path for r in seen] == ["/docs/import"]
    assert seen[0].url.params["link"] == "https://shop.example.com/share/abc123"
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_api_client_uploads_the_bundle(monkeypatch):
    def _handler(request):
        return httpx.Response(302, headers={"location": "/docs/received/rcv:def"})

    seen = _mock_api(monkeypatch, _handler)
    path = await api.import_doc_bundle("tok", "INV-1.celerp", io.BytesIO(b'{"doc": {"doc_type": "invoice"}}'))

    assert path == "/docs/received/rcv:def"
    assert seen[0].method == "POST" and seen[0].url.path == "/docs/import-bundle"
    assert b'name="bundle"' in seen[0].content
    assert b'{"doc": {"doc_type": "invoice"}}' in seen[0].content


@pytest.mark.asyncio
async def test_api_client_raises_the_api_error(monkeypatch):
    _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Share link not found on sender's instance"}))

    with pytest.raises(APIError) as exc:
        await api.import_shared_doc("tok", "https://shop.example.com/share/gone")
    assert exc.value.status == 404
    assert "not found" in exc.value.detail


def _received(**over) -> dict:
    r = {
        "id": "rcv:abc", "sender_name": "Sender Ltd", "doc_type": "invoice",
        "sender_doc_number": "INV-0042", "issue_date": "2026-09-01", "due_date": None,
        "total": 1250.0, "currency": "USD", "first_received_at": "2026-09-02T10:00:00+00:00",
        "last_received_at": "2026-09-03T10:00:00+00:00", "revision_count": 2,
        "revision_state": "unbooked", "book_target": {"kind": "doc", "type": "bill"},
        "booked_id": None, "booked_kind": None,
        "source_link": "https://shop.example.com/share/abc123", "booked_status": None,
        "document": {"currency": "USD", "line_items": [
            {"name": "Emerald ring", "quantity": 2, "unit_price": 625.0, "line_total": 1250.0}]},
        "revisions": [
            {"received_at": "2026-09-03T10:00:00+00:00", "doc_number": "INV-0042", "sender_revision": 2, "total": 1250.0},
            {"received_at": "2026-09-02T10:00:00+00:00", "doc_number": "INV-0042", "sender_revision": 1, "total": 1000.0},
        ],
    }
    r.update(over)
    return r


@pytest.mark.asyncio
async def test_received_list_shows_the_inbox_columns(ui_routes, monkeypatch):
    async def _list(token):
        return [_received()]

    monkeypatch.setattr(di.api, "list_received", _list)
    html = to_xml(await ui_routes[("GET", "/docs/received")](_FormReq({}, "GET")))

    assert 'href="/docs/received/rcv:abc"' in html and "Sender Ltd" in html
    assert "INV-0042" in html and "2026-09-01" in html and "2026-09-03" in html
    assert "Not booked" in html
    # The missing due date reads as "--", never blank.
    assert "<td>--</td>" in html
    assert 'href="/docs/import#shared-import"' in html


@pytest.mark.asyncio
async def test_received_list_empty_state(ui_routes, monkeypatch):
    async def _list(token):
        return []

    monkeypatch.setattr(di.api, "list_received", _list)
    html = to_xml(await ui_routes[("GET", "/docs/received")](_FormReq({}, "GET")))
    assert "Nothing received yet" in html


@pytest.mark.asyncio
async def test_received_list_shows_an_api_failure(ui_routes, monkeypatch):
    async def _fail(token):
        raise APIError(500, "Could not load received documents")

    monkeypatch.setattr(di.api, "list_received", _fail)
    html = to_xml(await ui_routes[("GET", "/docs/received")](_FormReq({}, "GET")))
    assert "Could not load received documents" in html


async def _detail(ui_routes, monkeypatch, **over) -> str:
    async def _get(token, rid):
        assert rid == "rcv:abc"
        return _received(**over)

    monkeypatch.setattr(di.api, "get_received", _get)
    return to_xml(await ui_routes[("GET", "/docs/received/{rid}")](_FormReq({}, "GET"), "rcv:abc"))


@pytest.mark.asyncio
async def test_received_detail_offers_book_and_shows_history(ui_routes, monkeypatch):
    html = await _detail(ui_routes, monkeypatch)
    assert 'action="/docs/received/rcv:abc/book"' in html
    assert "Bill draft" in html
    assert "Emerald ring" in html
    revisions = html[html.index('id="received-revisions"'):]
    assert revisions.index("2026-09-03") < revisions.index("2026-09-02")
    assert 'href="https://shop.example.com/share/abc123"' in html and 'rel="noopener noreferrer"' in html


@pytest.mark.parametrize("state, text", [
    ("not_bookable", "cannot be booked"),
    ("needs_reconciliation", "update the draft by hand"),
    ("source_changed", "cannot follow automatically"),
    ("review_only", "no longer a draft"),
])
@pytest.mark.asyncio
async def test_received_detail_explains_states_without_a_book_action(ui_routes, monkeypatch, state, text):
    html = await _detail(ui_routes, monkeypatch, revision_state=state, booked_id=None if state == "not_bookable" else "doc:1")
    assert text in html
    assert "/book" not in html and "/update-draft" not in html


@pytest.mark.parametrize("state", ["needs_reconciliation", "source_changed"])
@pytest.mark.asyncio
async def test_received_detail_offers_mark_reconciled_when_it_cannot_update(ui_routes, monkeypatch, state):
    html = await _detail(ui_routes, monkeypatch, revision_state=state, booked_id="doc:1")
    assert 'action="/docs/received/rcv:abc/mark-reconciled"' in html
    assert "Mark reconciled" in html


@pytest.mark.parametrize("state", ["unbooked", "booked", "update_available", "review_only"])
@pytest.mark.asyncio
async def test_mark_reconciled_is_offered_only_when_update_cannot_apply(ui_routes, monkeypatch, state):
    html = await _detail(ui_routes, monkeypatch, revision_state=state,
                         booked_id=None if state == "unbooked" else "doc:1")
    assert "/mark-reconciled" not in html


@pytest.mark.asyncio
async def test_mark_reconciled_opens_the_draft(ui_routes, monkeypatch):
    calls = []

    async def _mark(token, rid):
        calls.append(rid)
        return {"id": "doc:1", "kind": "doc"}

    monkeypatch.setattr(di.api, "mark_received_reconciled", _mark)
    resp = await ui_routes[("POST", "/docs/received/{rid}/mark-reconciled")](_FormReq({}), "rcv:abc")
    assert calls == ["rcv:abc"]
    assert resp.status_code == 303 and resp.headers["location"] == "/docs/doc:1"


@pytest.mark.asyncio
async def test_received_detail_offers_update_draft(ui_routes, monkeypatch):
    html = await _detail(ui_routes, monkeypatch, revision_state="update_available", booked_id="doc:1")
    assert 'action="/docs/received/rcv:abc/update-draft"' in html
    assert 'href="/docs/doc:1"' in html
    assert "/book" not in html


@pytest.mark.asyncio
async def test_received_detail_never_links_a_non_web_source(ui_routes, monkeypatch):
    html = await _detail(ui_routes, monkeypatch, source_link="javascript:alert(1)")
    assert "javascript:" not in html


@pytest.mark.asyncio
async def test_book_opens_the_new_draft(ui_routes, monkeypatch):
    async def _book(token, rid):
        return {"id": "doc:new"}

    monkeypatch.setattr(di.api, "book_received", _book)
    resp = await ui_routes[("POST", "/docs/received/{rid}/book")](_FormReq({}), "rcv:abc")
    assert resp.status_code == 303 and resp.headers["location"] == "/docs/doc:new"


@pytest.mark.asyncio
async def test_booked_list_opens_on_the_list_page(ui_routes, monkeypatch):
    async def _book(token, rid):
        return {"id": "list:Q-1", "kind": "list"}

    monkeypatch.setattr(di.api, "book_received", _book)
    resp = await ui_routes[("POST", "/docs/received/{rid}/book")](_FormReq({}), "rcv:abc")
    assert resp.status_code == 303 and resp.headers["location"] == "/lists/list:Q-1"
    html = await _detail(ui_routes, monkeypatch, doc_type="purchase_order", revision_state="booked",
                         book_target={"kind": "list", "type": "quotation"},
                         booked_id="list:Q-1", booked_kind="list")
    assert 'href="/lists/list:Q-1"' in html and 'href="/docs/list:Q-1"' not in html


@pytest.mark.asyncio
async def test_book_failure_is_explained_on_the_received_page(ui_routes, monkeypatch):
    async def _book(token, rid):
        raise APIError(422, "This document type is kept in Received for review and cannot be booked.")

    async def _get(token, rid):
        return _received()

    monkeypatch.setattr(di.api, "book_received", _book)
    monkeypatch.setattr(di.api, "get_received", _get)
    html = to_xml(await ui_routes[("POST", "/docs/received/{rid}/book")](_FormReq({}), "rcv:abc"))
    assert "kept in Received for review" in html and "flash--error" in html


@pytest.mark.asyncio
async def test_update_draft_opens_the_draft(ui_routes, monkeypatch):
    async def _update(token, rid):
        return {"id": "doc:1"}

    monkeypatch.setattr(di.api, "update_received_draft", _update)
    resp = await ui_routes[("POST", "/docs/received/{rid}/update-draft")](_FormReq({}), "rcv:abc")
    assert resp.status_code == 303 and resp.headers["location"] == "/docs/doc:1"


@pytest.mark.asyncio
async def test_api_client_received_calls(monkeypatch):
    def _handler(request):
        if request.method == "GET" and request.url.path == "/docs/received":
            return httpx.Response(200, json={"items": [{"id": "rcv:abc"}]})
        if request.method == "GET":
            return httpx.Response(200, json={"id": "rcv:abc"})
        return httpx.Response(200, json={"id": "doc:1"})

    seen = _mock_api(monkeypatch, _handler)
    assert await api.list_received("tok") == [{"id": "rcv:abc"}]
    assert (await api.get_received("tok", "rcv:abc"))["id"] == "rcv:abc"
    assert await api.book_received("tok", "rcv:abc") == {"id": "doc:1"}
    assert await api.update_received_draft("tok", "rcv:abc") == {"id": "doc:1"}
    assert await api.mark_received_reconciled("tok", "rcv:abc") == {"id": "doc:1"}
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/docs/received"), ("GET", "/docs/received/rcv:abc"),
        ("POST", "/docs/received/rcv:abc/book"), ("POST", "/docs/received/rcv:abc/update-draft"),
        ("POST", "/docs/received/rcv:abc/mark-reconciled"),
    ]
