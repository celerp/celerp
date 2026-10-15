# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Documents and lists share one scan primitive. The names of the retired per-scan document
lookup, and the prose that described it, must not appear anywhere in the repo, so a second scan
path cannot come back under them and no comment still documents the old one."""
from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve().relative_to(_ROOT).as_posix()
_RETIRED = (
    "doc_catalog_lookup", "catalog-lookup", "catalog_lookup", "scan_not_found", "scanned_prefix",
    "ListScanBody", "_HIDDEN_STATUSES", "_celerpStarterRow", "_CELERP_IS_LIST", "_generic_taxes",
)
# Phrases (any case) that only described the retired path: a scanner's Enter resolving one code
# through a catalog lookup.
_RETIRED_PROSE = ("scan Enter", "catalog lookup", "Resolve one typed or scanned code")
# The scan endpoints reach inventory through its service interface, never its routes file.
_SCAN_RESOLVER_NAMES = ("HIDDEN_STATUSES", "load_item_index", "duplicate_barcode_detail")


def _git_grep(*args: str) -> list[str]:
    out = subprocess.run(["git", "grep", "-n", "-I", *args, "--", ".", f":!{_SELF}"],
                         cwd=_ROOT, capture_output=True, text=True)
    assert out.returncode in (0, 1), out.stderr
    return out.stdout.splitlines()


def check_census_zero_stragglers() -> dict[str, list[str]]:
    """Every tracked line, outside this file, that still names a retired symbol, keyed by name."""
    hits: dict[str, list[str]] = {}
    for name in _RETIRED:
        if lines := _git_grep("-w", "-F", "-e", name):
            hits[name] = lines
    for phrase in _RETIRED_PROSE:
        if lines := _git_grep("-i", "-F", "-e", phrase):
            hits[phrase] = lines
    return hits


def check_scan_resolver_reach_in() -> list[str]:
    """Every line importing a scan resolver name from the inventory routes file."""
    return [line for line in _git_grep("-F", "-e", "from celerp_inventory.routes import")
            if any(name in line for name in _SCAN_RESOLVER_NAMES)]


def test_retired_scan_names_are_gone():
    assert check_census_zero_stragglers() == {}


def test_scan_resolver_is_reached_through_inventory_services():
    from celerp_inventory import services

    assert check_scan_resolver_reach_in() == []
    assert all(hasattr(services, name) for name in _SCAN_RESOLVER_NAMES)
