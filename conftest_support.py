# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Side-effect-free helpers for the root conftest.

Kept in a separate module (never collected as a test, never provisions a
database on import) so the per-worker config-isolation rule can be unit tested
without importing the root conftest, which spins up Postgres at import time.
"""
from __future__ import annotations

import os
import tempfile

_CONFIG_PREFIX = "celerp-test-config-"


def is_own_test_config(path: str | None, tmpdir: str | None = None) -> bool:
    """True for a config path this harness itself created (our per-worker temp).

    Used to tell an inherited value we set from a genuine external CELERP_CONFIG:
    only the former may be recomputed per worker, the latter is always honored.
    """
    if path is None:
        return False
    tmpdir = tmpdir or tempfile.gettempdir()
    return os.path.dirname(path) == tmpdir and os.path.basename(path).startswith(_CONFIG_PREFIX)


def resolve_worker_config(existing: str | None, worker: str, tmpdir: str | None = None) -> str:
    """Return the config.toml path this worker must use.

    An xdist worker inherits the controller process's environment, where the root
    conftest already ran and set CELERP_CONFIG to the controller's ("main") path.
    A plain setdefault would then leave every worker pointing at that one shared
    file, so concurrent read/write across workers corrupts it mid-write (a torn
    file fails tomllib.load and 500s unrelated requests). So whenever the inherited
    value is one of our own temp paths (or unset), recompute it for THIS worker; a
    genuine external CELERP_CONFIG is always left untouched.
    """
    tmpdir = tmpdir or tempfile.gettempdir()
    own = os.path.join(tmpdir, f"{_CONFIG_PREFIX}{worker}.toml")
    if existing is None or is_own_test_config(existing, tmpdir):
        return own
    return existing
