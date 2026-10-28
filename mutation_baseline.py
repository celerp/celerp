# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The findings of the write observer (mutation_observer.py) that predate it, accepted one
by one until their tracking issue is resolved (mutation_baseline.json).

An entry matches a finding exactly: its route, kind, table, statement shape (fingerprint)
and the app frames that made it, as functions without line numbers. A finding matching no
entry fails the test that made it; an entry no finding matched, or that its own
reproducing test did not see, fails the census (scripts/mutation_census.py --enforce).
Standard library only, so the census job reads it without the app installed."""

from __future__ import annotations

import json
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutation_baseline.json")
FIELDS = ("route", "kind", "table", "fingerprint", "where", "test", "issue", "owner", "remove_when")


def _unlined(frame: str) -> str:
    location, _, function = frame.partition(" ")
    path, colon, line = location.rpartition(":")
    return f"{path if colon and line.isdigit() else location} {function}"


def key(finding) -> tuple:
    """What an entry and a finding (a Finding or its census record) must share."""
    get = finding.get if isinstance(finding, dict) else lambda name: getattr(finding, name)
    return (get("kind"), get("route"), get("table"), get("fingerprint"),
            tuple(_unlined(frame) for frame in get("where")))


def load(path: str = PATH) -> dict[tuple, dict]:
    entries: dict[tuple, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for entry in json.load(fh):
            missing = [name for name in FIELDS if not entry.get(name)]
            if missing:
                raise ValueError(f"baseline entry for {entry.get('route')} lacks {', '.join(missing)}")
            if key(entry) in entries:
                raise ValueError(f"two baseline entries for {entry['route']} {entry['table']}")
            entries[key(entry)] = entry
    return entries


ENTRIES = load()


def accepted(finding) -> dict | None:
    """The entry a finding matches, or None."""
    return ENTRIES.get(key(finding))
