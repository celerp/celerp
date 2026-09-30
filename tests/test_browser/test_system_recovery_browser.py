# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""System Recovery when no safety copy of the current installation can be made: nothing is
changed, the page says why, and the owner can continue without a safety copy from the same
upload."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

pytestmark = pytest.mark.browser

_WAIT_MS = 30_000


def _archive(path) -> str:
    meta = {"pg_version": "unknown", "company_name": "Harbor Goods Ltd", "enabled_modules": ["celerp-inventory"]}
    with tarfile.open(path, "w:gz") as tar:
        for name, body in (("database.dump", b"SOURCE-DUMP"), ("meta.json", json.dumps(meta).encode()),
                           ("attachments/new.pdf", b"NEW")):
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return str(path)


def test_system_recovery_safety_failure_confirm_browser(page, ui_server, api, tmp_path, monkeypatch):
    import celerp.config as config
    import celerp.routers.system as system
    from celerp.config import settings
    from celerp.services import backup_export, backup_import, session_tracker

    from .conftest import _set_auth_cookie

    restored: list[bytes] = []

    async def _no_export():
        raise RuntimeError("No space left on device")

    async def _restore(dump, url):
        restored.append(dump.read_bytes())

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "backup_encryption_key", None)
    monkeypatch.setattr(backup_export, "export_full", _no_export)
    monkeypatch.setattr(backup_import, "_run_pg_restore", _restore)
    for name in ("_reconcile_schema", "_dispose_engine", "_revoke_current_connector_state",
                 "_clear_restored_connector_state"):
        monkeypatch.setattr(backup_import, name, _none)
    monkeypatch.setattr(session_tracker, "end_all_sessions", _none)
    monkeypatch.setattr(config, "replace_enabled_modules", lambda names: False)
    monkeypatch.setattr(system, "_send_sigterm", lambda: None)
    (tmp_path / "data").mkdir()
    token = api.headers["Authorization"].split(" ", 1)[1]

    try:
        page.goto(f"{ui_server}/settings/system-recovery", wait_until="domcontentloaded")
        page.set_input_files("#backup-import-input", _archive(tmp_path / "whole.celerp-backup"))
        button = page.locator('button:has-text("Restore without safety copy")')
        button.wait_for(timeout=_WAIT_MS)
        flash = page.locator("#backup-flash").inner_text()
        assert "A safety backup could not be made before restoring." in flash
        assert "No space left on device" in flash
        assert "Nothing has been changed" in flash
        assert restored == []
        assert not (tmp_path / "data" / "static" / "attachments").exists()

        button.click()
        page.wait_for_selector('#backup-flash:has-text("Database restored from the recovery point.")',
                               timeout=_WAIT_MS)
        assert restored == [b"SOURCE-DUMP"]
        assert (tmp_path / "data" / "static" / "attachments" / "new.pdf").read_bytes() == b"NEW"
    finally:
        _set_auth_cookie(page.context, token)
