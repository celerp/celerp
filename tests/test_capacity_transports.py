# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Truthful capacity constants and two bounded local UI transports.

One API worker owns one request DB pool, so the pool size and the web UI's
interactive connection ceiling must come from one shared source and stay equal.
A separate, smaller bulk transport carries the few long local operations so they
cannot starve interactive page traffic of connections.
"""
from __future__ import annotations

import contextlib

import httpx
import pytest

import celerp.capacity as capacity
import ui.api_client as api
from ui.api_client import APIError


def test_capacity_constants_are_the_pool_budget():
    assert capacity.REQUEST_DB_POOL_SIZE == 10
    assert capacity.REQUEST_DB_MAX_OVERFLOW == 5


def test_db_engine_pool_uses_capacity_constants():
    import inspect

    import celerp.db as db

    # The engine reads the shared constants, not literals, so its pool can never
    # drift from the UI's interactive ceiling. Under NullPool (test mode) the pool
    # has no fixed size to introspect, so assert the sized pool only when present
    # and always assert the source wires the constants through.
    pool = db.engine.pool
    size = getattr(pool, "size", None)
    if callable(size):
        assert pool.size() == capacity.REQUEST_DB_POOL_SIZE
        assert pool._max_overflow == capacity.REQUEST_DB_MAX_OVERFLOW
    src = inspect.getsource(db)
    assert "pool_size=REQUEST_DB_POOL_SIZE" in src
    assert "max_overflow=REQUEST_DB_MAX_OVERFLOW" in src
    # The stale multi-worker pool arithmetic must not survive.
    assert "gui_workers" not in src
    assert "2 API" not in src


def _limits_of(transport: httpx.AsyncHTTPTransport) -> httpx.Limits:
    # The transport keeps its pool on _pool; the configured limits are readable
    # from the pool's max-connection attributes.
    pool = transport._pool
    return pool


def test_interactive_transport_ceiling_is_pool_size():
    transport = api._get_transport()
    pool = _limits_of(transport)
    assert pool._max_connections == capacity.REQUEST_DB_POOL_SIZE


def test_bulk_transport_is_separate_and_small():
    interactive = api._get_transport()
    bulk = api._get_bulk_transport()
    assert bulk is not interactive
    assert _limits_of(bulk)._max_connections == 2


def test_local_client_factory_preserves_redirect_choice():
    # The factory must not force follow_redirects: a proxy route that inspects a
    # raw redirect status needs follow_redirects=False preserved.
    no_follow = api._local_client(follow_redirects=False)
    try:
        assert no_follow.follow_redirects is False
    finally:
        pass
    follow = api._local_client(follow_redirects=True)
    assert follow.follow_redirects is True


def test_local_client_bulk_uses_bulk_transport():
    c = api._local_client(bulk=True)
    assert c._transport is api._get_bulk_transport()
    interactive = api._local_client(bulk=False)
    assert interactive._transport is api._get_transport()


def _raising_client(exc):
    """A drop-in for _client/_anon_client whose context entry raises *exc*."""
    @contextlib.asynccontextmanager
    async def _cm(*_a, **_k):
        raise exc
        yield  # pragma: no cover - unreachable, keeps this an async generator

    return _cm


@pytest.mark.asyncio
async def test_pool_saturation_maps_to_503_not_504(monkeypatch):
    # A pool-acquire timeout means every interactive slot is busy: the app is
    # saturated. It must surface as 503 (retryable), distinct from a genuine
    # upstream 504 timeout - PoolTimeout subclasses TimeoutException, so the
    # order of the except branches is what makes this correct.
    monkeypatch.setattr(api, "_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._api_client("tok") as _c:
            pass
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_upstream_timeout_still_maps_to_504(monkeypatch):
    # A non-pool timeout (the request itself was slow) keeps its 504 meaning.
    monkeypatch.setattr(api, "_client", _raising_client(httpx.ReadTimeout("slow")))
    with pytest.raises(APIError) as exc:
        async with api._api_client("tok") as _c:
            pass
    assert exc.value.status == 504


@pytest.mark.asyncio
async def test_anon_pool_saturation_maps_to_503(monkeypatch):
    monkeypatch.setattr(api, "_anon_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._anon_api_client() as _c:
            pass
    assert exc.value.status == 503


# --- Section 2: the pool-acquire bound is real on every interactive client ---


def test_client_pool_timeout_is_two_seconds():
    # The plain authenticated wrapper factory must carry the finite pool-acquire
    # bound, not an unbounded default that lets a saturated pool hang.
    assert api._client("tok").timeout.pool == 2.0


def test_anon_client_pool_timeout_is_two_seconds():
    assert api._anon_client().timeout.pool == 2.0


def test_local_client_float_timeout_forces_pool_two_seconds():
    c = api._local_client("tok", timeout=7.0)
    assert c.timeout.pool == 2.0


def test_local_client_preconstructed_timeout_forces_pool_but_keeps_the_rest():
    # A caller that hands in a fully specified httpx.Timeout must still get the
    # local pool bound forced, while connect/read/write are preserved exactly.
    t = httpx.Timeout(connect=1.0, read=3.0, write=4.0, pool=30.0)
    c = api._local_client("tok", timeout=t)
    assert c.timeout.pool == 2.0
    assert c.timeout.connect == 1.0
    assert c.timeout.read == 3.0
    assert c.timeout.write == 4.0


@pytest.mark.asyncio
async def test_ai_api_client_uses_interactive_transport_and_pool_bound():
    async with api._ai_api_client("tok", "sess") as c:
        assert c._transport is api._get_transport()
        assert c.timeout.pool == 2.0


@pytest.mark.asyncio
async def test_bulk_api_client_uses_bulk_transport_and_pool_bound():
    async with api._bulk_api_client("tok") as c:
        assert c._transport is api._get_bulk_transport()
        assert c.timeout.pool == 2.0


@pytest.mark.asyncio
async def test_ai_pool_saturation_maps_to_503(monkeypatch):
    monkeypatch.setattr(api, "_local_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._ai_api_client("tok", "sess") as _c:
            pass
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_bulk_pool_saturation_maps_to_503(monkeypatch):
    monkeypatch.setattr(api, "_local_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._bulk_api_client("tok") as _c:
            pass
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_bulk_read_timeout_maps_to_504(monkeypatch):
    monkeypatch.setattr(api, "_local_client", _raising_client(httpx.ReadTimeout("slow")))
    with pytest.raises(APIError) as exc:
        async with api._bulk_api_client("tok") as _c:
            pass
    assert exc.value.status == 504


def test_configured_client_has_a_finite_pool_bound():
    # The concrete proof that a saturated pool fails fast rather than hanging: the
    # client the factory actually builds carries a finite 2.0 second pool-acquire
    # timeout, which is exactly the bound httpx applies when every connection is
    # busy. (A live-socket saturation drive is left to the manual gate in 14A;
    # httpcore's pool-acquire wait is not deterministically observable in-process.)
    for c in (api._client("tok"), api._anon_client(), api._local_client("tok")):
        assert c.timeout.pool == 2.0
        assert c.timeout.pool is not None


def test_bulk_and_interactive_transports_are_distinct():
    assert api._get_transport() is not api._get_bulk_transport()


# --- Section 10: every local file-body transfer selects the bulk transport ---

# Every wrapper that moves a finite file body (upload or download) over the local
# API must run on the small bulk pool, never the interactive one, so a large
# transfer can never hold an interactive connection slot. Metadata-only wrappers
# (tag/describe/delete/hero) stay interactive and are deliberately excluded.
_FILE_BODY_WRAPPERS = [
    "upload_attachment",
    "upload_item_file",
    "download_item_file",
    "bulk_attach",
    "upload_contact_file",
    "download_contact_file",
    "upload_doc_file",
    "download_doc_file",
    "import_recon_csv",
    "attach_recon_line",
    "import_module_zip",
    "export_items_csv",
    "export_contacts_csv",
]

_METADATA_ONLY_FILE_WRAPPERS = [
    "tag_item_file",
    "describe_item_file",
    "delete_item_file",
    "tag_contact_file",
    "patch_contact_file_description",
    "delete_contact_file",
    "tag_doc_file",
    "patch_doc_file_description",
    "delete_doc_file",
]


class _FakeUploadFile:
    """A drop-in for the multipart-upload object the file wrappers accept.

    Matches the shape the wrappers actually read: `.filename`, `.content_type`,
    and an async `.read()` (the `hasattr(file, "read")` branch every upload
    wrapper takes).
    """

    def __init__(self, filename="upload.bin", content=b"data", content_type="application/octet-stream"):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self):
        return self._content


class _FakeAsyncClient:
    """Answers every verb a wrapper calls with a stock success response.

    A real httpx.Response (not a hand-rolled stand-in) so `_raise` and the
    wrappers' own `.json()` / `.content` reads work exactly as they do against
    a live client.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, *_a, **_k):
        return httpx.Response(200, json={"ok": True})

    async def post(self, *_a, **_k):
        return httpx.Response(200, json={"ok": True})

    async def patch(self, *_a, **_k):
        return httpx.Response(200, json={"ok": True})

    async def delete(self, *_a, **_k):
        return httpx.Response(200, json={"ok": True})

    async def put(self, *_a, **_k):
        return httpx.Response(200, json={"ok": True})


def _spy_client_factories(monkeypatch):
    """Replace the three local client-factory functions with counting spies.

    Each spy yields a `_FakeAsyncClient` and increments a counter for the
    factory (and, for the AI factory, the bulk/interactive branch) it was
    entered through. This observes which transport a wrapper's code actually
    runs on, instead of inferring it from the wrapper's source text.
    """
    entered = {"interactive": 0, "bulk": 0, "ai_interactive": 0, "ai_bulk": 0}

    @contextlib.asynccontextmanager
    async def _interactive(*_a, **_k):
        entered["interactive"] += 1
        yield _FakeAsyncClient()

    @contextlib.asynccontextmanager
    async def _bulk(*_a, **_k):
        entered["bulk"] += 1
        yield _FakeAsyncClient()

    @contextlib.asynccontextmanager
    async def _ai(*_a, bulk=False, **_k):
        entered["ai_bulk" if bulk else "ai_interactive"] += 1
        yield _FakeAsyncClient()

    monkeypatch.setattr(api, "_api_client", _interactive)
    monkeypatch.setattr(api, "_bulk_api_client", _bulk)
    monkeypatch.setattr(api, "_ai_api_client", _ai)
    return entered


def _file_body_call_recipes(token: str) -> dict:
    """One zero-argument async call per file-body wrapper, valid arguments only."""
    return {
        "upload_attachment": lambda: api.upload_attachment(token, "e1", _FakeUploadFile()),
        "upload_item_file": lambda: api.upload_item_file(token, "e1", _FakeUploadFile()),
        "download_item_file": lambda: api.download_item_file(token, "e1", "f1"),
        "bulk_attach": lambda: api.bulk_attach(token, _FakeUploadFile(filename="a.zip")),
        "upload_contact_file": lambda: api.upload_contact_file(token, "c1", b"data", "f.txt", "text/plain"),
        "download_contact_file": lambda: api.download_contact_file(token, "c1", "f1"),
        "upload_doc_file": lambda: api.upload_doc_file(token, "e1", b"data", "f.txt", "text/plain"),
        "download_doc_file": lambda: api.download_doc_file(token, "e1", "f1"),
        "import_recon_csv": lambda: api.import_recon_csv(token, "s1", b"csv", "f.csv"),
        "attach_recon_line": lambda: api.attach_recon_line(token, "s1", "l1", b"data", "f.bin"),
        "import_module_zip": lambda: api.import_module_zip(token, "mod.zip", b"zip"),
        "export_items_csv": lambda: api.export_items_csv(token),
        "export_contacts_csv": lambda: api.export_contacts_csv(token),
    }


def _metadata_only_call_recipes(token: str) -> dict:
    """One zero-argument async call per metadata-only file wrapper."""
    return {
        "tag_item_file": lambda: api.tag_item_file(token, "e1", "f1", "tag"),
        "describe_item_file": lambda: api.describe_item_file(token, "e1", "f1", "desc"),
        "delete_item_file": lambda: api.delete_item_file(token, "e1", "f1"),
        "tag_contact_file": lambda: api.tag_contact_file(token, "c1", "f1", "tag"),
        "patch_contact_file_description": lambda: api.patch_contact_file_description(token, "c1", "f1", "desc"),
        "delete_contact_file": lambda: api.delete_contact_file(token, "c1", "f1"),
        "tag_doc_file": lambda: api.tag_doc_file(token, "e1", "f1", "tag"),
        "patch_doc_file_description": lambda: api.patch_doc_file_description(token, "e1", "f1", "desc"),
        "delete_doc_file": lambda: api.delete_doc_file(token, "e1", "f1"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _FILE_BODY_WRAPPERS)
async def test_file_body_wrapper_uses_bulk_transport(name, monkeypatch):
    entered = _spy_client_factories(monkeypatch)
    await _file_body_call_recipes("tok")[name]()
    assert entered["bulk"] == 1, f"{name} must use the bulk transport"
    assert entered["interactive"] == 0, f"{name} must not use the interactive transport"


@pytest.mark.asyncio
async def test_ai_client_can_select_bulk_transport_and_keeps_session():
    # The AI client normally rides the interactive transport, but an AI file
    # upload transfers a finite body and must be able to take the bulk pool while
    # still carrying the session token and the local pool-acquire bound.
    async with api._ai_api_client("tok", "sess", bulk=True) as c:
        assert c._transport is api._get_bulk_transport()
        assert c.headers.get("X-Session-Token") == "sess"
        assert c.timeout.pool == 2.0


@pytest.mark.asyncio
async def test_ai_file_upload_uses_bulk_transport(monkeypatch):
    # ai_upload posts a user-supplied file body to the local API; it must select
    # the bulk pool so a large upload never holds an interactive connection slot.
    entered = _spy_client_factories(monkeypatch)
    await api.ai_upload("tok", "sess", [("f.txt", b"data", "text/plain")])
    assert entered["ai_bulk"] == 1, "ai_upload must transfer its file body on the bulk pool"
    assert entered["ai_interactive"] == 0, "ai_upload must not run its upload on the interactive branch"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _METADATA_ONLY_FILE_WRAPPERS)
async def test_metadata_only_file_wrappers_stay_interactive(name, monkeypatch):
    # A file's tag/description/delete carry no body: they belong on the
    # interactive transport. This pins the boundary so a future edit cannot
    # quietly push small metadata calls onto the bulk pool (or vice versa).
    entered = _spy_client_factories(monkeypatch)
    await _metadata_only_call_recipes("tok")[name]()
    assert entered["interactive"] == 1, f"{name} must stay interactive"
    assert entered["bulk"] == 0, f"{name} must not use the bulk transport"
