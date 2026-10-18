# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The outcome of a System Recovery, which signed everyone out, is shown in full to the
installation owner on the first page after signing in again, once; never to anyone else,
and never on the login page, which anyone can open and which only says what happened."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

_OUTCOME = "System Recovery did not complete: DuplicateColumn: column already exists."
_COMPANY = {"id": "c1", "name": "Acme", "settings": {}}


@pytest_asyncio.fixture
async def ui_client(tmp_path, monkeypatch):
    from celerp.config import settings
    from celerp.services import backup_import
    from ui.app import app as ui_app
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    backup_import._keep_recovery_failure(_OUTCOME)
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


async def _page(client, *, install_owner: bool, role: str = "owner") -> str:
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": []})), \
         patch("ui.api_client.installation_owner", new=AsyncMock(return_value=install_owner)):
        r = await client.get("/settings/general?tab=users",
                             cookies={"celerp_token": make_test_token(role=role)})
    assert r.status_code == 200, r.text
    return r.text


@pytest.mark.asyncio
async def test_installation_owner_sees_the_failed_recovery_once_after_signing_in(ui_client):
    first = await _page(ui_client, install_owner=True)
    assert _OUTCOME in first and "flash--error" in first
    assert _OUTCOME not in await _page(ui_client, install_owner=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin", "viewer"])
async def test_the_outcome_waits_for_the_installation_owner(ui_client, role):
    from celerp.services import backup_import
    assert _OUTCOME not in await _page(ui_client, install_owner=False, role=role)
    assert backup_import.take_recovery_failure() == _OUTCOME


@pytest.mark.asyncio
async def test_login_page_does_not_show_the_failed_recovery(ui_client):
    from celerp.services import backup_import
    with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
        r = await ui_client.get("/login")
    assert r.status_code == 200
    assert "DuplicateColumn" not in r.text
    assert backup_import.take_recovery_failure() == _OUTCOME


_SAFETY = "C:\\Users\\\u0e2a\u0e21\u0e0a\u0e32\u0e22\\celerp-data\\recovery-safety\\pre-recovery.celerp-backup"


def _restored(warnings: list[str]) -> None:
    from celerp.services import backup_import
    backup_import.take_recovery_failure()
    backup_import._write_restore_notice(warnings, _SAFETY, False)


@pytest.mark.asyncio
async def test_installation_owner_sees_the_restore_outcome_once_after_signing_in(ui_client):
    _restored(["celerp-fictional is not installed."])
    first = await _page(ui_client, install_owner=True)
    assert "Backup imported." in first and "celerp-fictional is not installed." in first
    assert _SAFETY in first and "flash--warning" in first
    later = await _page(ui_client, install_owner=True)
    assert _SAFETY not in later and "Backup imported." not in later


@pytest.mark.asyncio
async def test_a_clean_restore_outcome_is_a_success_notice(ui_client):
    _restored([])
    first = await _page(ui_client, install_owner=True)
    assert _SAFETY in first and "flash--success" in first


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin", "viewer"])
async def test_the_restore_outcome_waits_for_the_installation_owner(ui_client, role):
    from celerp.services import backup_import
    _restored(["celerp-fictional is not installed."])
    assert _SAFETY not in await _page(ui_client, install_owner=False, role=role)
    assert backup_import.take_restore_notice()["safety_archive"] == _SAFETY
