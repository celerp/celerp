# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of what the subscriptions module runs for a company that has turned it off, read
from the code.

The rule it serves: a company that does not use the module runs no unattended mode, and
nothing the module adds app-wide keeps that company from stopping what billed on its own.

- ``slot_doors()``: every handler or render the module's manifest registers, by slot.
- ``after_commit_steps()``: every step the billing pass queues to run after it commits.

Run as a script for the census table: ``python module_off_census.py``.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "default_modules" / "celerp-subscriptions"
PACKAGE = MODULE_DIR / "celerp_subscriptions"


def manifest() -> dict:
    spec = importlib.util.spec_from_file_location("_subscriptions_manifest", MODULE_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PLUGIN_MANIFEST


def slot_doors() -> list[tuple[str, str]]:
    """(slot, target) for every manifest slot entry that names code the app calls."""
    rows = []
    for slot, entries in manifest()["slots"].items():
        for entry in entries if isinstance(entries, list) else [entries]:
            for key in ("handler", "render"):
                if entry.get(key):
                    rows.append((slot, entry[key]))
    return rows


def after_commit_steps() -> list[tuple[str, str]]:
    """(function, step) for every ``report.after_commit.append(step(...))`` in the module."""
    rows = []
    for path in sorted(PACKAGE.glob("*.py")):
        for fn in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "append"
                        and isinstance(c.func.value, ast.Attribute) and c.func.value.attr == "after_commit"
                        and c.args and isinstance(c.args[0], ast.Call)):
                    rows.append((f"{path.stem}.{fn.name}", f"{path.stem}.{ast.unparse(c.args[0].func)}"))
    return sorted(set(rows))


def main() -> None:
    doors = slot_doors()
    print(f"# Manifest slot doors ({len(doors)})")
    for slot, target in doors:
        print(f"SLOT {slot:28} {target}")
    steps = after_commit_steps()
    print(f"\n# After-commit steps of the billing pass ({len(steps)})")
    for fn, step in steps:
        print(f"STEP {fn:28} {step}")


if __name__ == "__main__":
    main()
