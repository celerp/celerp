# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Rewrite pyproject.toml so a release wheel requires the exact tested set.

constraints.txt is the full resolved dependency set CI installs and tests. The
ranges in pyproject.toml stay the human-edited source for development; before a
release is built, this script replaces them with the exact pins from
constraints.txt, markers included, so `pip install celerp==X` installs the same
set at any later date.

- [project].dependencies becomes every package reachable from the base
  requirements, pinned.
- Each optional extra whose requirements are all resolved in constraints.txt
  becomes its own additional packages, pinned. Extras that are not resolved
  there (dev tooling) are left as they are.

Usage: python scripts/pin_release_deps.py [--pyproject PATH] [--constraints PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
_SELF = "celerp (pyproject.toml)"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(requirement: str) -> str:
    return _norm(re.split(r"[\s\[<>=!~;]", requirement.strip(), maxsplit=1)[0])


def parse_constraints(text: str) -> dict[str, tuple[list[str], set[str]]]:
    """Map each package to (its pin lines, the set of packages that require it).
    A package resolved differently per Python version or platform has one line
    per marker, and every one of them is kept."""
    pins: dict[str, tuple[list[str], set[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            current = _req_name(line)
            pins.setdefault(current, ([], set()))[0].append(line)
            continue
        if current is None:
            continue
        via = line.lstrip("#").strip()
        if via.startswith("via "):
            via = via[4:].strip()
        if via and via != "via":
            pins[current][1].add(via if via == _SELF else _norm(via))
    return pins


def closure(roots: set[str], pins: dict[str, tuple[list[str], set[str]]]) -> set[str]:
    """Every package in `pins` required, directly or transitively, by `roots`."""
    found = {name for name in roots if name in pins}
    changed = True
    while changed:
        changed = False
        for name, (_, vias) in pins.items():
            if name not in found and vias & found:
                found.add(name)
                changed = True
    return found


def _lines(names: set[str], pins: dict[str, tuple[list[str], set[str]]]) -> list[str]:
    return [line for name in sorted(names) for line in pins[name][0]]


def _replace_list(text: str, header: str, entries: list[str]) -> str:
    """Replace the TOML array that opens on the line `header` with `entries`."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == header), None)
    if start is None:
        raise SystemExit(f"pin_release_deps: '{header}' not found in pyproject.toml")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip() == "]")
    body = [f"    {_toml_str(entry)},\n" for entry in entries]
    return "".join(lines[: start + 1] + body + lines[end:])


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pin(pyproject_text: str, constraints_text: str) -> str:
    project = tomllib.loads(pyproject_text)["project"]
    pins = parse_constraints(constraints_text)

    base_roots = {_req_name(r) for r in project["dependencies"]}
    missing = sorted(base_roots - set(pins))
    if missing:
        raise SystemExit(f"pin_release_deps: not in constraints.txt: {', '.join(missing)}")
    base = closure(base_roots, pins)
    text = _replace_list(pyproject_text, "dependencies = [", _lines(base, pins))

    for extra, requirements in project.get("optional-dependencies", {}).items():
        roots = {_req_name(r) for r in requirements}
        if not roots <= set(pins):
            continue
        own = closure(roots, pins) - base
        text = _replace_list(text, f"{extra} = [", _lines(own, pins))

    tomllib.loads(text)  # the result must still be valid TOML
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, default=REPO_ROOT / "pyproject.toml")
    parser.add_argument("--constraints", type=Path, default=REPO_ROOT / "constraints.txt")
    args = parser.parse_args(argv)
    args.pyproject.write_text(
        pin(args.pyproject.read_text(encoding="utf-8"), args.constraints.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    print(f"Pinned {args.pyproject} from {args.constraints}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
