# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Files stored inside ``storing()`` and what happens to them when the block or its commit fails."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.services import attachments
from company_backup_support import company, owner
from migration_support import maker, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

_PDF = b"%PDF-1.4\n%%EOF\n"


def _stored(tmp_path, company_id: str) -> list[str]:
    folder = tmp_path / "static" / "attachments" / company_id
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.fixture
def held_company(monkeypatch, tmp_path):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(attachments, "hold_company", AsyncMock(return_value=True))
    return "c0ffee00-0000-0000-0000-000000000001"


async def test_a_file_whose_record_fails_is_deleted(held_company, tmp_path):
    session = AsyncMock()
    with pytest.raises(RuntimeError):
        async with attachments.storing(session, held_company) as store:
            await store.file(_PDF, "a.pdf", "application/pdf")
            raise RuntimeError("recording refused")
    assert _stored(tmp_path, held_company) == []
    session.rollback.assert_awaited()


async def _store_and_commit(engine, company_id, commit):
    """Store one file inside ``storing()`` on a real session whose commit is ``commit``."""
    async with maker(engine)() as session:
        session.commit = types.MethodType(commit, session)
        with pytest.raises(OperationalError):
            async with attachments.storing(session, company_id) as store:
                return await store.file(_PDF, "a.pdf", "application/pdf")


async def test_a_file_whose_commit_never_landed_is_deleted(real_engine, monkeypatch, tmp_path):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    company_id = str(await company(real_engine, await owner(real_engine), "Harbor Goods Ltd", "alpha"))

    async def commit(self):
        await AsyncSession.rollback(self)
        raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    await _store_and_commit(real_engine, company_id, commit)
    assert _stored(tmp_path, company_id) == []


async def test_a_file_whose_commit_landed_and_then_reported_a_failure_is_kept(real_engine, monkeypatch,
                                                                              tmp_path):
    """The commit landed before the error reached Celerp, so the record can point at the
    file: deleting it then would leave the record pointing at nothing."""
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    company_id = str(await company(real_engine, await owner(real_engine), "Harbor Goods Ltd", "alpha"))

    async def commit(self):
        await AsyncSession.commit(self)
        raise OperationalError("COMMIT", {}, Exception("connection lost after commit"))

    await _store_and_commit(real_engine, company_id, commit)
    assert _stored(tmp_path, company_id) != []


async def test_a_file_is_kept_when_whether_its_commit_landed_cannot_be_read(real_engine, monkeypatch,
                                                                         tmp_path):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    company_id = str(await company(real_engine, await owner(real_engine), "Harbor Goods Ltd", "alpha"))
    monkeypatch.setattr(attachments, "_commit_landed", AsyncMock(side_effect=OperationalError("x", {}, None)),
                        raising=False)

    async def commit(self):
        raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    await _store_and_commit(real_engine, company_id, commit)
    assert _stored(tmp_path, company_id) != []
