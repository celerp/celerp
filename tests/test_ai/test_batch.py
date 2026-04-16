# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/batch.py"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base
from celerp.models.company import Company, User
from celerp.models.ai import AIBatchJob
from celerp.config import settings
from celerp.ai.batch import (
    MAX_BATCH_FILES,
    create_batch_job,
    get_batch_job,
    run_batch,
)

_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


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
    u = User(company_id=company.id, email="batch@test.com", name="Test", role="owner")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


def _create_test_files(company_id: uuid.UUID, count: int = 3) -> list[str]:
    """Create test files on disk and return file IDs."""
    upload_dir = settings.data_dir / "ai_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_ids = []
    for i in range(count):
        fid = f"ai_up_batch_{uuid.uuid4().hex[:8]}"
        (upload_dir / f"{fid}.bin").write_bytes(b"fake image data")
        (upload_dir / f"{fid}.meta").write_text(json.dumps({
            "filename": f"receipt_{i}.jpg",
            "content_type": "image/jpeg",
            "size": 15,
            "company_id": str(company_id),
        }))
        file_ids.append(fid)
    return file_ids


# ── create_batch_job ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_creates_job_pending(session, company, user):
    file_ids = ["f1", "f2", "f3"]
    job = await create_batch_job(session, company.id, user.id, "process these", file_ids, credits=3)
    await session.commit()

    assert job.id is not None
    assert job.status == "pending"
    assert job.total_files == 3
    assert job.completed_files == 0
    assert job.failed_files == 0
    assert job.credits_consumed == 3


@pytest.mark.asyncio
async def test_batch_max_files_enforced(session, company, user):
    file_ids = [f"f{i}" for i in range(MAX_BATCH_FILES + 1)]
    with pytest.raises(ValueError, match="Maximum"):
        await create_batch_job(session, company.id, user.id, "too many", file_ids, credits=101)


@pytest.mark.asyncio
async def test_batch_min_files_enforced(session, company, user):
    with pytest.raises(ValueError, match="at least 2"):
        await create_batch_job(session, company.id, user.id, "too few", ["f1"], credits=1)


# ── run_batch ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_all_success(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=3)
    await session.commit()

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value="Extracted data"):
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
    file_ids = _create_test_files(company.id, 3)

    call_count = 0

    async def _mock_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("API error")
        return "Success"

    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=3)
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
    file_ids = _create_test_files(company.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=2)
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
    file_ids = _create_test_files(company.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=2)
    await session.commit()

    mock_create_notif = AsyncMock()
    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value="Done"):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            with patch("celerp.notifications.service.create", mock_create_notif):
                await run_batch(job.id, company.id, user.id, "analyze", file_ids, db_factory)

    # Notification should have been called
    # create(session, company_id, category, title, body, ...)
    mock_create_notif.assert_called_once()
    call_args = mock_create_notif.call_args
    title = call_args[0][3]  # 4th positional: title
    assert "complete" in title.lower()
    assert call_args[1]["priority"] == "high"


@pytest.mark.asyncio
async def test_batch_progress_callback(session, db_factory, company, user):
    file_ids = _create_test_files(company.id, 3)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=3)
    await session.commit()

    progress_events = []

    async def on_progress(job_id, completed, failed, total, result):
        progress_events.append((completed, failed, total))

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value="Done"):
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
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=2)
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
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"], credits=2)
    await session.commit()

    result = await get_batch_job(session, job.id, company.id)
    assert result is not None
    assert result.id == job.id


@pytest.mark.asyncio
async def test_batch_status_wrong_company(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"], credits=2)
    await session.commit()

    result = await get_batch_job(session, job.id, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_batch_credits_consumed_upfront(session, company, user):
    job = await create_batch_job(session, company.id, user.id, "test", ["f1", "f2"], credits=5)
    await session.commit()
    assert job.credits_consumed == 5


@pytest.mark.asyncio
async def test_batch_on_progress_failure_handled(session, db_factory, company, user):
    """on_progress callback failure is caught and logged, not propagated."""
    file_ids = _create_test_files(company.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=2)
    await session.commit()

    async def failing_progress(*args):
        raise RuntimeError("progress callback exploded")

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value="Done"):
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
    file_ids = _create_test_files(company.id, 2)
    job = await create_batch_job(session, company.id, user.id, "analyze", file_ids, credits=2)
    await session.commit()

    with patch("celerp.ai.batch.call_llm", new_callable=AsyncMock, return_value="Done"):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock, side_effect=RuntimeError("notification error")):
            await run_batch(
                job.id, company.id, user.id, "analyze", file_ids, db_factory,
            )

    async with db_factory() as s:
        updated = await s.get(AIBatchJob, job.id)
        assert updated.status == "completed"
