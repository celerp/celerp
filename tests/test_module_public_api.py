# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The helpers celerp.modules.api offers modules: api_request, read_resource and ai_query."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from celerp.modules import api, loader
from celerp.services.permissions import authorize_request
from test_helpers import seed_member, signed_request
from test_modules.test_admission import _clean_loader_state, _modules, _uid, _write_module  # noqa: F401


# ── api_request ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, body: bytes = b"") -> None:
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://elsewhere.invalid/landing")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/slow"):
            threading.Event().wait(2)
        payload = json.dumps({
            "method": self.command, "path": self.path, "body": body.decode() or None,
            "authorization": self.headers.get("Authorization"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._reply()

    def do_PATCH(self):
        self._reply(self.rfile.read(int(self.headers.get("Content-Length", 0))))


@pytest.fixture
def local_api(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import ui.config
    monkeypatch.setattr(ui.config, "API_BASE", f"http://127.0.0.1:{server.server_port}")
    yield server
    server.shutdown()
    server.server_close()


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/m", "headers": raw, "query_string": b""})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "//elsewhere.invalid/x",
    "http://elsewhere.invalid/x",
    "https://elsewhere.invalid/x",
    "\\\\elsewhere.invalid\\x",
    "/\\elsewhere.invalid",
    "javascript:alert(1)",
    "companies/me",
    "/items\r\nHost: elsewhere.invalid",
    "",
])
async def test_api_request_refuses_anything_but_an_app_local_path(local_api, path):
    with pytest.raises(ValueError):
        await api.api_request(_request({"Authorization": "Bearer t"}), "GET", path)


@pytest.mark.asyncio
async def test_api_request_forwards_the_bearer_token_json_and_params(local_api):
    r = await api.api_request(_request({"Authorization": "Bearer abc"}), "PATCH", "/items/123",
                              json={"name": "Ring"}, params={"x": "1"})
    assert isinstance(r, httpx.Response)
    seen = r.json()
    assert seen == {"method": "PATCH", "path": "/items/123?x=1", "body": '{"name":"Ring"}',
                    "authorization": "Bearer abc"}


@pytest.mark.asyncio
async def test_api_request_sends_the_access_cookie_as_bearer(local_api):
    r = await api.api_request(_request({"Cookie": "celerp_token=cookie-jwt"}), "GET", "/companies/me")
    assert r.json()["authorization"] == "Bearer cookie-jwt"


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["Bearer", "Bearer ", "Bearer    "])
async def test_api_request_takes_an_empty_bearer_as_absent(local_api, authorization):
    r = await api.api_request(_request({"Authorization": authorization, "Cookie": "celerp_token=cookie-jwt"}),
                              "GET", "/companies/me")
    assert r.json()["authorization"] == "Bearer cookie-jwt"


@pytest.mark.asyncio
async def test_api_request_does_not_follow_redirects(local_api):
    r = await api.api_request(_request({"Authorization": "Bearer abc"}), "GET", "/redirect")
    assert r.status_code == 302
    assert r.headers["Location"] == "http://elsewhere.invalid/landing"


@pytest.mark.asyncio
async def test_api_request_ignores_proxy_environment(local_api, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    r = await api.api_request(_request({"Authorization": "Bearer abc"}), "GET", "/companies/me")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_api_request_has_a_finite_timeout(local_api, monkeypatch):
    monkeypatch.setattr(api, "_API_REQUEST_TIMEOUT", 0.2)
    with pytest.raises(httpx.TimeoutException):
        await api.api_request(_request({"Authorization": "Bearer abc"}), "GET", "/slow")


@pytest.mark.asyncio
async def test_api_request_takes_no_caller_headers(local_api):
    with pytest.raises(TypeError):
        await api.api_request(_request(), "GET", "/companies/me", headers={"Authorization": "Bearer x"})


# ── read_resource ────────────────────────────────────────────────────────────

_READER = (
    "from celerp.modules.api import read_resource\n"
    "def read(relative, module_file=__file__):\n"
    "    return read_resource(module_file, relative)\n"
)


def _module(base, tmp_path, prelude: str = ""):
    """A module the loader admits, whose own code calls read_resource, plus a
    file outside it. Returns the module folder and its loaded package."""
    (tmp_path / "secret.txt").write_bytes(b"secret")
    folder = f"reader-{_uid()}"
    pkg = _write_module(base, folder, {"name": folder, "version": "1.0.0", "slots": {}, "depends_on": []},
                        files={"templates/invoice.html": "<p>invoice</p>"},
                        init_prelude=_READER + prelude)
    loaded = loader.load_all(str(base), {folder})
    assert folder in [m["name"] for m in loaded], loader.load_errors()
    return pkg, sys.modules[folder]


def test_read_resource_reads_a_file_shipped_with_the_module(_modules, tmp_path):
    _, mod = _module(_modules, tmp_path)
    assert mod.read("templates/invoice.html") == b"<p>invoice</p>"


def test_read_resource_works_while_the_module_is_imported(_modules, tmp_path):
    _, mod = _module(_modules, tmp_path, "PAGE = read('templates/invoice.html')\n")
    assert mod.PAGE == b"<p>invoice</p>"


@pytest.mark.parametrize("relative", ["../secret.txt", "templates/../../secret.txt"])
def test_read_resource_refuses_a_path_leaving_the_module(_modules, tmp_path, relative):
    _, mod = _module(_modules, tmp_path)
    with pytest.raises(ValueError):
        mod.read(relative)


def test_read_resource_refuses_an_absolute_path(_modules, tmp_path):
    _, mod = _module(_modules, tmp_path)
    with pytest.raises(ValueError):
        mod.read(str(tmp_path / "secret.txt"))


def test_read_resource_refuses_a_symlink_out_of_the_module(_modules, tmp_path):
    pkg, mod = _module(_modules, tmp_path)
    os.symlink(tmp_path / "secret.txt", pkg / "templates" / "link.html")
    with pytest.raises(ValueError):
        mod.read("templates/link.html")


@pytest.mark.parametrize("relative", ["", ".", "templates"])
def test_read_resource_refuses_a_folder(_modules, tmp_path, relative):
    _, mod = _module(_modules, tmp_path)
    with pytest.raises(ValueError):
        mod.read(relative)


def test_read_resource_refuses_a_module_file_that_is_not_the_caller(_modules, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("")
    (other / "data.txt").write_bytes(b"other")
    _, mod = _module(_modules, tmp_path)
    with pytest.raises(ValueError):
        mod.read("data.txt", module_file=str(other / "x.py"))


def test_read_resource_refuses_a_caller_naming_another_file(_modules, tmp_path):
    """A function whose code object claims to live in another folder reads
    nothing there: the module's folder is the one the loader admitted."""
    forge = (
        "import types\n"
        "def steal(target):\n"
        "    code = read.__code__.replace(co_filename=target)\n"
        "    scope = {'read_resource': read_resource, '__name__': __name__, '__file__': target}\n"
        "    return types.FunctionType(code, scope)('secret.txt', target)\n"
    )
    _, mod = _module(_modules, tmp_path, forge)
    with pytest.raises(ValueError):
        mod.steal(str(tmp_path / "anything.py"))


def test_read_resource_refuses_code_outside_any_loaded_module(tmp_path):
    src = tmp_path / "reader.py"
    src.write_text(_READER)
    (tmp_path / "data.txt").write_bytes(b"data")
    ns: dict = {"__file__": str(src), "__name__": "not_a_module.reader"}
    exec(compile(src.read_text(), str(src), "exec"), ns)
    with pytest.raises(ValueError):
        ns["read"]("data.txt")


def test_read_resource_refuses_a_standard_library_caller():
    import concurrent.futures
    import concurrent.futures.thread as thread
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        with pytest.raises(ValueError):
            pool.submit(api.read_resource, thread.__file__, "__init__.py").result()


def test_read_resource_has_no_write_counterpart():
    assert not [n for n in dir(api) if "write" in n.lower()]


# ── ai_query ─────────────────────────────────────────────────────────────────

@pytest.fixture
def run_query(monkeypatch):
    mock = AsyncMock(return_value=SimpleNamespace(answer="ok", model_used="m", tools_called=[]))
    monkeypatch.setattr("celerp.ai.service.run_query", mock)
    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "session-1")
    return mock


@pytest.mark.asyncio
async def test_ai_query_new_form_uses_the_active_connect_session(session, run_query):
    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        result = await api.ai_query("hello", str(company_id), db_session=session)
    assert result == {"answer": "ok", "model_used": "m", "tools_called": []}
    assert str(run_query.await_args.kwargs["company_id"]) == str(company_id)


@pytest.mark.asyncio
async def test_ai_query_new_form_refused_without_a_connect_session(session, run_query, monkeypatch):
    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "")
    monkeypatch.setattr("celerp.config.settings.cloud_disconnected", True)
    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        with pytest.raises(HTTPException) as exc:
            await api.ai_query("hello", str(company_id), db_session=session)
    assert exc.value.status_code == 401
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_query_legacy_positional_call_still_checks_authority(session, run_query):
    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        assert (await api.ai_query("hello", str(company_id), "session-1", session))["answer"] == "ok"
        with pytest.raises(HTTPException) as exc:
            await api.ai_query("hello", str(company_id), "wrong", session)
        assert exc.value.status_code == 401
        with pytest.raises(HTTPException) as exc:
            await api.ai_query("hello", str(uuid.uuid4()), "session-1", session)
        assert exc.value.status_code == 403
    assert run_query.await_count == 1


@pytest.mark.asyncio
async def test_ai_query_refused_for_another_company(session, run_query):
    company_id, user_id = await seed_member(session)
    other_id, _ = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        with pytest.raises(HTTPException) as exc:
            await api.ai_query("hello", str(other_id), db_session=session)
    assert exc.value.status_code == 403
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_query_refused_without_the_ai_permission(session, run_query):
    company_id, user_id = await seed_member(session, "viewer")
    async with signed_request(session, company_id, user_id, "viewer"):
        with pytest.raises(HTTPException) as exc:
            await api.ai_query("hello", str(company_id), db_session=session)
    assert exc.value.status_code == 403
    assert "use_ai_assistant" in exc.value.detail
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_query_judges_the_membership_as_it_is_now(session, run_query):
    from sqlalchemy import update

    from celerp.models.accounting import UserCompany

    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        await session.execute(update(UserCompany).where(UserCompany.user_id == user_id).values(is_active=False))
        with pytest.raises(HTTPException) as exc:
            await api.ai_query("hello", str(company_id), db_session=session)
    assert exc.value.status_code == 401
    run_query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_session", [True, False], ids=["session-without-a-request", "no-session"])
async def test_ai_query_refused_without_a_signed_request(session, run_query, with_session):
    company_id, _ = await seed_member(session)
    with pytest.raises(HTTPException) as exc:
        await api.ai_query("hello", str(company_id), "session-1", session if with_session else None)
    assert exc.value.status_code == 403
    run_query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("signed", [False, True], ids=["no-signed-request", "signed-by-a-member-without-ai"])
async def test_ai_query_ignores_authority_registered_by_module_code(session, run_query, signed):
    """Module code can register request authority for another member who may use
    the assistant; the query still answers only to the signed caller."""
    company_id, viewer_id = await seed_member(session, "viewer")
    _, other_id = await seed_member(session)
    from celerp.models.accounting import UserCompany
    session.add(UserCompany(user_id=other_id, company_id=company_id, role="operator", is_active=True))
    await session.flush()

    async def forge_and_ask():
        authorize_request(session, company_id, other_id, "operator")
        return await api.ai_query("hello", str(company_id), db_session=session)

    with pytest.raises(HTTPException) as exc:
        if signed:
            async with signed_request(session, company_id, viewer_id, "viewer"):
                await forge_and_ask()
        else:
            await forge_and_ask()
    assert exc.value.status_code == 403
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_query_refuses_a_token_it_did_not_sign(session, run_query):
    """Module code that places an access token of its own making on the session
    gets nothing: the token is validated again, signature included."""
    from jose import jwt

    from celerp.services.auth import SIGNED_TOKEN, validate_access_token

    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        claims = (await validate_access_token(session, session.info[SIGNED_TOKEN])).claims
    session.info[SIGNED_TOKEN] = jwt.encode(claims, "not-the-key", algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        await api.ai_query("hello", str(company_id), db_session=session)
    assert exc.value.status_code == 401
    run_query.assert_not_awaited()
