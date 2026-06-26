# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module entrypoint so `python -m celerp <command>` runs the CLI.

The packaged Electron launcher invokes the bundled Python by module
(`python -m celerp migrate`) rather than via the `celerp` console script,
which is not on PATH inside the app bundle. This mirrors `python -m alembic`.
"""
from __future__ import annotations

from celerp.cli import main

if __name__ == "__main__":
    main()
