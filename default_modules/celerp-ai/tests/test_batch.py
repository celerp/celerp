# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/batch.py"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base
from celerp.models.company import Company, User
from celerp.models.ai import AIBatchJob
from celerp.config import settings
from celerp.ai.batch import (
    INTERRUPTED_ERROR,
    MAX_BATCH_FILES,
    create_batch_job,
    fail_interrupted_jobs,
    get_batch_job,
    parse_extraction,
    run_batch,
)
from celerp.ai.llm import ModelResult, RelayError


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
    """Async Postgres engine on a fresh isolated schema. run_batch opens its own
    sessions via db_factory, so it needs a real multi-session engine rather than
    the rollback-isolated root session."""
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
    """Returns a callable that produces session context managers (for run_batch)."""
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _make():
        async with factory() as sess:
            yield sess

    return _make


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


# ── create_batch_job ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_creates_job_pending(session, company, user):
    file_ids = ["f1", "f2", "f3"]
    job = await create_batch_job(session, company.id, user.id, "process these", file_ids)
    await session.commit()

    assert job.id is not None
    assert job.status == "pending"
    assert job.total_files == 3
    assert job.completed_files == 0
    assert job.failed_files == 0
    assert job.credits_consumed == 0


@pytest.mark.asyncio
async def test_batch_max_files_enforced(session, company, user):
    file_ids = [f"f{i}" for i in range(MAX_BATCH_FILES + 1)]
    with pytest.raises(ValueError, match="Maximum"):
        await create_batch_job(session, company.id, user.id, "too many", file_ids)


@pytest.mark.asyncio
async def test_batch_deduplicates_same_file_id(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "duplicates", ["f1", "f1", "f2", "f1"])
    assert list(job.file_ids) == ["f1", "f2"]
    assert job.total_files == 2


@pytest.mark.asyncio
async def test_batch_single_file_allowed(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "one receipt", ["f1"])
    assert job.total_files == 1


@pytest.mark.asyncio
async def test_batch_empty_rejected(session, company, user):
    with pytest.raises(ValueError, match="at least one"):
        await create_batch_job(session, company.id, user.id, "none", [])


# ── run_batch ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_all_success(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value=_model_result("Extracted data")):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    # Re-fetch from DB
    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "completed"
        assert updated.completed_files == 3
        assert updated.failed_files == 0
        assert updated.completed_at is not None
        assert len(updated.results["files"]) == 3


@pytest.mark.asyncio
async def test_batch_partial_failure(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 3)

    call_count = 0

    async def _mock_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("API error")
        return _model_result("Success")

    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    with patch("celerp.ai.batch.call_llm", side_effect=_mock_llm):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "completed"  # partial success = completed
        assert updated.completed_files == 2
        assert updated.failed_files == 1


@pytest.mark.asyncio
async def test_batch_total_failure(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, side_effect=RuntimeError("All fail")):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "failed"
        assert updated.failed_files == 2


@pytest.mark.asyncio
async def test_batch_creates_notification_on_complete(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    mock_create_notif = AsyncMock()
    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value=_model_result("Done")):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            with patch("celerp.notifications.service.create", mock_create_notif):
                await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    # Notification should have been called
    # create(session, company_id, category, title, body, ...)
    mock_create_notif.assert_called_once()
    call_args = mock_create_notif.call_args
    title = call_args[0][3]  # 4th positional: title
    assert title == "2 of 2 file(s) read"
    assert call_args[1]["priority"] == "high"


@pytest.mark.asyncio
async def test_batch_progress_callback(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    progress_events = []

    async def on_progress(job_id, completed, failed, total, result):
        progress_events.append((completed, failed, total))

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value=_model_result("Done")):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(
                job.id, company.id, user.id, "analyze", file_ids, db_factory,
                on_progress=on_progress,
            )

    assert len(progress_events) == 3
    # Last event should show all completed
    last = progress_events[-1]
    assert last[0] + last[1] == last[2]  # completed + failed = total


@pytest.mark.asyncio
async def test_batch_missing_file_handled(session, db_factory, company, user):
    """Files that don't exist are reported as errors, not crashes."""
    file_ids = ["ai_up_nonexistent1", "ai_up_nonexistent2"]
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "failed"
        assert updated.failed_files == 2


# ── get_batch_job ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_status_endpoint(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"])
    await session.commit()

    result = await get_batch_job(session, job.id, company.id, user.id)
    assert result is not None
    assert result.id == job.id


@pytest.mark.asyncio
async def test_batch_status_wrong_company(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"])
    await session.commit()

    result = await get_batch_job(session, job.id, uuid.uuid4(), user.id)
    assert result is None


@pytest.mark.asyncio
async def test_batch_status_wrong_user(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1"])
    await session.commit()

    result = await get_batch_job(session, job.id, company.id, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_batch_credits_from_relay_usage(session, db_factory, company, user):
    """Credits are the sum of what the gateway metered per file that was read."""
    file_ids = _create_test_files(company.id, user.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    calls = 0

    async def _mock_llm(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RelayError("busy", "The AI service is temporarily busy.", status=503)
        return _model_result("ok", credits=2)

    with patch("celerp.ai.batch.call_llm", side_effect=_mock_llm):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.credits_consumed == 4
        errors = [f["error"] for f in updated.results["files"] if f["status"] == "error"]
        assert errors == ["The AI service is temporarily busy."]


@pytest.mark.asyncio
async def test_batch_result_carries_extraction(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, user.id, 1)
    job = await create_batch_job(session, company.id, user.id, "", file_ids)
    await session.commit()

    answer = 'Here it is:\n```json\n{"vendor_name": "Shop", "total": 12.5, "line_items": []}\n```'
    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value=_model_result(answer)):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(job.id, company.id, user.id, "", file_ids, db_factory)

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        entry = updated.results["files"][0]
        assert entry["extraction"] == {"vendor_name": "Shop", "total": 12.5, "line_items": []}
        assert entry["answer"] == answer


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

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "failed"
        assert updated.error == "None of the files could be read."
        assert all("did not answer in time" in f["error"] for f in updated.results["files"])


@pytest.mark.asyncio
async def test_interrupted_jobs_marked_failed_on_startup(session, company, user):
    pending = await create_batch_job(session, company.id, user.id, "a", ["f1"])
    running = await create_batch_job(session, company.id, user.id, "b", ["f2"])
    running.status = "running"
    done = await create_batch_job(session, company.id, user.id, "c", ["f3"])
    done.status = "completed"
    await session.commit()

    assert await fail_interrupted_jobs(session) == 2
    await session.commit()

    for job_id in (pending.id, running.id):
        job = await session.get(AIBatchJob, job_id)
        assert job.status == "failed"
        assert job.error == INTERRUPTED_ERROR
        assert job.completed_at is not None
    assert (await session.get(AIBatchJob, done.id)).status == "completed"
    assert await fail_interrupted_jobs(session) == 0


@pytest.mark.asyncio
async def test_batch_on_progress_failure_handled(session, db_factory, company, user):
    """on_progress callback failure is caught and logged, not propagated."""
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    async def failing_progress(*args):
        raise RuntimeError("progress callback exploded")

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value=_model_result("Done")):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await run_batch(
                job.id, company.id, user.id, "analyze", file_ids, db_factory,
                on_progress=failing_progress,
            )

    # Batch should still complete despite callback failure
    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "completed"


@pytest.mark.asyncio
async def test_batch_notification_failure_handled(session, db_factory, company, user):
    """Notification creation failure is caught and logged."""
    file_ids = _create_test_files(company.id, user.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids)
    await session.commit()

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value=_model_result("Done")):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock, side_effect=RuntimeError("notification error")):
            await run_batch(
                job.id, company.id, user.id, "analyze", file_ids, db_factory,
            )

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "completed"
