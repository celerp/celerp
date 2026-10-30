# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One id, one request. A job keeps the identity of the request that created it
(``jobs.JobRequest``: everything the asker sends that the answer depends on), and
every path that reuses a job by its id compares that one identity: a request that
differs in anything is refused ``operation_conflict``, never answered with the job
asked for something else, whether that job answered, still runs or was interrupted.
"""
from __future__ import annotations

import dataclasses
import time
import uuid

import pytest
from fastapi import HTTPException

from ai_fake_cloud import answer, cloud, until  # noqa: F401
from celerp.ai import jobs
from celerp.ai.batch import create_batch_job
from celerp.models.ai import AIBatchJob
from test_ai_jobs import _files
from test_ai_usage import _conversation, owner  # noqa: F401

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731


async def test_the_same_question_id_with_another_file_is_refused_never_given_the_first_files_answer(
        client, owner, cloud):
    """The audit's reproduction: "Analyze this invoice" with file A under id X answers;
    the same text under X with file B must not return file A's answer."""
    token, company_id, user_id = owner
    file_a, file_b = _files(company_id, user_id, 2)
    op = str(uuid.uuid4())
    cloud.script = lambda identity, body: answer("Invoice A totals 100.")
    first = await client.post("/ai/query", headers=AUTH(token),
                              json={"query": "Analyze this invoice", "file_ids": [file_a], "operation_id": op})
    assert first.status_code == 200, first.text
    assert first.json()["answer"] == "Invoice A totals 100."

    again = await client.post("/ai/query", headers=AUTH(token),
                              json={"query": "Analyze this invoice", "file_ids": [file_b], "operation_id": op})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "operation_conflict"
    assert cloud.executions[op] == 1


async def test_the_same_files_in_another_order_are_another_request(client, owner, cloud):
    token, company_id, user_id = owner
    files = _files(company_id, user_id, 2)
    op = str(uuid.uuid4())
    first = await client.post("/ai/query", headers=AUTH(token),
                              json={"query": "Compare these", "file_ids": files, "operation_id": op})
    assert first.status_code == 200, first.text
    again = await client.post("/ai/query", headers=AUTH(token),
                              json={"query": "Compare these", "file_ids": files[::-1], "operation_id": op})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "operation_conflict"


def _other(value):
    """A value of the same kind as ``value`` that differs from it."""
    if value is None or isinstance(value, uuid.UUID):
        return uuid.uuid4()
    if isinstance(value, str):
        return value + " (changed)"
    if isinstance(value, tuple):
        return (*value, "ai_up_other")
    raise AssertionError(f"no changed value for {value!r}: teach _other this field's type")


def _held(job: AIBatchJob, state: str) -> None:
    if state == "completed":
        job.status, job.results = "completed", {"answer": "The first request's answer.", "model_used": "m",
                                                "tools_called": [], "help_pages": None, "error": None,
                                                "error_code": None}
    elif state == "running":
        job.status, job.mode = "running", "background"
    else:   # interrupted: an interactive question a restart ended, open and run by nobody
        job.status = "running"


@pytest.mark.parametrize("state", ["completed", "running", "interrupted"])
async def test_every_field_of_the_request_identity_is_held_to_its_id(client, owner, cloud, session, state):
    """Each field of ``jobs.JobRequest`` in turn differs between the job and the
    request reusing its id: each is ``operation_conflict``, nothing is sent and no
    answer is returned. The fields come from the identity itself, so a field added to
    it is covered here without a change to this test."""
    token, company_id, user_id = owner
    asked = dict(company_id=company_id, user_id=user_id, query="Analyze this invoice",
                 file_ids=_files(company_id, user_id, 2))
    base = jobs.JobRequest.question(**asked)
    fields = [f.name for f in dataclasses.fields(jobs.JobRequest)]
    assert set(fields) >= {"company_id", "user_id", "kind", "conversation_id", "query", "file_ids"}
    for name in fields:
        job_id = uuid.uuid4()
        job = jobs.question_job(job_id=job_id, request=base)
        job.request_digest = dataclasses.replace(base, **{name: _other(getattr(base, name))}).digest()
        _held(job, state)
        session.add(job)
        await session.commit()

        with pytest.raises(HTTPException) as refused:
            await jobs.ask(**asked, job_id=job_id, started=time.monotonic())
        assert refused.value.status_code == 409, name
        assert refused.value.detail["code"] == "operation_conflict", name
        assert not jobs.task_alive(job_id), name
    assert not cloud.sends


async def test_a_job_stores_the_identity_of_the_request_that_created_it(client, owner, session):
    _, company_id, user_id = owner
    files = _files(company_id, user_id, 2)
    question = jobs.question_job(job_id=uuid.uuid4(), request=jobs.JobRequest.question(
        company_id=company_id, user_id=user_id, query="Analyze this invoice", file_ids=files))
    reading = await create_batch_job(session, company_id, user_id, "Read", files)
    assert question.request_digest == jobs.JobRequest(
        company_id=company_id, user_id=user_id, kind="question", conversation_id=None,
        query="Analyze this invoice", file_ids=tuple(files)).digest()
    assert reading.request_digest == jobs.JobRequest(
        company_id=company_id, user_id=user_id, kind="documents", conversation_id=None,
        query="Read", file_ids=tuple(files)).digest()


def test_every_field_a_caller_sends_is_part_of_the_request_identity():
    """What the AI routes accept, but the id itself, is in the identity: a field added
    to a request model fails here until the identity holds it."""
    from celerp_ai.routes import ConversationQueryRequest, QueryRequest
    identity = {f.name for f in dataclasses.fields(jobs.JobRequest)}
    as_identity = {"document_mode": "kind"}
    for model in (QueryRequest, ConversationQueryRequest):
        sent = {as_identity.get(name, name) for name in model.model_fields} - {"operation_id"}
        assert sent <= identity, (model.__name__, sent - identity)


async def test_a_reading_job_asked_again_with_other_files_is_refused(client, owner, cloud):
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    first, second = _files(company_id, user_id, 2)
    op = str(uuid.uuid4())
    body = {"query": "Read", "document_mode": "receipts", "operation_id": op}
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={**body, "file_ids": [first]})
    assert r.status_code == 202, r.text
    await until(lambda: cloud.sends)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={**body, "file_ids": [second]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "operation_conflict"
