# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The app's own Python sources: the one root list every guard that scans the source reads, so
no guard stops short of a module. At the repository root, so a module's own tests import it too."""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parent
CORE_ROOT = "celerp"
MODULE_ROOT = "default_modules"
UI_ROOT = "ui"
# Where modules installed beside the bundled ones live, in a checkout that carries them.
PREMIUM_ROOT = "premium_modules"
# Where the app's server code lives: the core and its bundled modules. The UI reaches the
# database only through the app's API.
SHIPPED_ROOTS = (CORE_ROOT, MODULE_ROOT)
# The server code and the UI.
APP_ROOTS = (CORE_ROOT, UI_ROOT, MODULE_ROOT)


def shipped_sources() -> list[tuple[str, pathlib.Path]]:
    """(path relative to the repo, path) for every Python file under SHIPPED_ROOTS, sorted, tests
    left out."""
    return [(rel.as_posix(), path) for root in SHIPPED_ROOTS for path in sorted((REPO / root).rglob("*.py"))
            for rel in [path.relative_to(REPO)]
            if not any(p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.parts)]


def ui_sources() -> list[pathlib.Path]:
    """Every UI Python file: the core UI and each bundled module's UI files."""
    return sorted({*(REPO / UI_ROOT).glob("**/*.py"), *(REPO / MODULE_ROOT).glob("*/*/ui*.py"),
                   *(REPO / MODULE_ROOT).glob("*/*/ui/**/*.py")})
