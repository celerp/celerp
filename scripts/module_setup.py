#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Module dependency installer.

Run by Electron main.js before starting the Python processes.
Scans DATA_DIR/modules/ for requirements.txt files and pip-installs them
into the running Python environment.

Usage:
    python scripts/module_setup.py --data-dir /path/to/celerp-data [--dry-run]

Exit codes:
    0 — all installs succeeded (or no requirements found)
    1 — one or more modules failed to install (logged; startup continues)

Design:
    - pip install is a no-op if requirements are already satisfied (fast)
    - failures are logged and the module is marked as load-failed
    - never blocks startup; Electron proceeds regardless of exit code
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def install_module_deps(module_dir: Path, dry_run: bool = False) -> dict[str, bool]:
    """Install requirements for all modules in module_dir.

    Returns a dict of {module_name: success}.
    """
    results: dict[str, bool] = {}

    if not module_dir.exists():
        print(f"[module_setup] Module directory {module_dir} does not exist — nothing to do.")
        return results

    for pkg_path in sorted(module_dir.iterdir()):
        if not pkg_path.is_dir():
            continue
        req_file = pkg_path / "requirements.txt"
        if not req_file.exists():
            continue

        pkg_name = pkg_path.name
        # Skip if requirements.txt has no installable lines (blank/comment-only)
        lines = [l.strip() for l in req_file.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
        if not lines:
            print(f"[module_setup] {pkg_name}: requirements.txt is empty — nothing to install", flush=True)
            results[pkg_name] = True
            continue

        print(f"[module_setup] {pkg_name}: installing deps from requirements.txt...", flush=True)

        if dry_run:
            print(f"[module_setup] {pkg_name}: dry-run — skipping pip install")
            results[pkg_name] = True
            continue

        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "-r", str(req_file),
                    "--quiet",
                    "--disable-pip-version-check",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"[module_setup] {pkg_name}: deps installed OK", flush=True)
            results[pkg_name] = True
        except subprocess.CalledProcessError as exc:
            print(
                f"[module_setup] {pkg_name}: pip install FAILED\n"
                f"  stdout: {exc.stdout.strip()}\n"
                f"  stderr: {exc.stderr.strip()}",
                flush=True,
            )
            results[pkg_name] = False

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Celerp module dependencies")
    parser.add_argument("--data-dir", required=True, help="Path to celerp-data directory")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be installed, don't install")
    parser.add_argument("--json", dest="output_json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    module_dir = Path(args.data_dir) / "modules"
    results = install_module_deps(module_dir, dry_run=args.dry_run)

    if args.output_json:
        print(json.dumps(results))

    failed = [name for name, ok in results.items() if not ok]
    if failed:
        print(f"[module_setup] WARNING: {len(failed)} module(s) failed dep install: {failed}", flush=True)
        # Exit 1 but don't abort Electron — it reads exit code for logging only
        return 1

    print(f"[module_setup] Done. {len(results)} module(s) processed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
