"""Everything Celerp AI writes for a person reads in that person's language.

A stand-in language carries every English string with a marker in front, so any
text below that comes back without the marker was written in English in code."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from celerp.accounting_roles import refusal as _refusal
from celerp.ai import batch, jobs, llm, service
from celerp_ai import routes, ui_routes
from ui import i18n

_MARK = "QX:"
_EN = json.loads((Path(i18n.__file__).parent / "locales" / "en.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _stand_in_language():
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("qx", {k: _MARK + v for k, v in _EN.items()})
    i18n.set_lang("qx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _english(texts) -> list[str]:
    return [text for text in texts if not text.startswith(_MARK)]


class _Lookups(routes._Lookups):
    """The real contact matching over canned contact rows: a count of contacts
    named as the vendor, or the error text of a failed lookup."""

    def __init__(self, rows):
        self._rows = rows

    def has(self, name):
        return True

    async def rows(self, name, query):
        if isinstance(self._rows, dict):
            return routes._action_error(self._rows)
        return [{"id": f"c{i}", "name": query["q"]} for i in range(self._rows)]

    async def item_id(self, description):
        return None


async def _proposal_texts(rows, extraction) -> list[str]:
    entry = {"file_id": "f1", "filename": "scan.png"}
    records, summary = await routes._bill_proposals(_Lookups(rows), entry, extraction, datetime.now(timezone.utc))
    texts = [summary]
    for record in records:
        texts.append(record["title"])
        texts.extend(record["warnings"])
        notes = record["arguments"]["body"].get("notes")
        if notes:
            texts.append(notes)
    return texts


@pytest.mark.asyncio
@pytest.mark.parametrize("rows, extraction", [
    (0, {"vendor_name": "Kingfisher", "reference": "R1", "total": 99,
             "line_items": [{"description": "Box", "quantity": 1, "unit_price": 10}, {"description": "Tape"}]}),
    (2, {"vendor_name": "Kingfisher", "line_items": []}),
    ({"ok": False, "status": 0, "error": {"code": "capability_timeout", **_refusal(
        "ai.err_capability_timeout", "The operation did not finish.", seconds=30)}},
     {"vendor_name": "Kingfisher", "date": "2026-10-31", "total": 5}),
    (1, {}),
    (1, {"vendor_name": "Kingfisher", "total": 10, "line_items": [{"description": "Box", "quantity": 1,
                                                                   "unit_price": 10}]}),
])
async def test_receipt_proposals_read_in_the_reader_language(rows, extraction):
    texts = await _proposal_texts(rows, extraction)
    assert len(texts) > 1
    assert _english(texts) == []
    assert not any("did not finish" in text for text in texts)


def test_files_without_a_bill_say_why_in_the_reader_language():
    entries = [
        {"filename": "a.png", "status": "failed", "error": None},
        {"filename": "b.png", "status": "success", "extraction": None},
        {"filename": "c.png", "status": "success", "extraction": {"document_kind": "statement"}},
        {"filename": "d.png", "status": "success", "extraction": {"document_kind": "other"}},
    ]
    assert _english([routes._unproposed_line(e) for e in entries]) == []


def test_unmet_dependencies_are_refused_in_the_reader_language():
    target = {"id": "b", "bindings": [{"source_action_id": "a", "source_result_key": "id", "target_path": ["x"]}]}
    sources = [
        None, {"id": "a", "status": "failed"}, {"id": "a", "status": "pending"},
        {"id": "a", "status": "completed", "result_summary": {}},
    ]
    errors = [routes._resolve_action_arguments([s, target] if s else [target], target)[1] for s in sources]
    bad_target = {"id": "b", "bindings": [{"source_action_id": "a", "source_result_key": "id", "target_path": []}]}
    errors.append(routes._resolve_action_arguments(
        [{"id": "a", "status": "completed", "result_summary": {"id": "1"}}, bad_target], bad_target)[1])
    errors.append(routes._resolve_action_arguments([{"id": "b", "bindings": ["x"]}], {"bindings": ["x"]})[1])
    errors.append(routes._action_error({"ok": False, "status": 500}))
    errors.append(routes._action_error({"ok": False, "status": 422, "data": {"detail": [{"msg": "Field required"}]}}))
    assert all(e.get("message_key") for e in errors)
    assert _english([i18n.refusal_text(e) for e in errors]) == []


@pytest.mark.asyncio
async def test_file_reading_results_read_in_the_reader_language(monkeypatch):
    sent = []

    async def notify(company_id, user_id, conversation_id, key, **params):
        sent.extend([i18n.t(f"{key}.title", **params), i18n.t(f"{key}.body", **params)])

    monkeypatch.setattr(jobs, "notify", notify)
    run = batch.FileRun(job_id=uuid.uuid4(), company_id=uuid.uuid4(), user_id=uuid.uuid4(),
                        conversation_id=None, query="")
    for completed, failed in ((0, 1), (0, 2), (1, 0), (1, 1)):
        await batch.notify_finished(run, completed, failed)
    errors = [batch._file_error(e) for e in (HTTPException(402), asyncio.TimeoutError(), ValueError())]
    assert _english(sent + errors) == []


def test_a_file_read_while_free_questions_are_paused_says_so():
    """The relay refuses a free account's read while free questions are paused: the
    file says that, not that it could not be read."""
    paused = HTTPException(503, detail={"code": "free_paused", "message": "Free AI queries are paused.",
                                        "resets_at": "2026-12-01T00:00:00Z"})
    assert batch._file_error(paused) == i18n.t("ai.free_paused", date="2026-12-01")


# Every failure code the AI service reports (celerp.ai.llm.RelayError): those its
# replies map to, and those raised in celerp.ai.llm itself.
# Every code the relay client can raise: the relay's own codes it passes through, and
# the codes llm.py raises itself, read from its source so a new one is covered.
_RELAY_CODES = sorted(set(llm._RELAY_CODES.values())
                      | set(re.findall(r'RelayError\(\s*"(\w+)"', Path(llm.__file__).read_text())))


@pytest.mark.parametrize("code", _RELAY_CODES)
def test_a_service_failure_reads_in_the_reader_language_never_as_the_service_wrote_it(code):
    from celerp.ai.llm import RelayError
    exc = RelayError(code, "Service text in English")
    texts = [service.user_error(exc), batch._file_error(exc)]
    assert _english(texts) == []
    assert service._error_code(exc) == code
    assert (texts[1] == i18n.t("ai.file_lost")) == (code in service.NEW_QUESTION_ERRORS)


def test_ai_service_refusals_read_in_the_reader_language_never_as_the_service_wrote_them():
    english = {"code": "plan_limit", "message": "This plan allows 3 file per question.", "max_files": 3}
    files, _ = ui_routes._refusal_text(402, english, "qx")
    texts = [files, ui_routes._refusal_text(402, {"code": "plan_limit"}, "qx")[0],
             ui_routes._error_text(ui_routes._keyed({"code": "new_code", "message": "Service text"}), "qx"),
             ui_routes._outcome_text({"ok": False, "error": {"code": "x", "message": "Route text",
                                                              "message_key": "ai.err_unexpected"}}, "qx")[1]]
    assert _english(texts) == []
    assert "3" in files


def test_the_app_refusals_an_ai_job_raises_read_in_the_reader_language():
    """A refusal the app raises while running AI work is keyed, so a thread reloaded later
    shows it in the reader's language, not as an unexpected error."""
    details = [jobs._JobJournal._refused(key).detail for key in ("ai.job_permission_lost", "ai.job_ai_unavailable")]
    with pytest.raises(HTTPException) as gone:
        service._load_files(["no-such-file"], uuid.uuid4(), uuid.uuid4())
    details.append(gone.value.detail)
    texts = [ui_routes._error_text(ui_routes._keyed(detail), "qx") for detail in details]
    assert _english(texts) == []
    assert i18n.t("ai.err_unexpected", "qx") not in texts


# The HTTPException details in the AI code that are neither keyed nor translated where
# they are raised, each with why no reader sees English from it.
_UNKEYED_DETAILS = {
    ("llm.py", "detail"): "the AI service's refusal: explained by its code or never shown (_keyed)",
    ("routes.py", "refusal['detail']"): "a question's stored refusal, raised again as it was stored",
    ("routes.py", "out['error']"): "a question's failure, written in its asker's language (service.user_error)",
    ("routes.py", "str(exc)"): "create_batch_job's refusals, translated where raised (batch.py)",
    ("routes.py", "error"): "a confirmed action's error, kept whole by _action_error",
    ("routes.py", "{'code': 'job_failed', 'message': job.error}"):
        "the job's failure, written in its asker's language when it failed",
    ("routes.py", "'include_protected requires offset=0'"): "a misused API parameter no screen sends",
    ("routes.py", "keyed_error(code, key)"): "_conflict: every caller names a catalog key (checked here)",
}


def test_every_refusal_the_ai_code_raises_is_keyed_or_translated_where_raised():
    """Class guard: an HTTPException under celerp/ai or celerp_ai carries the canonical
    keyed refusal (spread into its detail, or built by keyed_error with a catalog key) or
    text translated where it is raised, so no reader sees its English or a stand-in,
    live or after a reload. A str, f-string or variable detail is reviewed above."""
    import ast
    from celerp import ai

    def keyed(node, names) -> bool:
        if isinstance(node, ast.Dict):
            return any(key is None and isinstance(value, ast.Call) and getattr(value.func, "id", None) in names
                       for key, value in zip(node.keys, node.values))
        name = getattr(getattr(node, "func", None), "id", None)
        if name == "t":
            return True
        return name == "keyed_error" and len(node.args) > 1 and getattr(node.args[1], "value", None) in _EN

    unkeyed = []
    for path in [*Path(ai.__file__).parent.glob("*.py"), *Path(routes.__file__).parent.glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 and n.module == "celerp.accounting_roles" for a in n.names if a.name == "refusal"}
        for node in ast.walk(tree):
            name = getattr(getattr(node, "func", None), "id", None)
            if name == "_conflict":
                if getattr(node.args[1], "value", None) not in _EN:
                    unkeyed.append(f"{path.name}:{node.lineno}")
                continue
            if name != "HTTPException":
                continue
            detail = next((k.value for k in node.keywords if k.arg == "detail"), None)
            if detail is not None and not keyed(detail, names) \
                    and (path.name, ast.unparse(detail)) not in _UNKEYED_DETAILS:
                unkeyed.append(f"{path.name}:{node.lineno} {ast.unparse(detail)}")
    assert unkeyed == []


def test_a_refusal_stored_without_its_key_is_never_shown_in_english():
    """A stored question refusal with no catalog key (the AI service's English, or a
    detail that was never keyed) reads as the unexpected-error text."""
    assert ui_routes._error_text(ui_routes._keyed({"message": "Something broke"}), "qx") == \
        i18n.t("ai.err_unexpected", "qx")
    assert ui_routes._keyed("Something broke") is None


def test_answer_errors_read_in_the_reader_language():
    texts = [service.user_error(RuntimeError()), service.user_error(asyncio.TimeoutError())]
    assert _english(texts) == []


def test_proposal_card_field_names_and_values_read_in_the_reader_language():
    keys = ["doc_type", "contact_name", "issue_date", "line_items", "name", "description", "quantity",
            "unit_price", "line_total", "subtotal", "tax", "total", "notes", "currency", "contact_type"]
    texts = [ui_routes._label(k) for k in keys]
    texts += [ui_routes._fmt_scalar("doc_type", "bill", None), ui_routes._fmt_scalar("contact_type", "vendor", None)]
    assert _english(texts) == []
    assert ui_routes._fmt_scalar("name", "Kingfisher Packaging", None) == "Kingfisher Packaging"


async def _executor_errors(monkeypatch) -> list[dict]:
    """One failed capability call for every error the executor reports, each from the
    real executor against a small app."""
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from celerp.ai import deadlines, tools

    app = FastAPI()

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(1)

    @app.get("/text", response_class=PlainTextResponse)
    async def text():
        return "plain"

    @app.get("/big")
    async def big():
        return {"value": "x" * 200}

    def cap(path, **extra):
        return {"method": "GET", "path": path, "path_names": [], "query_names": [], "expects_body": False, **extra}

    thing = cap("/thing/{item_id}", path_names=["item_id"])
    run = tools.execute_agent_capability
    results = [
        await run(app, "", cap("/text"), {}, "c"),
        await run(app, "Bearer t", cap("/text"), {"body": {}}, "c"),
        await run(app, "Bearer t", thing, {"path": {"item_id": "a/b"}}, "c"),
        await run(app, "Bearer t", cap("/text"), {}, "c"),
        await run(app, "Bearer t", cap("/big"), {}, "c", result_max_bytes=32),
    ]
    monkeypatch.setattr(deadlines, "TOOL_STEP_S", 0.01)
    results.append(await run(app, "Bearer t", cap("/slow"), {}, "c"))
    return results


_EXECUTOR_CODES = set(re.findall(r'_agent_error\(\s*"(\w+)"', Path(service._agent_error.__code__.co_filename)
                                 .read_text(encoding="utf-8")))


@pytest.mark.asyncio
async def test_every_executor_error_is_a_keyed_refusal_that_reads_in_the_reader_language(monkeypatch):
    results = await _executor_errors(monkeypatch)
    errors = [r["error"] for r in results]
    assert {e["code"] for e in errors} == _EXECUTOR_CODES
    assert all(not r["ok"] and e.get("message_key") in _EN for r, e in zip(results, errors))
    assert _english([i18n.refusal_text(e) for e in errors]) == []


def test_every_agent_error_names_a_catalog_key():
    """Class guard: every _agent_error call under celerp/ai names a catalog key, so no
    reader of a failed capability call is left with the model's English."""
    import ast
    from celerp import ai
    calls = []
    for path in sorted(Path(ai.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_agent_error":
                key = node.args[1] if len(node.args) > 1 else None
                calls.append((f"{path.name}:{node.lineno}", getattr(key, "value", None)))
    assert len(calls) > 10
    assert [where for where, key in calls if key not in _EN] == []


class _Store:
    """The one message whose proposals a confirmed action finalizes, as the database
    would hold it between a reply and a reload."""

    def __init__(self, record):
        self.message = type("Message", (), {"tools_called": [record]})()

    async def get(self, model, message_id):
        return self.message

    def add(self, row):
        pass

    async def flush(self):
        pass

    async def commit(self):
        pass


async def _confirmed(monkeypatch, result) -> tuple[dict, dict]:
    """(live outcome, stored record) of confirming one proposal whose capability call
    returns ``result``, through the real confirm runner and store."""
    from types import SimpleNamespace
    from celerp.ai import conversations, tools
    record = {"id": "call", "name": "create_contact", "status": "pending", "arguments": {}}
    store = _Store(record)

    async def resolved(session, **kw):
        return {}, None, record

    async def claim(session, **kw):
        return record

    async def execute(*args, **kw):
        return result

    monkeypatch.setattr(routes, "_resolved_action_arguments", resolved)
    monkeypatch.setattr(conversations, "claim_tool_call", claim)
    monkeypatch.setattr(tools, "execute_agent_capability", execute)
    outcome = await routes._run_confirmed_action(
        SimpleNamespace(app=None, headers={}), store, {"create_contact": {}},
        conversation_id=uuid.uuid4(), message_id=uuid.uuid4(), tool_call_id="call",
        company_id=uuid.uuid4(), user_id=uuid.uuid4(),
    )
    return outcome, store.message.tools_called[0]


def _retryable_route_refusal() -> dict:
    from celerp.accounting_roles import refusal
    return {"ok": False, "status": 503, "data": {"detail": {
        "code": "busy", **refusal("ai.err_busy", "The AI service is temporarily busy.")}}}


@pytest.mark.asyncio
async def test_a_failed_action_reads_the_same_in_the_reply_and_after_a_reload(monkeypatch):
    """Class guard at the store: every executor error and a keyed route refusal that
    comes back retryable is stored whole, so the reloaded thread says what the reply
    said, in the reader's language, never the English source."""
    from fasthtml.common import to_xml
    from celerp.ai import tools
    results = await _executor_errors(monkeypatch) + [_retryable_route_refusal()]
    monkeypatch.setattr(tools, "execute_agent_capability", None)
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.set_lang("th")
    for result in results:
        outcome, stored = await _confirmed(monkeypatch, result)
        live = ui_routes._outcome_text(outcome, "th")[1]
        english = (outcome["error"] or {}).get("message")
        assert stored["error"]["message_key"] == outcome["error"]["message_key"]
        reloaded = [to_xml(ui_routes._action_card("c", "m", stored, "th")),
                    to_xml(ui_routes._action_status_cell(stored, "th")[1])]
        assert all(live in text for text in reloaded), (outcome["error"]["code"], live)
        assert english not in live and live != i18n.t("ai.err_unexpected", "th")


@pytest.mark.asyncio
async def test_a_failed_action_stored_before_errors_were_kept_whole_still_reads():
    """An older record holds only its text: it is shown as written."""
    from fasthtml.common import to_xml
    stored = {"id": "call", "name": "create_contact", "status": "failed", "error": "ข้อความเดิม"}
    assert "ข้อความเดิม" in to_xml(ui_routes._action_card("c", "m", stored, "qx"))
