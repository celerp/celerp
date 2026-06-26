# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Custom-module code must survive a backup round-trip.

Module code lives on disk under data_dir/modules/ (user data), not in the pg
dump. The backup now bundles it so a restore on another machine has the code to
interpret the module's restored tables — not just the tables.
"""

from __future__ import annotations

import io
import tarfile

import pytest


def test_build_archive_bundles_modules_and_excludes_bytecode(tmp_path):
    from celerp.services.backup_export import _build_archive

    modules = tmp_path / "modules"
    (modules / "mymod" / "__pycache__").mkdir(parents=True)
    (modules / "mymod" / "code.py").write_text("X = 1")
    (modules / "mymod" / "__pycache__" / "code.cpython-312.pyc").write_bytes(b"junk")

    archive = _build_archive(b"DUMP", [modules], {"celerp_version": "test"})
    try:
        with tarfile.open(archive) as tar:
            names = tar.getnames()
    finally:
        archive.unlink()

    assert "modules/mymod/code.py" in names
    # Regenerable bytecode must not be bundled (bloat + stale across machines).
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


@pytest.mark.asyncio
async def test_extract_files_restores_module_code(tmp_path, monkeypatch):
    from celerp.config import settings
    from celerp.services import backup_import as bi

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    # An archive carrying a module file (plus the always-present db dump).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        dump = b"PGDMP"
        di = tarfile.TarInfo("database.dump")
        di.size = len(dump)
        tar.addfile(di, io.BytesIO(dump))
        body = b"PLUGIN = 1"
        mi = tarfile.TarInfo("modules/mymod/__init__.py")
        mi.size = len(body)
        tar.addfile(mi, io.BytesIO(body))
    archive = tmp_path / "backup.celerp-backup"
    archive.write_bytes(buf.getvalue())

    await bi._extract_files(archive)

    restored = tmp_path / "modules" / "mymod" / "__init__.py"
    assert restored.read_bytes() == b"PLUGIN = 1"
