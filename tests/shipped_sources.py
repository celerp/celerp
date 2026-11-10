# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The app's own Python sources: the one root list every guard that scans the source reads, so
no guard stops short of a module."""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
# Where the app's server code lives: the core and its bundled modules. The UI reaches the
# database only through the app's API.
SHIPPED_ROOTS = ("celerp", "default_modules")


def shipped_sources() -> list[tuple[str, pathlib.Path]]:
    """(path relative to the repo, path) for every Python file under SHIPPED_ROOTS, sorted, tests
    left out."""
    return [(rel.as_posix(), path) for root in SHIPPED_ROOTS for path in sorted((REPO / root).rglob("*.py"))
            for rel in [path.relative_to(REPO)]
            if not any(p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.parts)]
