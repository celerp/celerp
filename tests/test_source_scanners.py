# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every test that walks the app's source reads its roots from shipped_sources, so no
source guard stops short of a module: a scanner with a root tuple of its own fails, even one that
lists every root today, since the next root added would pass it by."""

from __future__ import annotations

import re

from shipped_sources import REPO

# A file walks a source tree when it calls one of these...
_WALKS = re.compile(r"""rglob\(|os\.walk\(|glob\(["']\*\*""")
# ...and names an app root as a path segment.
_ROOTS = re.compile(r"""["'](?:celerp|default_modules)["']""")
_SHARED = re.compile(r"^\s*from shipped_sources import |^\s*import shipped_sources\b", re.M)

# Files that pass for a stated reason.
_ALLOWED: dict[str, str] = {
    "tests/test_company_backup.py": "walks a company backup directory, not the source",
    "tests/test_company_backup_lifecycle.py": "walks the data directory's company backups, not the source",
    "tests/test_system_recovery_exact.py": "walks a backup and the recovery staging directory, not the source",
    "tests/test_modules/test_admission.py": "walks the module packages the test builds",
    "tests/test_regression.py": "reads only ui/, not a server root",
    "tests/test_connectors_proxy.py": "reads only ui/, not a server root",
}


def _test_files() -> list[str]:
    files = [*REPO.glob("tests/**/*.py"), *REPO.glob("default_modules/*/tests/**/*.py")]
    return sorted(p.relative_to(REPO).as_posix() for p in files if "__pycache__" not in p.parts)


def test_source_scanners_cover_every_shipped_root():
    flagged = []
    for rel in _test_files():
        source = (REPO / rel).read_text(encoding="utf-8")
        if _WALKS.search(source) and _ROOTS.search(source) and not _SHARED.search(source) and rel not in _ALLOWED:
            flagged.append(rel)
    assert not flagged, "walk the app's source through shipped_sources:\n" + "\n".join(flagged)
    assert all(_ALLOWED.values())
