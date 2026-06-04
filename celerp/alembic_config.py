# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Shared alembic.ini lookup for `cli.py` and `backup_import.py`.

`AlembicConfig("alembic.ini")` is CWD-relative. The celerp package can be
launched from many working dirs (dev repo, installed site-packages, frozen
Electron .app, mounted DMG), so we have to probe well-known locations.

This is a single source of truth so both call sites stay in sync. (Before
this, cli.py had the lookup; backup_import.py called AlembicConfig naively
and failed with 'No script_location key found in configuration' whenever
the working dir wasn't the repo root. Regression on 2026-06-04.)
"""
from __future__ import annotations

from pathlib import Path


def find_alembic_ini() -> Path:
    """Return the path to alembic.ini, searching well-known locations.

    Lookup order:
      1. celerp/alembic.ini         (source repo: cli.py runs from celerp/)
      2. alembic.ini                (repo root: dev mode, pytest)
      3. parent of celerp/alembic.ini (installed site-packages layout)
      4. cwd-relative alembic.ini   (last-resort fallback)

    Raises FileNotFoundError with a clear message if no candidate exists.
    """
    pkg_root = Path(__file__).parent  # celerp/
    candidates = [
        pkg_root / "alembic.ini",
        pkg_root.parent / "alembic.ini",
        Path.cwd() / "alembic.ini",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"alembic.ini not found. Searched: {[str(c) for c in candidates]}"
    )


def build_alembic_config() -> "object":
    """Return a configured alembic Config pointing at the resolved ini file.

    Caller must import alembic.config.Config themselves to keep this module
    importable without the alembic dependency at top-level test time.
    """
    from alembic.config import Config
    ini_path = find_alembic_ini()
    return Config(str(ini_path))
