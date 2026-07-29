# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Generate default_modules/first_party.lock.json from the git-tracked tree.

The loader trusts a bundled module only when its live content digest matches the
digest committed here (celerp/modules/loader.py:is_first_party). This script is
the single writer of that file: it digests each first-party module from its
git-tracked files - never the raw working tree - so the lock is reproducible from
a clean checkout, which is exactly what CI enforces with `git diff --exit-code`.

    python scripts/gen_first_party_lock.py            # write the lock in place
    python scripts/gen_first_party_lock.py --stdout   # print it, write nothing

Digesting reuses loader.module_content_digest, so the generated digest and the
runtime digest of an installed/seeded copy are computed by one implementation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from celerp.modules.loader import module_content_digest  # noqa: E402

MODULES_ROOT = "default_modules"


def _tracked_paths() -> list[str]:
    """Every git-tracked path under default_modules/, POSIX-relative to the repo."""
    out = subprocess.run(
        ["git", "ls-files", MODULES_ROOT],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def build_lock() -> dict[str, str]:
    """Map each first-party module name to the content digest of its tracked tree.

    A module is a directory directly under default_modules/ that tracks an
    __init__.py. Files are staged into a temp copy of only the tracked tree so the
    digest can never pick up an untracked working-tree file.
    """
    tracked = _tracked_paths()
    module_names = sorted({
        Path(p).parts[1]
        for p in tracked
        if len(Path(p).parts) >= 3 and Path(p).parts[-1] == "__init__.py"
        and len(Path(p).parts) == 3
    })
    lock: dict[str, str] = {}
    staging = Path(tempfile.mkdtemp(prefix="celerp-lock-"))
    try:
        for rel in tracked:
            src = REPO_ROOT / rel
            if not src.is_file():
                continue
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for name in module_names:
            digest = module_content_digest(staging / MODULES_ROOT / name)
            if digest is None:
                raise SystemExit(f"error: could not digest module {name!r}")
            lock[name] = digest
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return lock


def render(lock: dict[str, str]) -> str:
    return json.dumps(lock, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    lock = build_lock()
    text = render(lock)
    if "--stdout" in argv:
        sys.stdout.write(text)
    else:
        (REPO_ROOT / MODULES_ROOT / "first_party.lock.json").write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
