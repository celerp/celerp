# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A job is read by the user who asked it. Every caller that names a job by its id
reads it through ``jobs.owned_job`` (company, user and what the job is), or reuses it
through ``jobs.reused_job`` (the whole request), so another user's job in the same
company, another company's job and a thread's question asked for through the one-off
module API are all not found alike. A runner that kept failing leaves nothing that
reads as running.
"""
from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from fastapi import HTTPException

from ai_fake_cloud import answer, cloud, until  # noqa: F401
from celerp.ai import jobs
from celerp.models.ai import AIBatchJob
from test_ai_usage import _claims, _conversation, _settled, owner  # noqa: F401
from test_helpers import invite_user, signed_request

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731
ROOT = pathlib.Path(__file__).resolve().parent.parent
ANSWER = {"answer": "The owner's figures.", "model_used": "m", "tools_called": [], "help_pages": None,
          "error": None, "error_code": None}


async def _one_off(session, company_id, user_id, state: str) -> uuid.UUID:
    """A question asked outside a thread: answered, running in the background, or
    interrupted (open, and nobody holds its lease)."""
    job_id = uuid.uuid4()
    job = jobs.question_job(job_id=job_id, request=jobs.JobRequest.question(
        company_id=company_id, user_id=user_id, query="What are this month's figures?"))
    if state == "completed":
        job.status, job.results = "completed", ANSWER
        jobs.release(job)
    else:
        job.status = "running"
    if state == "interrupted":
        jobs.release(job)                 # its runner is gone: the lease lapsed
    session.add(job)
    await session.commit()
    return job_id


async def _colleague(client, session, token) -> uuid.UUID:
    other = await invite_user(client, session, AUTH(token), "colleague@perm.example", "manager")
    return _claims(other)[1]


@pytest.mark.parametrize("state", ["completed", "running"])
async def test_a_colleague_in_the_same_company_never_reads_another_users_question(
        client, owner, session, state):
    """The audit's reproduction: a second user allowed to use the assistant, in the
    same company, holding the asker's job id, gets 404 from the module API, never the
    asker's answer or its state."""
    from celerp.modules.api import ai_job
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, state)
    colleague = await _colleague(client, session, token)

    async with signed_request(session, company_id, user_id):
        assert (await ai_job(str(job_id), company_id=str(company_id), db_session=session))["status"] == state
    async with signed_request(session, company_id, colleague, role="manager"):
        with pytest.raises(HTTPException) as refused:
            await ai_job(str(job_id), company_id=str(company_id), db_session=session)
    assert refused.value.status_code == 404
    assert await jobs.question_result(job_id, company_id, colleague) is None


async def test_another_companys_question_is_not_found(owner, session):
    _, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "completed")
    assert (await jobs.question_result(job_id, company_id, user_id))["answer"] == ANSWER["answer"]
    assert await jobs.question_result(job_id, uuid.uuid4(), user_id) is None


@pytest.mark.parametrize("state", ["completed", "running", "interrupted"])
async def test_the_module_api_reads_a_one_off_question(client, owner, session, state):
    from celerp.modules.api import ai_job
    _, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, state)
    async with signed_request(session, company_id, user_id):
        out = await ai_job(str(job_id), company_id=str(company_id), db_session=session)
    answered = {"answer": ANSWER["answer"], "model_used": "m", "tools_called": []} if state == "completed" else {}
    assert out == {"status": state, "job_id": str(job_id), **answered}


async def test_a_thread_question_asked_for_through_the_module_api_is_not_found(client, owner, cloud, session):
    """A thread question's job stores a message id, not an answer: the one-off module
    API never reads it (404), and it stays behind its conversation's routes."""
    from celerp.modules.api import ai_job
    token, company_id, user_id = owner
    cloud.script = lambda identity, body: answer("Open Contacts and choose New.")
    conv, qid = await _conversation(client, token), uuid.uuid4()
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": "How do I add a supplier?", "operation_id": str(qid)})
    assert r.status_code == 202, r.text
    assert (await _settled(client, token, conv, qid))["state"] == "answered"
    async with signed_request(session, company_id, user_id):
        with pytest.raises(HTTPException) as refused:
            await ai_job(str(qid), company_id=str(company_id), db_session=session)
    assert refused.value.status_code == 404


@pytest.mark.parametrize("action", ["cancel", "resume", "retry-failed"])
async def test_another_users_job_is_not_found_by_its_actions(client, owner, cloud, session, action):
    """Stop, continue and retry name a job by its id: another user's is 404, as a job
    that does not exist is, never told apart from one that is only not open."""
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "running")
    other = await invite_user(client, session, AUTH(token), "colleague@perm.example", "manager")
    for headers, job in ((AUTH(other), job_id), (AUTH(token), uuid.uuid4())):
        r = await client.post(f"/ai/jobs/{job}/{action}", headers=headers)
        assert r.status_code == 404, (action, r.text)


@pytest.mark.parametrize("asker", ["the asker", "a colleague"])
@pytest.mark.parametrize("mode", ["question", "receipts"])
async def test_a_thread_send_under_a_one_off_questions_id_is_refused(
        client, owner, cloud, session, mode, asker):
    """A thread send names its question by an id; an id that already names a job made
    for another request (here a question asked outside a thread) is refused and nothing
    is added to the thread: the asker's own job is ``operation_conflict``, a colleague's
    is not found, as every job of another user is."""
    from test_ai_jobs import _files
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "completed")
    if asker == "a colleague":
        token = await invite_user(client, session, AUTH(token), "colleague@perm.example", "manager")
        company_id, user_id = _claims(token)
    conv = await _conversation(client, token)
    body = {"query": "What are this month's figures?", "operation_id": str(job_id)}
    if mode == "receipts":
        body |= {"file_ids": _files(company_id, user_id, 1), "document_mode": "receipts"}
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json=body)
    if asker == "a colleague":
        assert r.status_code == 404, r.text
    else:
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "operation_conflict"
    thread = (await client.get(f"/ai/conversations/{conv}", headers=AUTH(token))).json()
    assert thread["messages"] == [] and thread["jobs"] == []
    assert not cloud.sends


async def test_a_job_of_another_kind_is_not_found_where_it_is_read(client, owner, cloud, session):
    """A question asked outside a thread is not a thread's question, nor a reading job:
    each route that reads one of those by its id answers 404 for it."""
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "completed")
    conv = await _conversation(client, token)
    for method, path in (("GET", f"/ai/conversations/{conv}/questions/{job_id}"),
                         ("GET", f"/ai/batch/{job_id}"),
                         ("POST", f"/ai/conversations/{conv}/jobs/{job_id}/proposals")):
        r = await client.request(method, path, headers=AUTH(token))
        assert r.status_code == 404, (path, r.text)


async def test_an_interrupted_question_asked_again_reads_under_the_askers_current_session(
        client, owner, cloud, session):
    """Continued by its asker, a question's tools read the app under the session that
    asked again, never under the one that asked first (which a sign-out may have ended)."""
    from celerp.services.auth import decode_access_token
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "interrupted")
    job = await session.get(AIBatchJob, job_id)
    job.auth_nonce = "a-session-since-ended"
    await session.commit()
    cloud.script = lambda identity, body: answer("This month's figures.")
    r = await client.post("/ai/query", headers=AUTH(token),
                          json={"query": "What are this month's figures?", "operation_id": str(job_id)})
    assert r.status_code == 200, r.text
    job = await session.get(AIBatchJob, job_id, populate_existing=True)
    assert job.auth_nonce == decode_access_token(token)["snonce"]


# ── Guard: one way to name a job ─────────────────────────────────────────────

JOB_TABLES = {"AIBatchJob", "AIJobStep"}
QUERIES = {"get", "select", "update", "delete"}
# Reads of the job tables that never name one job by its id, with why each is allowed.
ALLOWED = {
    ("celerp/ai/batch.py", "publish_progress"): "the runner counts its own job's steps",
    ("celerp/ai/batch.py", "list_conversation_jobs"): "lists by company, user and conversation",
    ("celerp/ai/conversations.py", "_protected_conversation_ids"): "conversation ids the caller already owns",
    ("celerp/services/company_reset.py", "reset"): "whether the company has any open job",
}


def _job_queries(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            target = node.args[1] if name == "get" and len(node.args) > 1 else node.args[0]
            while isinstance(target, ast.Attribute):
                target = target.value
            if name in QUERIES and isinstance(target, ast.Name) and target.id in JOB_TABLES:
                yield fn.name, node.lineno


def test_only_the_job_module_names_a_job_by_its_id():
    """Outside ``celerp/ai/jobs.py`` (the accessors and the runner) no code reads or
    writes the job tables but the listed few that never name one job by its id, so a
    new caller cannot read a job without its owner."""
    found, seen = [], set()
    for top in ("celerp", "ui", "default_modules"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or rel == "celerp/ai/jobs.py" or "/migrations/" in rel:
                continue
            for fn, line in _job_queries(path):
                seen.add((rel, fn))
                if (rel, fn) not in ALLOWED:
                    found.append(f"{rel}:{line} in {fn}")
    assert not found, "read or reuse a job through jobs.owned_job or jobs.reused_job:\n" + "\n".join(found)
    assert seen >= set(ALLOWED), f"stale allowances: {set(ALLOWED) - seen}"
