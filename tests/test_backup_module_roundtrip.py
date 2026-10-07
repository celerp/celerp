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


def _archive(path, members: list[tuple[str, bytes]]):
    import json

    from celerp import __version__

    meta = json.dumps({"celerp_version": __version__, "pg_version": "16",
                       "created_at": "2026-06-04T00:00:00Z", "company_name": "T"}).encode()
    with tarfile.open(path, mode="w:gz") as tar:
        for name, body in [("database.dump", b"PGDMP"), ("meta.json", meta), *members]:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


async def _stage_and_swap(archive):
    import asyncio

    from celerp.services import backup_import as bi

    prepared = await bi.prepare_recovery(archive)
    try:
        await asyncio.to_thread(bi._swap_roots, prepared)
    finally:
        bi._remove_staging(prepared.root)


@pytest.mark.asyncio
async def test_recovery_restores_module_code(tmp_path, monkeypatch):
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    archive = _archive(tmp_path / "backup.celerp-backup", [("modules/mymod/__init__.py", b"PLUGIN = 1")])

    await _stage_and_swap(archive)

    restored = tmp_path / "modules" / "mymod" / "__init__.py"
    assert restored.read_bytes() == b"PLUGIN = 1"


@pytest.mark.asyncio
async def test_recovery_never_overwrites_current_first_party_modules(tmp_path, monkeypatch):
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    current = tmp_path / "modules" / "celerp-inventory" / "sentinel.py"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"CURRENT")

    archive = _archive(tmp_path / "backup.celerp-backup", [
        ("modules/celerp-inventory/sentinel.py", b"STALE"),
        ("modules//celerp-inventory/double.py", b"STALE"),
        ("modules/./celerp-inventory/dot.py", b"STALE"),
        ("modules/acme-custom/__init__.py", b"CUSTOM"),
    ])

    await _stage_and_swap(archive)

    assert current.read_bytes() == b"CURRENT"
    assert not (tmp_path / "modules" / "celerp-inventory" / "double.py").exists()
    assert not (tmp_path / "modules" / "celerp-inventory" / "dot.py").exists()
    assert (tmp_path / "modules" / "acme-custom" / "__init__.py").read_bytes() == b"CUSTOM"


def test_protected_module_dir_uses_filesystem_identity(tmp_path):
    from celerp.services.backup_import import _is_protected_module_dir

    root = tmp_path / "modules"
    protected = root / "celerp-inventory"
    protected.mkdir(parents=True)
    names = frozenset({"celerp-inventory"})

    assert _is_protected_module_dir(root, "celerp-inventory", names)

    alias = root / "inventory-alias"
    alias.symlink_to(protected, target_is_directory=True)
    assert _is_protected_module_dir(root, "inventory-alias", names)

    distinct = root / "CELERP-INVENTORY"
    try:
        distinct.mkdir()
    except FileExistsError:
        # Case-insensitive filesystems alias this spelling to the protected dir.
        assert _is_protected_module_dir(root, "CELERP-INVENTORY", names)
    else:
        # Case-sensitive filesystems treat it as a distinct custom module name.
        assert not _is_protected_module_dir(root, "CELERP-INVENTORY", names)
