# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/batch.py"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base
from celerp.models.company import Company, User
from celerp.models.ai import AIBatchJob
from celerp.config import settings
from celerp.ai import jobs
from celerp.ai.batch import MAX_BATCH_FILES, create_batch_job, parse_extraction
from celerp.ai.jobs import owned_job
from celerp.ai.llm import ModelResult, RelayError
from celerp.models.accounting import UserCompany
from ui.i18n import t


def _model_result(content: str, credits: int = 1) -> ModelResult:
    """A gateway reply carrying ``content`` and ``credits`` of metered usage."""
    return ModelResult(
        message={"role": "assistant", "content": content},
        model_used="test-model",
        usage={"total_tokens": 10, "credits": credits},
        reservation_id=None,
        remaining=None,
    )

@pytest_asyncio.fixture
async def engine():
    """Async Postgres engine on a fresh isolated schema. A job's runner opens its own
    sessions, so it needs a real multi-session engine rather than the
    rollback-isolated root session."""
    import uuid as _uuid
    from sqlalchemy import text as _text

    base_url = os.environ["DATABASE_URL"]
    schema = f"aibatch_{_uuid.uuid4().hex[:8]}"

    admin = create_async_engine(base_url)
    async with admin.begin() as c:
        await c.execute(_text(f'CREATE SCHEMA "{schema}"'))
    await admin.dispose()

    eng = create_async_engine(base_url, connect_args={"server_settings": {"search_path": schema}})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()

    admin = create_async_engine(base_url)
    async with admin.begin() as c:
        await c.execute(_text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture
async def db_factory(engine):
    """The session factory the job runner opens its sessions from."""
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(jobs, "SessionLocal", factory), \
         patch.object(jobs, "gate", jobs.ModelGate(jobs.gate.size)), \
         patch.object(jobs, "STEP_RETRY_DELAY_S", 0.01), \
         patch("celerp.session_gate.get_session_token", return_value="test-session-token"), \
         patch("celerp.notifications.service.deliver"):
        yield factory
    await jobs.shutdown()


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="BatchCo", slug="batchco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user(session, company) -> User:
    u = User(email="batch@test.com", name="Test")
    session.add(u)
    await session.flush()
    session.add(UserCompany(user_id=u.id, company_id=company.id, role="owner"))
    await session.commit()
    await session.refresh(u)
    return u


def _create_test_files(company_id: uuid.UUID, user_id: uuid.UUID, count: int = 3) -> list[str]:
    """Create test files on disk and return file IDs."""
    upload_dir = settings.data_dir / "ai_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_ids = []
    for i in range(count):
        fid = f"ai_up_{uuid.uuid4().hex}"
        (upload_dir / f"{fid}.bin").write_bytes(b"fake image data")
        (upload_dir / f"{fid}.meta").write_text(json.dumps({
            "filename": f"receipt_{i}.jpg",
            "content_type": "image/jpeg",
            "size": 15,
            "company_id": str(company_id),
            "user_id": str(user_id),
        }))
        file_ids.append(fid)
    return file_ids


def _model_call(reply):
    """A stand-in for llm.complete: it reports the send, then answers with ``reply``
    (a ModelResult, an exception, or a callable of the operation id returning one)."""
    async def _complete(messages, *, operation_id=None, before_send=None, **_kw):
        await before_send(len(str(messages)))
        value = reply(operation_id) if callable(reply) and not isinstance(reply, ModelResult) else reply
        if isinstance(value, BaseException):
            raise value
        return value
    return _complete


async def _run(job_id: uuid.UUID, reply) -> None:
    """Run a reading job to its end with every model call answered by ``reply``."""
    with patch("celerp.ai.llm.complete", side_effect=_model_call(reply)):
        jobs.start_job(job_id)
        while jobs.running_tasks():
            await asyncio.gather(*jobs.running_tasks(), return_exceptions=True)


async def _files_of(db_factory, job_id) -> list:
    async with db_factory() as s:
        return (await jobs.job_steps(s, [job_id]))[job_id]


async def _job(db_factory, job_id) -> AIBatchJob:
    async with db_factory() as s:
        return await s.get(AIBatchJob, job_id)


# ── create_batch_job ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_creates_job_pending(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "process these", ["f1", "f2", "f3"])
    await session.commit()

    assert job.status == "pending"
    steps = (await jobs.job_steps(session, [job.id]))[job.id]
    assert [(s.key, s.state, s.credits) for s in steps] == [("f1", "pending", 0), ("f2", "pending", 0),
                                                            ("f3", "pending", 0)]
    assert [s.operation_id for s in steps] == [jobs.file_operation_id(job.id, f, 0) for f in ("f1", "f2", "f3")]


@pytest.mark.asyncio
async def test_batch_max_files_enforced(session, company, user):
    file_ids = [f"f{i}" for i in range(MAX_BATCH_FILES + 1)]
    with pytest.raises(ValueError, match="at most"):
        await create_batch_job(session, company.id, user.id, "too many", file_ids)


@pytest.mark.asyncio
async def test_batch_deduplicates_same_file_id(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "duplicates", ["f1", "f1", "f2", "f1"])
    assert list(job.file_ids) == ["f1", "f2"]
    assert [s.key for s in (await jobs.job_steps(session, [job.id]))[job.id]] == ["f1", "f2"]


@pytest.mark.asyncio
async def test_batch_single_file_allowed(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "one receipt", ["f1"])
    assert list(job.file_ids) == ["f1"]


@pytest.mark.asyncio
async def test_batch_empty_rejected(session, company, user):
    with pytest.raises(ValueError, match="at least one"):
        await create_batch_job(session, company.id, user.id, "none", [])


# ── Running a reading job ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_all_success(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    await _run(job.id, _model_result("Extracted data"))

    updated = await _job(db_factory, job.id)
    assert updated.status == "completed"
    assert updated.completed_at is not None
    assert [s.state for s in await _files_of(db_factory, job.id)] == ["done"] * 3


@pytest.mark.asyncio
async def test_batch_partial_failure(session, db_factory, company, user):
    """A file the model keeps failing on ends retryable; the others are read and the
    job completes."""
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()
    failing = jobs.file_operation_id(job.id, file_ids[1], 0)

    await _run(job.id, lambda op: RuntimeError("API error") if op == failing else _model_result("Success"))

    assert (await _job(db_factory, job.id)).status == "completed"
    steps = await _files_of(db_factory, job.id)
    assert [s.state for s in steps] == ["done", "failed", "done"]
    assert steps[1].result["error"] == t("ai.err_file_unexpected")


@pytest.mark.asyncio
async def test_batch_total_failure(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    await _run(job.id, RuntimeError("All fail"))

    updated = await _job(db_factory, job.id)
    assert (updated.status, updated.error) == ("failed", t("ai.job_failed"))
    assert [s.state for s in await _files_of(db_factory, job.id)] == ["failed", "failed"]


@pytest.mark.asyncio
async def test_batch_creates_notification_on_complete(session, db_factory, company, user):
    from celerp.ai.conversations import create_conversation
    file_ids = _create_test_files(company.id, user.id, 2)
    conv = await create_conversation(session, company.id, user.id, title="Receipts")
    job = await create_batch_job(
        session, company.id, user.id, "analyze", file_ids, conversation_id=conv.id,
    )
    await session.commit()

    mock_create_notif = AsyncMock()
    with patch("celerp.notifications.service.create", mock_create_notif):
        await _run(job.id, _model_result("Done"))

    # create(session, company_id, category, title, body, ...)
    mock_create_notif.assert_called_once()
    call_args = mock_create_notif.call_args
    assert call_args[0][3] == "2 of 2 file(s) read"
    assert call_args[1]["priority"] == "high"
    assert call_args[1]["action_url"] == f"/ai?conversation={conv.id}"


@pytest.mark.asyncio
async def test_batch_progress_event_per_file(session, db_factory, company, user):
    """Each file that ends tells the user's open pages, counted from the job's steps."""
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    events = []

    async def _publish(company_id, user_id, event):
        events.append(event)
    with patch("celerp.notifications.sse.publish", _publish):
        await _run(job.id, _model_result("Done"))

    assert sorted(e["file_id"] for e in events) == sorted(file_ids)
    # Files end concurrently, so two events may read the same count; the last reads all.
    assert all(e["total"] == 3 and 1 <= e["completed"] <= 3 for e in events)
    assert max(e["completed"] for e in events) == 3


@pytest.mark.asyncio
async def test_batch_progress_failure_does_not_send_a_file_again(session, db_factory, company, user):
    """A progress event that cannot be counted or published is skipped: the file's
    result is already written, so it is never sent again for it."""
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()
    sent = []

    def _reply(op):
        sent.append(op)
        return _model_result("Done")
    with patch("celerp.ai.jobs.job_steps", side_effect=RuntimeError("count failed")):
        await _run(job.id, _reply)

    assert (await _job(db_factory, job.id)).status == "completed"
    assert sorted(sent) == sorted(jobs.file_operation_id(job.id, f, 0) for f in file_ids)


@pytest.mark.asyncio
async def test_batch_notification_failure_handled(session, db_factory, company, user):
    """Notification creation failure is caught and logged."""
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    with patch("celerp.notifications.service.create", new_callable=AsyncMock,
               side_effect=RuntimeError("notification error")):
        await _run(job.id, _model_result("Done"))

    assert (await _job(db_factory, job.id)).status == "completed"


@pytest.mark.asyncio
async def test_batch_missing_file_handled(session, db_factory, company, user):
    """Files that don't exist are reported as errors, never sent and never charged."""
    job = await create_batch_job(session, company.id, user.id, "analyze", ["ai_up_nonexistent1", "ai_up_nonexistent2"])
    await session.commit()

    await _run(job.id, AssertionError("a missing file is never sent"))

    assert (await _job(db_factory, job.id)).status == "failed"
    steps = await _files_of(db_factory, job.id)
    assert [(s.state, s.credits, s.result["error"]) for s in steps] == [("failed", 0, t("error.file_gone"))] * 2


# ── owned_job ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_status_endpoint(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"])
    await session.commit()

    result = await owned_job(session, job.id, company_id=company.id, user_id=user.id, shape="documents")
    assert result is not None
    assert result.id == job.id


@pytest.mark.asyncio
async def test_batch_status_wrong_company(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"])
    await session.commit()

    result = await owned_job(session, job.id, company_id=uuid.uuid4(), user_id=user.id, shape="documents")
    assert result is None


@pytest.mark.asyncio
async def test_batch_status_wrong_user(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1"])
    await session.commit()

    result = await owned_job(session, job.id, company_id=company.id, user_id=uuid.uuid4(), shape="documents")
    assert result is None


@pytest.mark.asyncio
async def test_batch_credits_from_relay_usage(session, db_factory, company, user):
    """Each file is charged what the service metered for it; a file the service
    refused is charged nothing and shows the service's reason."""
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()
    refused = jobs.file_operation_id(job.id, file_ids[2], 0)

    await _run(job.id, lambda op: RelayError("bad_request", "The file could not be read.", status=400)
               if op == refused else _model_result("ok", credits=2))

    steps = await _files_of(db_factory, job.id)
    assert [(s.state, s.credits) for s in steps] == [("done", 2), ("done", 2), ("failed", 0)]
    assert steps[2].result["error"] == "The file could not be read."


@pytest.mark.asyncio
async def test_batch_result_carries_extraction(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 1)
    job = await create_batch_job(session, company.id, user.id, "", file_ids)
    await session.commit()

    answer = 'Here it is:\n```json\n{"vendor_name": "Shop", "total": 12.5, "line_items": []}\n```'
    await _run(job.id, _model_result(answer))

    (step,) = await _files_of(db_factory, job.id)
    assert step.result["extraction"] == {"vendor_name": "Shop", "total": 12.5, "line_items": []}
    assert step.result["answer"] == answer


def test_parse_extraction_forms():
    assert parse_extraction('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_extraction('{"a": 1}') == {"a": 1}
    assert parse_extraction('Sure. {"a": {"b": 2}} done') == {"a": {"b": 2}}
    assert parse_extraction("The image is blank.") is None
    assert parse_extraction("[1, 2]") is None


@pytest.mark.asyncio
async def test_batch_total_failure_records_error(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    await _run(job.id, httpx.ReadTimeout("slow"))

    updated = await _job(db_factory, job.id)
    assert (updated.status, updated.error) == ("failed", t("ai.job_failed"))
    assert all("did not answer in time" in s.result["error"] for s in await _files_of(db_factory, job.id))


@pytest.mark.asyncio
async def test_batch_lost_answer_is_asked_again_under_the_files_id(session, db_factory, company, user):
    """Each file is asked under an id of its own within the job. A file whose answer
    timed out is asked again once under that id and gets the answer the service kept;
    a file whose outcome the service reports unknown keeps its charge and is not asked
    again (that id only ever gets the same outcome); a file the service refused is not
    asked again and not charged. The usage record holds one row per file's id."""
    from sqlalchemy import select
    from celerp.models.ai import AIUsage

    file_ids = _create_test_files(company.id, user.id, 4)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()
    file_of = {jobs.file_operation_id(job.id, fid, 0): fid for fid in file_ids}
    # The service's reply to the retry adds nothing and names the file's whole charge.
    replayed = _model_result("Extracted", credits=0)
    replayed.operation_credits = 1
    replies = {
        file_ids[0]: [_model_result("Extracted", credits=1)],
        file_ids[1]: [httpx.ReadTimeout("slow"), replayed],
        file_ids[2]: [RelayError("bad_request", "refused", status=400)],
        file_ids[3]: [RelayError("outcome_unknown", "lost", status=503, credits=1)],
    }
    asked: list = []

    def _reply(operation_id):
        asked.append(file_of[operation_id])
        return replies[file_of[operation_id]].pop(0)

    await _run(job.id, _reply)

    assert sorted(asked) == sorted([file_ids[0], file_ids[1], file_ids[1], file_ids[2], file_ids[3]])
    steps = await _files_of(db_factory, job.id)
    assert [(s.state, s.credits) for s in steps] == [("done", 1), ("done", 1), ("failed", 0), ("unknown", 1)]
    async with db_factory() as s:
        usage = (await s.execute(select(AIUsage))).scalars().all()
    assert {file_of[u.operation_id]: u.credits for u in usage} == {
        file_ids[0]: 1, file_ids[1]: 1, file_ids[2]: 0, file_ids[3]: 1}
    assert {(u.company_id, u.user_id) for u in usage} == {(company.id, user.id)}
