# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The Celerp version policy in celerp.services.backup_import.validate_archive.

A backup made by a newer Celerp than the running one is refused, at any version
level, with a message saying to update Celerp first. Older and same-version
backups, and legacy backups that never recorded a version, restore; a recorded
version that can't be read is refused.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from ui.i18n import t

RUNNING = "2.5.4"


@pytest.fixture(autouse=True)
def _running(monkeypatch):
    import celerp
    monkeypatch.setattr(celerp, "__version__", RUNNING)


def _archive(tmp_path, meta_extra: dict):
    meta = {"pg_version": "16", "created_at": "2026-01-01T00:00:00Z", "company_name": "T", **meta_extra}
    path = tmp_path / "b.celerp-backup"
    with tarfile.open(path, "w:gz") as tar:
        for name, body in (("meta.json", json.dumps(meta).encode()), ("database.dump", b"PGDMP")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


@pytest.mark.parametrize("version", ["3.0.0", "2.6.0", "2.5.5", "2.5.5.dev1", "2.5.4.post1", "10.0.0"])
def test_newer_backup_refused(tmp_path, version):
    from celerp.services.backup_import import validate_archive
    with pytest.raises(ValueError) as exc:
        validate_archive(_archive(tmp_path, {"celerp_version": version}))
    assert f"made with Celerp {version}" in str(exc.value)
    assert f"newer than this copy ({RUNNING})" in str(exc.value)
    assert "Update Celerp, then restore it." in str(exc.value)


@pytest.mark.parametrize("version", ["2.5.4", "2.5.3", "2.4.9", "1.0.0", "0.1.dev1", "2.5.4.dev7", "2.5.4rc1"])
def test_older_or_same_backup_accepted(tmp_path, version):
    from celerp.services.backup_import import validate_archive
    assert validate_archive(_archive(tmp_path, {"celerp_version": version})).celerp_version == version


def test_version_compared_numerically_not_as_text(tmp_path, monkeypatch):
    """"10" sorts before "9" as text; 10.0.0 is newer than 9.0.0."""
    import celerp
    from celerp.services.backup_import import validate_archive
    monkeypatch.setattr(celerp, "__version__", "9.0.0")
    with pytest.raises(ValueError):
        validate_archive(_archive(tmp_path, {"celerp_version": "10.0.0"}))


@pytest.mark.parametrize("meta", [{}, {"celerp_version": None}, {"celerp_version": ""},
                                  {"celerp_version": "unknown"}])
def test_backup_without_a_recorded_version_accepted(tmp_path, meta):
    from celerp.services.backup_import import validate_archive
    assert validate_archive(_archive(tmp_path, meta)).celerp_version == "unknown"


@pytest.mark.parametrize("version", ["abc", "2.x", 7, ["2.5.4"]])
def test_unreadable_recorded_version_refused(tmp_path, version):
    from celerp.services.backup_import import validate_archive
    with pytest.raises(ValueError, match=re.escape(t("error.restore_version_unreadable", "en", recorded=repr(version)))):
        validate_archive(_archive(tmp_path, {"celerp_version": version}))

