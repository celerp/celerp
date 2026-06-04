# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Module reconciliation: diff enabled_modules against on-disk package dirs.

Used by the restore flow to surface missing modules to the user before they
hit a broken dashboard. The audit is read-only and pure — it never installs
anything. Installation is a separate concern (see celerp.modules.ensure, not
yet built).

Lookup walks the same directories the loader does (MODULE_DIR env var,
comma-separated). On darwin the loader also gets CELERP_TRUSTED_MODULE_DIRS
but the audit treats every dir the same: a dir is a dir, package is named
after the dir.
"""
from __future__ import annotations

import os
from pathlib import Path


def _module_dirs() -> list[Path]:
    """Return the list of module directories the loader will scan.

    Mirrors the order from celerp.modules.loader.load_all().
    """
    raw = os.environ.get("MODULE_DIR", "")
    return [Path(d.strip()) for d in raw.split(",") if d.strip()]


def _installed_package_names() -> set[str]:
    """Return the set of package directory names that exist on disk.

    A 'package' is a directory containing an __init__.py. Mirrors the loader
    behavior at celerp/modules/loader.py:226-229.
    """
    found: set[str] = set()
    for d in _module_dirs():
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_dir():
                continue
            if not (p / "__init__.py").is_file():
                continue
            found.add(p.name)
    return found


def audit_missing_modules(enabled: set[str] | list[str]) -> list[str]:
    """Return the sorted list of enabled module names not present on disk.

    This is read-only: it does NOT install anything. The install pass
    (future) lives in celerp.modules.ensure.

    Args:
        enabled: set or list of module names that should be running.

    Returns:
        Sorted list of names that have no on-disk package. Empty means
        every enabled module has a package ready to load.
    """
    enabled_set = set(enabled)
    installed = _installed_package_names()
    missing = enabled_set - installed
    return sorted(missing)
