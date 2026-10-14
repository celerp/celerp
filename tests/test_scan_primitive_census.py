# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Documents and lists share one scan primitive. The names of the retired per-scan document
lookup must not appear anywhere in the repo, so a second scan path cannot come back under them."""
from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve().relative_to(_ROOT).as_posix()
_RETIRED = (
    "doc_catalog_lookup", "catalog-lookup", "catalog_lookup", "scan_not_found", "scanned_prefix",
    "ListScanBody", "_HIDDEN_STATUSES", "_celerpStarterRow", "_CELERP_IS_LIST", "_generic_taxes",
)


def check_census_zero_stragglers() -> dict[str, list[str]]:
    """Every tracked line, outside this file, that still names a retired symbol, keyed by name."""
    hits: dict[str, list[str]] = {}
    for name in _RETIRED:
        out = subprocess.run(["git", "grep", "-n", "-I", "-w", "-F", "-e", name, "--", ".", f":!{_SELF}"],
                             cwd=_ROOT, capture_output=True, text=True)
        assert out.returncode in (0, 1), out.stderr
        if out.stdout:
            hits[name] = out.stdout.splitlines()
    return hits


def test_retired_scan_names_are_gone():
    assert check_census_zero_stragglers() == {}
