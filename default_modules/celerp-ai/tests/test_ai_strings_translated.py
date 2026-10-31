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
        if isinstance(self._rows, str):
            return self._rows
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
    ("down", {"vendor_name": "Kingfisher", "date": "2026-10-31", "total": 5}),
    (1, {}),
    (1, {"vendor_name": "Kingfisher", "total": 10, "line_items": [{"description": "Box", "quantity": 1,
                                                                   "unit_price": 10}]}),
])
async def test_receipt_proposals_read_in_the_reader_language(rows, extraction):
    texts = await _proposal_texts(rows, extraction)
    assert len(texts) > 1
    assert _english(texts) == []


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
    errors.append(routes._action_error({"ok": False, "status": 500})["message"])
    assert _english(errors) == []


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


def test_every_structured_refusal_in_the_ai_service_carries_its_catalog_key():
    """Class guard: an HTTPException under celerp/ai whose detail is a dict literal spreads
    the canonical keyed refusal into it, so no reader sees its English or a stand-in."""
    import ast
    from celerp import ai
    unkeyed = []
    for path in sorted(Path(ai.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 and n.module == "celerp.accounting_roles" for a in n.names if a.name == "refusal"}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HTTPException"):
                continue
            detail = next((k.value for k in node.keywords if k.arg == "detail"), None)
            if isinstance(detail, ast.Dict) and not any(
                    key is None and isinstance(value, ast.Call) and getattr(value.func, "id", None) in names
                    for key, value in zip(detail.keys, detail.values)):
                unkeyed.append(f"{path.name}:{node.lineno}")
    assert unkeyed == []


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
