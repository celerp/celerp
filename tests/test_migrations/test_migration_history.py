# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The migration history is one line: every revision id belongs to one file, and
upgrading has exactly one head to reach. Two branches that each add a revision
with the same id merge without a textual conflict, and alembic then keeps one
file and silently skips the other."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "celerp" / "migrations" / "versions"


def _revision_ids() -> dict[str, str]:
    ids = {}
    for path in sorted(VERSIONS.glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            target = node.target if isinstance(node, ast.AnnAssign) else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id == "revision":
                ids[path.name] = ast.literal_eval(node.value)
    return ids


def test_every_revision_id_belongs_to_one_file():
    ids = _revision_ids()
    repeated = {rev for rev, n in Counter(ids.values()).items() if n > 1}
    assert not repeated, {f: rev for f, rev in ids.items() if rev in repeated}


def test_history_has_one_head():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert len(script.get_heads()) == 1, script.get_heads()
    assert len(list(script.walk_revisions())) == len(_revision_ids())
