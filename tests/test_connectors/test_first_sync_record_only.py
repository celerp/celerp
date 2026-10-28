# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The first-sync record is the only proof a first sync ran: no code outside the
migrations reads or writes the old config columns."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_OLD = re.compile(r"\bfirst_sync_at\b|\bfirst_sync_undo\b(?!_links)")


def test_no_code_outside_the_migrations_names_the_old_first_sync_columns():
    hits = []
    paths = [p for d in ("celerp", "ui", "default_modules", "tests") for p in (_ROOT / d).rglob("*.py")]
    for path in paths:
        rel = path.relative_to(_ROOT)
        if "migrations" in rel.parts:
            continue
        if path == Path(__file__).resolve():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if _OLD.search(line):
                hits.append(f"{rel}:{n}")
    assert hits == []
