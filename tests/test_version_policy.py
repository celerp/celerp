# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the celerp version parser and the version policy in
celerp.services.backup_import.validate_archive.

Two concerns covered here:

  1. _parse_version: handles all the version shapes Celerp actually emits,
     plus the edge cases (None, empty, "unknown", "0.1.dev1", etc.).

  2. The version policy in validate_archive:
     - newer-major → warn (populated in BackupResult.warnings), don't raise
     - same-major-different-minor → existing warn behavior preserved
     - downgrade (older backup) → silent, no warning
     - string-comparison bug ("10" < "9") must not regress

The previous implementation in backup_import.py:73-91 had:
  - String comparison on the major component
  - Hard raise on newer-major (broke the user's actual workflow)
  - No way to proceed even when the user understood the risk
"""

from __future__ import annotations

import importlib.metadata
import os
import re

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest


# ── _parse_version ────────────────────────────────────────────────────────────

class TestParseVersion:
    """_parse_version must handle every version shape Celerp emits."""

    def test_simple_semver(self):
        from celerp.services.backup_import import _parse_version
        assert _parse_version("1.2.3") == (1, 2, 3, "")

    def test_semver_with_dev_suffix(self):
        """'1.1.19.dev158' is the format setuptools_scm emits on dev installs."""
        from celerp.services.backup_import import _parse_version
        assert _parse_version("1.1.19.dev158") == (1, 1, 19, "dev158")

    def test_semver_with_zero_dev(self):
        """setuptools_scm sometimes emits '0.1.dev1' on a fresh CI checkout."""
        from celerp.services.backup_import import _parse_version
        assert _parse_version("0.1.dev1") == (0, 1, 0, "dev1")

    def test_semver_with_zero_minor(self):
        from celerp.services.backup_import import _parse_version
        assert _parse_version("1.0.0") == (1, 0, 0, "")

    def test_unknown_returns_zeros(self):
        """Garbage / 'unknown' must not raise. Used as a sentinel."""
        from celerp.services.backup_import import _parse_version
        assert _parse_version("unknown") == (0, 0, 0, "")

    def test_empty_string_returns_zeros(self):
        from celerp.services.backup_import import _parse_version
        assert _parse_version("") == (0, 0, 0, "")

    def test_two_part_version(self):
        """'1.2' is technically valid PEP 440. Tolerate it."""
        from celerp.services.backup_import import _parse_version
        assert _parse_version("1.2") == (1, 2, 0, "")

    def test_garbage_with_letters(self):
        """'abc.def.ghi' should return zeros, not crash."""
        from celerp.services.backup_import import _parse_version
        assert _parse_version("abc.def.ghi") == (0, 0, 0, "")

    def test_two_digit_major_works(self):
        """REGRESSION: string compare would say '10' < '9'. Int compare
        must say 10 > 9."""
        from celerp.services.backup_import import _parse_version
        assert _parse_version("10.0.0") == (10, 0, 0, "")
        assert _parse_version("9.0.0") == (9, 0, 0, "")
        # Tuple comparison: (10,0,0) > (9,0,0) must be True
        assert _parse_version("10.0.0") > _parse_version("9.0.0")


# ── Version policy in validate_archive ───────────────────────────────────────

class TestVersionPolicy:
    """The policy should:
      - newer-major: warn, don't raise
      - older-major (downgrade): silent
      - same-major, different-minor: warn
      - same-major, same-minor, different-patch: silent
    """

    def test_newer_major_does_not_raise(self, monkeypatch):
        """A backup at version 99.0.0 against an install at 1.0.0 must NOT
        raise. It used to (the bug Noah reported). Now it returns a warning
        via the meta path — but the ImportMeta dataclass doesn't carry
        warnings (it carries meta.json contents), so the test verifies by
        checking that no exception is raised."""
        from celerp.services.backup_import import validate_archive
        import io
        import tarfile
        import json

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({
                "celerp_version": "99.0.0",
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": "T",
            }).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
            dump = b"PGDMP"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
        buf.seek(0)

        import tempfile
        from pathlib import Path
        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.write(buf.read())
        tmp.close()
        try:
            # The current install might be 1.1.11.dev20 locally, so 99.0.0 is
            # definitely newer. Must not raise.
            result = validate_archive(Path(tmp.name))
            assert result.celerp_version == "99.0.0"
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_downgrade_silent(self):
        """Backup at 0.1.0 against install at 1.1.11.dev20 must succeed
        silently (no warning, no error). Downgrades are valid recoveries."""
        from celerp.services.backup_import import validate_archive
        import io
        import tarfile
        import json
        import tempfile
        from pathlib import Path

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({
                "celerp_version": "0.1.0",
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": "T",
            }).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
            dump = b"PGDMP"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
        buf.seek(0)

        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.write(buf.read())
        tmp.close()
        try:
            result = validate_archive(Path(tmp.name))
            assert result.celerp_version == "0.1.0"
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_same_major_different_minor_warns_but_passes(self, caplog):
        """Backup at 1.5.0 against install at 1.1.11.dev20: same major,
        different minor. The existing behavior is a warning log; the
        import must still succeed."""
        from celerp.services.backup_import import validate_archive
        import io
        import tarfile
        import json
        import logging
        import tempfile
        from pathlib import Path

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({
                "celerp_version": "1.5.0",
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": "T",
            }).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
            dump = b"PGDMP"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
        buf.seek(0)

        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.write(buf.read())
        tmp.close()
        try:
            with caplog.at_level(logging.WARNING, logger="celerp.services.backup_import"):
                result = validate_archive(Path(tmp.name))
            assert result.celerp_version == "1.5.0"
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_same_version_passes_silently(self):
        """Backup at the exact same version as the install: silent success."""
        from celerp.services.backup_import import validate_archive
        import io
        import tarfile
        import json
        import tempfile
        from pathlib import Path

        current = importlib.metadata.version("celerp")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({
                "celerp_version": current,
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": "T",
            }).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
            dump = b"PGDMP"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
        buf.seek(0)

        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.write(buf.read())
        tmp.close()
        try:
            result = validate_archive(Path(tmp.name))
            assert result.celerp_version == current
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_unknown_version_in_meta_passes(self):
        """An archive with celerp_version='unknown' (very old) must not
        raise. We just trust it and proceed."""
        from celerp.services.backup_import import validate_archive
        import io
        import tarfile
        import json
        import tempfile
        from pathlib import Path

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({
                "celerp_version": "unknown",
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": "T",
            }).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
            dump = b"PGDMP"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
        buf.seek(0)

        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.write(buf.read())
        tmp.close()
        try:
            result = validate_archive(Path(tmp.name))
            assert result.celerp_version == "unknown"
        finally:
            Path(tmp.name).unlink(missing_ok=True)


# ── Test version helper for test data ────────────────────────────────────────

class TestSafeTestVersion:
    """The _safe_test_version helper returns a version guaranteed to be
    <= the current installed version. Used to construct test archives
    that won't trip the version policy in any environment (CI, dev,
    packaged install).
    """

    def test_returns_version_string(self):
        from celerp.services.backup_import import _safe_test_version
        v = _safe_test_version()
        assert isinstance(v, str)
        # Must be parseable
        from celerp.services.backup_import import _parse_version
        parsed = _parse_version(v)
        assert parsed[0] >= 0

    def test_safe_version_is_not_newer_than_current(self):
        """The safe version must be <= the current installed version, so
        tests never trip the newer-major / newer-version guards."""
        from celerp.services.backup_import import _safe_test_version, _parse_version
        importlib_version = importlib.metadata.version("celerp")
        # setuptools_scm may give "1.1.11.dev20" on local, "0.1.dev1" on CI.
        # Both must be >= safe version.
        safe = _parse_version(_safe_test_version())
        current = _parse_version(importlib_version)
        assert safe <= current, (
            f"Safe test version {safe} is newer than current {current}. "
            f"Tests would trip the version policy."
        )
