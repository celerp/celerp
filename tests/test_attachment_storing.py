# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Files stored inside ``storing()`` and what happens to them when the block or its commit fails."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from celerp.services import attachments

pytestmark = pytest.mark.asyncio

_PDF = b"%PDF-1.4\n%%EOF\n"


def _stored(tmp_path, company_id: str) -> list[str]:
    folder = tmp_path / "static" / "attachments" / company_id
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.fixture
def company(monkeypatch, tmp_path):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(attachments, "hold_company", AsyncMock(return_value=True))
    return "c0ffee00-0000-0000-0000-000000000001"


async def test_a_file_whose_record_fails_is_deleted(company, tmp_path):
    session = AsyncMock()
    with pytest.raises(RuntimeError):
        async with attachments.storing(session, company) as store:
            await store.file(_PDF, "a.pdf", "application/pdf")
            raise RuntimeError("recording refused")
    assert _stored(tmp_path, company) == []
    session.rollback.assert_awaited()


async def test_a_file_whose_commit_landed_and_then_reported_a_failure_is_kept(company, tmp_path):
    """The commit may have landed before the error reached Celerp, so the record can point at
    the file: deleting it then would leave the record pointing at nothing."""
    session = AsyncMock()
    session.commit.side_effect = ConnectionResetError("connection lost after commit")
    with pytest.raises(ConnectionResetError):
        async with attachments.storing(session, company) as store:
            meta = await store.file(_PDF, "a.pdf", "application/pdf")
    assert any(name.startswith(meta["id"]) for name in _stored(tmp_path, company))
