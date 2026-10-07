# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Check a packaged default_modules folder against the first-party lock.

    python scripts/check_packaged_modules.py <default_modules> [--lock <lock.json>]

Run with the packaged app folder first on PYTHONPATH so the packaged loader does
the checking. Exits 1 and lists every problem when the folder does not match.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from celerp.modules import loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path)
    parser.add_argument("--lock", type=Path, default=None)
    args = parser.parse_args(argv)
    print(f"checking {args.tree} with {loader.__file__}")
    problems = loader.check_module_tree(args.tree, args.lock)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print("packaged modules match the lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
