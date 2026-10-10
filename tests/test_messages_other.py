# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Messages from shared services, the AI assistant and the app shell say what went
wrong and what to do, in the user's language, without internal ids."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from ui.i18n import t

_ROOT = Path(__file__).resolve().parents[1]
_LOCALES = _ROOT / "ui" / "locales"
_LANGS = ("en", "th", "de", "fr", "es", "it", "pt", "id", "vi", "ja", "ar", "am")


def _en(key: str) -> str:
    return json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))[key]


async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "OtherMsgCo", "email": f"oth-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# -- API-wide handlers ------------------------------------------------------

@pytest.mark.asyncio
async def test_unhandled_error_says_try_again_and_where_to_ask():
    from celerp.main import unhandled_exception_handler
    from starlette.requests import Request
    req = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})
    resp = await unhandled_exception_handler(req, RuntimeError("boom"))
    assert json.loads(resp.body)["detail"] == t("error.server_error", "en")


@pytest.mark.asyncio
async def test_rate_limit_says_wait_a_minute():
    from celerp.main import rate_limit_handler
    resp = await rate_limit_handler(None, None)
    assert json.loads(resp.body)["detail"] == t("error.rate_limited", "en")


def _response(status: int, body=None, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {},
                          request=httpx.Request("GET", "http://api/x"))


def test_validation_list_becomes_a_sentence_naming_the_fields():
    from ui.api_client import APIError, _raise
    detail = [{"loc": ["body", "quantity"], "msg": "bad", "type": "x"},
              {"loc": ["body", "price"], "msg": "bad", "type": "x"}]
    with pytest.raises(APIError) as ei:
        _raise(_response(422, {"detail": detail}))
    assert ei.value.detail == t("error.invalid_input_fields", "en", fields="quantity, price")
    assert ei.value.data == {"detail": detail}


def test_unexpected_redirect_hides_the_address():
    from ui.api_client import APIError, _raise
    with pytest.raises(APIError) as ei:
        _raise(httpx.Response(302, headers={"location": "/internal/x"},
                              request=httpx.Request("GET", "http://api/x")))
    assert ei.value.detail == t("error.unexpected_redirect", "en")


# -- Shared services --------------------------------------------------------

def test_other_company_file_is_not_named_by_id(tmp_path, monkeypatch):
    from celerp.ai import files
    monkeypatch.setattr(files, "upload_dir", lambda: tmp_path)
    fid = "ai_up_" + uuid.uuid4().hex
    (tmp_path / f"{fid}.bin").write_bytes(b"x")
    (tmp_path / f"{fid}.meta").write_text(json.dumps({"company_id": "other"}))
    with pytest.raises(PermissionError) as ei:
        files.load_file(fid, uuid.uuid4())
    assert str(ei.value) == t("error.file_not_accessible", "en")


def test_attachment_size_and_type_say_what_to_attach():
    from celerp.services import attachments
    with pytest.raises(ValueError) as ei:
        attachments.check_file_size(attachments.MAX_FILE_BYTES + 1)
    assert str(ei.value) == t("error.file_too_large", "en", mb=attachments.MAX_FILE_BYTES // 1024 // 1024)


@pytest.mark.asyncio
async def test_bad_attachment_id_is_not_echoed():
    from celerp.services.attachments import LocalBackend
    with pytest.raises(ValueError) as ei:
        await LocalBackend().delete("c1", "../x", "image/png")
    assert str(ei.value) == t("error.attachment_not_found", "en")


def test_timezone_messages_point_at_the_list():
    from celerp.services.business_time import business_timezone
    with pytest.raises(ValueError) as ei:
        business_timezone(5)
    assert str(ei.value) == t("error.timezone_not_text", "en")
    with pytest.raises(ValueError) as ei:
        business_timezone("Mars/Base")
    assert str(ei.value) == t("error.invalid_timezone", "en", value="Mars/Base")


def test_rate_messages_say_which_rate_to_enter():
    from celerp.services.money import doc_rate, require_doc_rate
    with pytest.raises(ValueError) as ei:
        doc_rate({"currency": "USD", "conversion_rate": "2"}, "USD")
    assert str(ei.value) == t("error.rate_not_one_for_base", "en", base="USD", raw="2")
    with pytest.raises(ValueError) as ei:
        require_doc_rate({"currency": "EUR"}, "USD")
    assert str(ei.value) == t("error.rate_required", "en", currency="EUR", base="USD")


def test_quantity_messages_name_the_rule():
    from celerp.services.units import validate_positive, validate_quantity
    with pytest.raises(HTTPException) as ei:
        validate_quantity(1.234, 2, label="Ring")
    assert (ei.value.detail["message_key"], ei.value.detail["params"]) == (
        "quantity.precision", {"label": "Ring", "qty": "1.234", "decimals": 2})
    with pytest.raises(HTTPException) as ei:
        validate_positive(-1, label="Ring")
    assert ei.value.detail == t("error.qty_positive", "en", label="Ring")


def test_stripe_amount_says_pay_another_way():
    from celerp.services.payments import to_stripe_amount
    with pytest.raises(ValueError) as ei:
        to_stripe_amount("10.5", "JPY")
    assert str(ei.value) == t("error.stripe_amount", "en", value="10.5", code="JPY")


@pytest.mark.asyncio
@pytest.mark.parametrize("url,kw,key", [
    ("ftp://example.com", {}, "error.url_invalid"),
    ("https://example.com/?a=1", {"reject_query": True}, "error.url_has_query"),
    ("https://example.com/#a", {"reject_fragment": True}, "error.url_has_fragment"),
])
async def test_web_address_messages_say_how_to_fix(url, kw, key):
    from celerp.services.outbound_url import validate_public_base_url
    with pytest.raises(ValueError) as ei:
        await validate_public_base_url(url, **kw)
    assert str(ei.value) == t(key, "en")


def test_backup_key_problem_says_who_fixes_it():
    from celerp.services.backup import _parse_key
    with pytest.raises(ValueError) as ei:
        _parse_key("c2hvcnQ=")
    assert str(ei.value) == t("error.backup_key_invalid", "en")


def _fail(exc):
    def run(*_a, **_k):
        raise exc
    return run


@pytest.mark.parametrize("exc,key", [
    (FileNotFoundError(), "error.backup_tool_missing"),
    (subprocess.TimeoutExpired("pg_dump", 300), "error.backup_timed_out"),
])
def test_backup_failures_say_what_to_do(exc, key, monkeypatch):
    from celerp.services import backup
    monkeypatch.setattr(backup, "_find_pg_tool", lambda n: n)
    with pytest.raises(RuntimeError) as ei:
        backup.dump_database("postgresql://x/y", runner=_fail(exc))
    assert str(ei.value) == t(key, "en")


def test_failed_backup_keeps_details_for_support(monkeypatch):
    from celerp.services import backup
    monkeypatch.setattr(backup, "_find_pg_tool", lambda n: n)
    done = subprocess.CompletedProcess([], 1, b"", b"disk full")
    with pytest.raises(RuntimeError) as ei:
        backup.dump_database("postgresql://x/y", runner=lambda *a, **k: done)
    assert str(ei.value) == t("error.backup_failed_detail", "en", detail="disk full")


@pytest.mark.parametrize("exc,key", [
    (FileNotFoundError(), "error.restore_tool_missing"),
    (subprocess.TimeoutExpired("pg_restore", 600), "error.restore_timed_out"),
])
def test_restore_failures_say_what_to_do(exc, key):
    from celerp.services import backup
    with pytest.raises(RuntimeError) as ei:
        backup._run_tool(["pg_restore"], _fail(exc))
    assert str(ei.value) == t(key, "en")


@pytest.mark.asyncio
async def test_unknown_notification_says_refresh(client):
    r = await client.post(f"/notifications/{uuid.uuid4()}/read", headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("error.notification_not_found", "en")


@pytest.mark.asyncio
async def test_long_search_says_how_long(client):
    from celerp.routers.search import _MAX_Q_LEN
    r = await client.get("/search", params={"q": "x" * (_MAX_Q_LEN + 1)}, headers=await _headers(client))
    assert r.status_code == 422
    assert r.json()["detail"] == t("search.err_too_long", "en", max=_MAX_Q_LEN)


# -- AI assistant -----------------------------------------------------------

def test_ai_errors_are_translated():
    import asyncio
    from celerp.ai.service import _SessionExpired, _user_error
    assert _user_error(asyncio.TimeoutError()) == t("ai.err_timeout", "en")
    assert _user_error(_SessionExpired()) == t("error.session_expired", "en")


def test_unfinished_action_says_retry_is_safe():
    from celerp.ai.conversations import unfinished_action_text
    assert unfinished_action_text() == t("ai.action_unfinished", "en")


def test_batch_limit_says_remove_some():
    from celerp.ai.batch import MAX_BATCH_FILES, interrupted_error
    assert "Remove some" in t("error.batch_too_many", "en", max=MAX_BATCH_FILES)
    assert interrupted_error() == t("ai.job_interrupted", "en")


# -- Copy -------------------------------------------------------------------

@pytest.mark.parametrize("key,needle", [
    ("api.timed_out", "Refresh the page to see whether it worked"),
    ("api.unreachable", "restart Celerp"),
    ("page.api_unavailable", "isn't responding"),
    ("dashboard.error_loading", "Refresh the page"),
    ("ai.action_expired", "Ask the assistant again"),
    ("ai.action_failed", "make the change yourself"),
    ("ai.confirm_all_result", "Open the ones marked"),
    ("ai.confirm_all_result_attention", "see what to fix"),
    ("ai.conversation_load_failed", "Refresh the page"),
    ("ai.conversations_load_failed", "Refresh the page"),
    ("ai.memory_load_failed", "saved notes"),
    ("ai.quota_unavailable", "internet connection"),
    ("ai.job_failed", "attach them again"),
    ("ai.upload_failed", "Try again"),
    ("search.partial", "may be incomplete"),
    ("msg.upload_failed", "internet connection"),
    ("shell.request_failed", "Refresh the page"),
    ("shell.network_error", "still running"),
    ("shell.update_check_failed", "internet connection"),
    ("shell.update_result_rollback_failed", "GitHub Issues or Discussions"),
    ("shell.update_blocked_channel", "Use that program to update"),
    ("shell.update_blocked_administrator", "Ask them"),
    ("shell.update_blocked_check_failed", "internet connection"),
    ("labels.err_template_name_required", "Enter a name"),
    ("ai.err_conversation_not_found", "Start a new one"),
])
def test_message_says_what_to_do(key, needle):
    assert needle in _en(key)


@pytest.mark.parametrize("key", ["shell.update_blocked_administrator", "error.restore_already_set_up"])
def test_owner_only_messages_name_the_installation_owner(key):
    # The installation owner role can be handed on, so the original installer may no longer hold it.
    assert "installation owner" in _en(key)
    assert "installed" not in _en(key)


@pytest.mark.parametrize("key", ["shell.error_prefix", "ai.error_prefix",
                                 "shell.request_failed_prefix", "shell.network_error_prefix"])
def test_prefix_keys_are_gone(key):
    for lang in _LANGS:
        raw = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        assert key not in raw, (lang, key)


def test_thai_reader_gets_thai_service_message():
    assert t("error.server_error", "th") != t("error.server_error", "en")


def test_shell_errors_show_the_message_not_the_request_path():
    from ui.components import shell
    texts = shell._shell_js_i18n("en")
    assert texts["requestFailed"] == t("shell.request_failed", "en")
    assert texts["networkError"] == t("shell.network_error", "en")
    assert "unknownRequest" not in texts
    assert "requestPath" not in shell._CLIENT_JS
    assert "serverErrorText(e.detail && e.detail.xhr)" in shell._CLIENT_JS


def test_unusable_rate_is_one_sentence_naming_the_value():
    from celerp.services.money import require_doc_rate
    with pytest.raises(ValueError) as ei:
        require_doc_rate({"currency": "EUR", "conversion_rate": "abc"}, "USD")
    assert str(ei.value) == t("error.rate_invalid", "en", raw="abc")


def test_document_rate_problem_is_not_wrapped_twice():
    from celerp_docs.routes import _require_doc_rate_http
    with pytest.raises(HTTPException) as ei:
        _require_doc_rate_http({"currency": "EUR"}, "USD")
    assert ei.value.detail == t("error.rate_required", "en", currency="EUR", base="USD")
