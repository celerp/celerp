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
_DATA_PREFIX = "celerp-test-data-"


def _is_own(path: str | None, prefix: str, tmpdir: str | None) -> bool:
    if path is None:
        return False
    tmpdir = tmpdir or tempfile.gettempdir()
    return os.path.dirname(path) == tmpdir and os.path.basename(path).startswith(prefix)


def is_own_test_config(path: str | None, tmpdir: str | None = None) -> bool:
    """True for a config path this harness itself created (our per-worker temp).

    Used to tell an inherited value we set from a genuine external CELERP_CONFIG:
    only the former may be recomputed per worker, the latter is always honored.
    """
    return _is_own(path, _CONFIG_PREFIX, tmpdir)


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


def resolve_worker_data_dir(existing: str | None, worker: str, tmpdir: str | None = None) -> str:
    """Return the data directory (DATA_DIR) this worker must use.

    Unset, settings.data_dir is the relative ./data of the checkout, shared by
    every worker and by any app booted from the same checkout. Runtime state kept
    there (uploads, caches, the System Recovery in-progress marker that puts the
    API into maintenance) would then cross from one process into another. Same
    rule as resolve_worker_config: our own temp path or unset is recomputed per
    worker, a genuine external DATA_DIR is always honored.
    """
    tmpdir = tmpdir or tempfile.gettempdir()
    own = os.path.join(tmpdir, f"{_DATA_PREFIX}{worker}")
    if existing is None or _is_own(existing, _DATA_PREFIX, tmpdir):
        return own
    return existing


def provision_worker_db(url: str, worker: str) -> str:
    """Create an empty database <base>_<worker> on the shared server; return its asyncpg URL.

    The worker database is the run's own throwaway, but its name is reused by every
    run against the same base. A run that stopped before its teardown leaves rows
    behind (one leftover User makes every later first-user registration answer
    "System already bootstrapped"), and create_all never changes a leftover table.
    So an existing one is dropped and created again. Only connections this role
    holds on it are ended first; the base database itself is never touched.
    """
    import re
    from urllib.parse import urlsplit, urlunsplit
    import psycopg2

    parts = urlsplit(url.replace("+asyncpg", ""))
    base_db = parts.path.lstrip("/") or "postgres"
    worker_db = f"{base_db}_{re.sub(r'[^a-zA-Z0-9]', '', worker)}"
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=base_db)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid() AND usename = current_user",
                (worker_db,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{worker_db}"')
            cur.execute(f'CREATE DATABASE "{worker_db}"')
    finally:
        conn.close()
    return urlunsplit(parts._replace(path=f"/{worker_db}")).replace(
        "postgresql://", "postgresql+asyncpg://")
