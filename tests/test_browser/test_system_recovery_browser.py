# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""System Recovery from a real backup when no safety copy of the current installation can be
made: nothing is changed, the page says why, and the owner can continue without a safety copy
from the same upload. A backup whose database dump is cut short is refused before that."""

from __future__ import annotations

import io
import json
import tarfile
import time

import psycopg2
import pytest

from test_helpers import released_migration_history
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
            "enabled_modules": ["celerp-inventory"]}
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
    with released_migration_history(settings.database_url):
        backup.dump_database(settings.database_url, tmp_path / "database.dump")
    dump = (tmp_path / "database.dump").read_bytes()
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
    page.on("dialog", lambda d: d.accept())
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
    page.on("dialog", lambda d: d.accept())
    page.set_input_files("#backup-import-input", _archive(tmp_path / "cut.celerp-backup", dump[:len(dump) // 8]))
    page.wait_for_selector("#backup-flash.flash--error", timeout=_WAIT_MS)
    flash = page.locator("#backup-flash").inner_text()
    assert "couldn't be restored" in flash and "pg_restore" in flash
    assert page.locator('button:has-text("Restore without safety copy")').count() == 0
    assert set(_company_names(url)) == {_RENAMED}
    assert not (tmp_path / "data" / "static" / "attachments").exists()


def _slow_answer(accept: bool):
    """A dialog handler that answers like a person reading the warning first."""
    def answer(dialog):
        time.sleep(6)
        dialog.accept() if accept else dialog.dismiss()
    return answer


def test_import_backup_opens_the_file_picker_after_a_slow_confirm_browser(page, recovery, tmp_path):
    """Reading the warning for more than a few seconds must not stop the file picker
    from opening: the browser only lets a click open it for a short time."""
    dump, names, url = recovery
    page.on("dialog", _slow_answer(accept=True))
    with page.expect_file_chooser(timeout=_WAIT_MS) as chooser:
        page.click("#backup-import-btn")
    chooser.value.set_files(_archive(tmp_path / "whole.celerp-backup", dump))
    page.locator('button:has-text("Restore without safety copy")').wait_for(timeout=_WAIT_MS)


def test_import_backup_cancelled_at_the_warning_sends_nothing_browser(page, recovery, tmp_path):
    dump, names, url = recovery
    sent = []
    page.on("request", lambda r: sent.append(r.url) if "/backup/import" in r.url else None)
    page.on("dialog", lambda d: d.dismiss())
    with page.expect_file_chooser(timeout=_WAIT_MS) as chooser:
        page.click("#backup-import-btn")
    chooser.value.set_files(_archive(tmp_path / "whole.celerp-backup", dump))
    page.wait_for_timeout(1000)
    assert sent == []
    button = page.locator("#backup-import-btn")
    assert button.is_enabled() and button.inner_text() == t("btn.import_backup", "en")
    assert set(_company_names(url)) == {_RENAMED}


@pytest.fixture
def recorded_history():
    """The live database with its migration history recorded, as every install has it, so
    the safety copy a recovery takes of it can be put back."""
    from celerp.config import settings
    with released_migration_history(settings.database_url):
        yield


def test_failed_recovery_shows_its_outcome_after_signing_in_again_browser(
        page, ui_server, api, seeded_user, keep_sign_ins, recorded_history, tmp_path, monkeypatch):
    """A recovery that fails after it started is put back and signs everyone out. The page
    says so and offers sign-in; the login page says only that it did not complete; the
    first page after signing in again shows the failure, once."""
    import celerp.config as config
    import celerp.routers.system as system
    from celerp.config import settings
    from celerp.services import backup, backup_import

    from .conftest import _set_auth_cookie, clear_session_registry

    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    names = _company_names(url)
    backup.dump_database(settings.database_url, tmp_path / "database.dump")
    dump = (tmp_path / "database.dump").read_bytes()
    real_reconcile = backup_import._reconcile_schema
    failed = []

    async def _reconcile_fails_once():
        if not failed:
            failed.append(True)
            raise RuntimeError("DuplicateColumn: column already exists")
        await real_reconcile()

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "backup_encryption_key", None)
    monkeypatch.setattr(backup_import, "_reconcile_schema", _reconcile_fails_once)
    monkeypatch.setattr(config, "replace_enabled_modules", lambda names: False)
    monkeypatch.setattr(system, "_send_sigterm", lambda: None)
    (tmp_path / "data").mkdir()
    # The safety copy is checked like any backup: it needs the history every install has.
    with released_migration_history(settings.database_url):
        try:
            page.goto(f"{ui_server}/settings/system-recovery", wait_until="domcontentloaded")
            page.on("dialog", lambda d: d.accept())
            page.set_input_files("#backup-import-input", _archive(tmp_path / "whole.celerp-backup", dump))
            page.wait_for_selector("#backup-flash .flash--error", timeout=_WAIT_MS)
            flash = page.locator("#backup-flash").inner_text()
            assert "System Recovery did not complete" in flash and "DuplicateColumn" in flash
            assert t("system_recovery.failed_signed_out", "en") in flash
            assert _company_names(url) == names

            page.click('#backup-flash a:has-text("Sign in")')
            page.wait_for_selector("#email", timeout=_WAIT_MS)
            assert t("auth.recovery_failed_signin", "en") in page.content()
            assert "DuplicateColumn" not in page.content()

            clear_session_registry()
            page.fill("#email", seeded_user["email"])
            page.fill("#password", seeded_user["password"])
            page.click('button[type="submit"]')
            page.wait_for_selector("#main-content", timeout=_WAIT_MS)
            outcome = page.locator("#main-content .flash--error").first
            outcome.wait_for(timeout=_WAIT_MS)
            assert "System Recovery did not complete" in outcome.inner_text()
            assert "DuplicateColumn" in outcome.inner_text()

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#main-content", timeout=_WAIT_MS)
            assert "did not complete" not in page.locator("#main-content").inner_text()
        finally:
            _set_auth_cookie(page.context, api.headers["Authorization"].split(" ", 1)[1])
