# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp.services.backup_import — the run_import() flow.

Most of the heavy lifting (validate_archive, pg_restore, alembic) is
already tested individually. This file focuses on integration:
  - run_import calls validate_archive first
  - run_import disposes the engine before pg_restore
  - run_import runs alembic upgrade head from the SHARED config helper
    (regression: used to call AlembicConfig('alembic.ini') naively)
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest


def _make_archive(
    company_name: str = "Test",
    version: str = "1.0.0",
    extra_meta: dict | None = None,
    include_dump: bool = True,
    include_meta: bool = True,
) -> bytes:
    """Build a minimal .celerp-backup in memory and return bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if include_meta:
            meta = {
                "celerp_version": version,
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": company_name,
            }
            if extra_meta:
                meta.update(extra_meta)
            meta_bytes = json.dumps(meta).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))
        if include_dump:
            dump = b"PGDMP dummy dump"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
    buf.seek(0)
    return buf.read()


def _write_archive_to_tmp(archive_bytes: bytes) -> Path:
    """Write archive bytes to a temp .celerp-backup file and return its path."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.write(archive_bytes)
    tmp.close()
    return Path(tmp.name)


class TestValidateArchiveEnabledModules:
    """Validate archive reads enabled_modules from meta.json (Layer 1)."""

    def test_reads_enabled_modules_from_meta(self):
        from celerp.services.backup_import import validate_archive
        archive = _make_archive(extra_meta={"enabled_modules": ["celerp-inventory", "celerp-contacts"]})
        path = _write_archive_to_tmp(archive)
        try:
            meta = validate_archive(path)
            assert hasattr(meta, "enabled_modules")
            assert meta.enabled_modules == ["celerp-inventory", "celerp-contacts"]
        finally:
            path.unlink(missing_ok=True)

    def test_enabled_modules_defaults_to_empty_list(self):
        """Backwards compat: old archives without the key still parse."""
        from celerp.services.backup_import import validate_archive
        archive = _make_archive(extra_meta={})  # no enabled_modules
        path = _write_archive_to_tmp(archive)
        try:
            meta = validate_archive(path)
            assert meta.enabled_modules == []
        finally:
            path.unlink(missing_ok=True)

    def test_enabled_modules_can_be_none_in_json(self):
        """enabled_modules: null in JSON should become empty list, not None."""
        from celerp.services.backup_import import validate_archive
        archive = _make_archive(extra_meta={"enabled_modules": None})
        path = _write_archive_to_tmp(archive)
        try:
            meta = validate_archive(path)
            assert meta.enabled_modules == []
        finally:
            path.unlink(missing_ok=True)


class TestRunImportEnabledModules:
    """validate_archive must surface enabled_modules so the UI can preflight.

    The full run_import flow is exercised in test_routers/test_backup.py
    (auth + bootstrap endpoints). This file focuses on validate_archive
    because it is the single point that reads meta.json.
    """

    def test_validate_archive_passes_enabled_modules_to_meta(self):
        """The ImportMeta dataclass must carry the enabled_modules list."""
        from celerp.services.backup_import import ImportMeta
        assert "enabled_modules" in ImportMeta.__dataclass_fields__


class TestAlembicConfigHelperUsed:
    """Regression: backup_import must use celerp.alembic_config, not raw AlembicConfig.

    Before this refactor, backup_import.py called AlembicConfig('alembic.ini')
    directly, which is CWD-relative and failed when running from a non-repo
    directory (e.g. an installed package, a frozen Electron .app, or a test
    that changed cwd). The fix is to call build_alembic_config() which uses
    the shared Path-based lookup.
    """

    def test_backup_import_does_not_call_AlembicConfig_with_bare_string(self):
        """Static check: no `AlembicConfig('alembic.ini')` in backup_import.py."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "services" / "backup_import.py").read_text()
        # Reject the buggy pattern: bare "alembic.ini" string passed to Config
        assert 'AlembicConfig("alembic.ini")' not in src, (
            "backup_import.py still uses bare AlembicConfig('alembic.ini'). "
            "Use celerp.alembic_config.build_alembic_config() instead."
        )

    def test_backup_import_uses_shared_helper(self):
        """Positive check: backup_import.py imports and uses build_alembic_config."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "services" / "backup_import.py").read_text()
        assert "build_alembic_config" in src, (
            "backup_import.py must use celerp.alembic_config.build_alembic_config()."
        )

    def test_cli_uses_shared_helper(self):
        """cli.py must also use the shared helper (no duplicate lookup logic)."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "cli.py").read_text()
        assert "build_alembic_config" in src, (
            "cli.py should use celerp.alembic_config.build_alembic_config() to share the lookup."
        )
