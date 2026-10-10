# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""System Recovery from a real backup when no safety copy of the current installation can be
made: nothing is changed, the page says why, and the owner can continue without a safety copy
from the same upload. A backup whose database dump is cut short is refused before that."""

from __future__ import annotations

import io
import json
import tarfile

import psycopg2
import pytest

from ui.i18n import t

pytestmark = pytest.mark.browser

_WAIT_MS = 30_000
_RENAMED = "Renamed After The Backup"


def _company_names(url: str) -> list[str]:
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM companies ORDER BY name")
        return [row[0] for row in cur.fetchall()]


def _rename_companies(url: str, name: str) -> None:
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("UPDATE companies SET name = %s", (name,))


def _archive(path, dump: bytes) -> str:
    from celerp.migrations.compatibility import running_version
    from celerp.services.backup_export import _pg_version
    meta = {"celerp_version": running_version(), "pg_version": _pg_version(),
            "company_name": "Harbor Goods Ltd", "enabled_modules": ["celerp-inventory"]}
    with tarfile.open(path, "w:gz") as tar:
        for name, body in (("database.dump", dump), ("meta.json", json.dumps(meta).encode()),
                           ("attachments/new.pdf", b"NEW")):
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return str(path)


@pytest.fixture
def recovery(page, ui_server, api, tmp_path, monkeypatch):
    """A real dump of this database taken before its companies are renamed, on an
    installation that cannot make a safety copy. Yields (dump, names before, url)."""
    import celerp.config as config
    import celerp.routers.system as system
    from celerp.config import settings
    from celerp.services import backup, backup_export, session_tracker

    from .conftest import _set_auth_cookie

    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    names = _company_names(url)
    dump = backup.dump_database(settings.database_url)
    _rename_companies(url, _RENAMED)

    async def _no_export():
        raise RuntimeError("No space left on device")

    async def _keep_sessions(session):
        await session.commit()

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "backup_encryption_key", None)
    monkeypatch.setattr(backup_export, "export_full", _no_export)
    # The browser suite shares one signed-in session, which the restore would end.
    monkeypatch.setattr(session_tracker, "end_all_sessions", _keep_sessions)
    monkeypatch.setattr(config, "replace_enabled_modules", lambda names: False)
    monkeypatch.setattr(system, "_send_sigterm", lambda: None)
    (tmp_path / "data").mkdir()
    page.goto(f"{ui_server}/settings/system-recovery", wait_until="domcontentloaded")
    try:
        yield dump, names, url
    finally:
        with psycopg2.connect(url) as conn, conn.cursor() as cur:
            for name in names:
                cur.execute("UPDATE companies SET name = %s WHERE name = %s", (name, _RENAMED))
        _set_auth_cookie(page.context, api.headers["Authorization"].split(" ", 1)[1])


def test_system_recovery_safety_failure_confirm_browser(page, recovery, tmp_path):
    dump, names, url = recovery
    page.set_input_files("#backup-import-input", _archive(tmp_path / "whole.celerp-backup", dump))
    button = page.locator('button:has-text("Restore without safety copy")')
    button.wait_for(timeout=_WAIT_MS)
    flash = page.locator("#backup-flash").inner_text()
    assert t("system_recovery.safety_failed", "en", detail="No space left on device") in flash
    assert "Nothing has been changed" in flash
    assert set(_company_names(url)) == {_RENAMED}
    assert not (tmp_path / "data" / "static" / "attachments").exists()

    button.click()
    page.wait_for_selector('#backup-flash:has-text("Database restored from the recovery point.")',
                           timeout=_WAIT_MS)
    assert _company_names(url) == names
    assert (tmp_path / "data" / "static" / "attachments" / "new.pdf").read_bytes() == b"NEW"


def test_system_recovery_refuses_a_cut_short_dump_browser(page, recovery, tmp_path):
    dump, names, url = recovery
    page.set_input_files("#backup-import-input", _archive(tmp_path / "cut.celerp-backup", dump[:len(dump) // 8]))
    page.wait_for_selector("#backup-flash.flash--error", timeout=_WAIT_MS)
    flash = page.locator("#backup-flash").inner_text()
    assert "pg_restore failed" in flash
    assert page.locator('button:has-text("Restore without safety copy")').count() == 0
    assert set(_company_names(url)) == {_RENAMED}
    assert not (tmp_path / "data" / "static" / "attachments").exists()
