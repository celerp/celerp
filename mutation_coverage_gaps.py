# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The writers no test covers yet, accepted one by one (mutation_coverage_gaps.json), which the
write-observer census (scripts/mutation_census.py) holds the run to. Standard library only, so
the census job reads it without the app installed."""

from __future__ import annotations

import json
import os
import subprocess

# A gap is a mutating route whose source reaches a write that no test made write durably, keyed by app, method
# and path, and the write sites of one function and call no observed request write passed
# through, keyed by file, function and call with their count. The census fails a gap no
# entry names and an entry that no longer matches its gap.
GAPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutation_coverage_gaps.json")
ROUTE_CLASSES = ("never requested", "requested, no durable write")
SITE_CLASSES = ("not reached from a route", "reached from a route")  # by the static call graph
# The checked-in entries at most: closing a gap lowers it in the same change; only a write site no
# route reaches may be added below it.
GAPS_CEILING = 300
# The branch whose list of gaps a change may only shrink: an entry it does not hold, or a site
# entry counting more sites than its copy there, is a newly untested writer, even when the
# change drops another entry to keep the count. The exception is a site no route reaches.
GAPS_BASE = "origin/main"


def gap_key(entry: dict) -> tuple[str, str]:
    return ("route", entry["route"]) if "route" in entry else ("site", entry["site"])


def load_gaps(path: str = GAPS_PATH) -> dict[tuple[str, str], dict]:
    with open(path, encoding="utf-8") as fh:
        return _gaps(json.load(fh))


def gaps_at(ref: str, path: str = GAPS_PATH) -> dict[tuple[str, str], dict] | None:
    """The coverage gaps checked in at git *ref*, or None when the file is not there yet (the
    change that adds it). A ref git cannot resolve raises CalledProcessError."""
    root, name = os.path.split(os.path.abspath(path))
    git = ["git", "-C", root]
    listed = subprocess.run([*git, "ls-tree", "--name-only", ref, "--", name],
                            capture_output=True, text=True, check=True).stdout
    if not listed.strip():
        return None
    return _gaps(json.loads(subprocess.run([*git, "show", f"{ref}:./{name}"],
                                           capture_output=True, text=True, check=True).stdout))


def _gaps(entries: list[dict]) -> dict[tuple[str, str], dict]:
    gaps: dict[tuple[str, str], dict] = {}
    for entry in entries:
        if ("route" in entry) == ("site" in entry):
            raise ValueError(f"coverage gap {entry} names no single route or site")
        name = entry.get("route") or entry.get("site")
        missing = [field for field in ("class", "reason") + (() if "route" in entry else ("count",))
                   if not entry.get(field)]
        if missing:
            raise ValueError(f"coverage gap {name} lacks {', '.join(missing)}")
        if entry["class"] not in (ROUTE_CLASSES if "route" in entry else SITE_CLASSES):
            raise ValueError(f"coverage gap {name} has an unknown class {entry['class']!r}")
        if gap_key(entry) in gaps:
            raise ValueError(f"two coverage gap entries for {name}")
        gaps[gap_key(entry)] = entry
    return gaps
