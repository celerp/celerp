# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Inspect the structure of a Manager business file without reading business content.

    python scripts/diagnostics/manager_probe.py path/to/file.manager

prints the SQLite tables and columns, the Manager schema version and the
object count per content type with its classification. It never prints object
payloads, attachment content, names or amounts.

`check_manager_content_types_all_classified` is the mechanical inventory check
over the committed synthetic files: every content type found in them is
classified, no type is left unclassified, and every type Celerp maps (fully or
with loss) is present in a synthetic fixture and has a hand-worked source
checkpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader  # noqa: E402
from celerp.importers.adapters.manager_io.types import CONTENT_TYPES, GUIDS, TYPE_NAMES  # noqa: E402
from celerp.importers.schema import CoverageClass  # noqa: E402
from celerp.importers.sample import SAMPLE_ARTIFACT  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "manager_io"
CHECKPOINTS = FIXTURES / "checkpoints.json"
MAPPED = (CoverageClass.MAPPED, CoverageClass.MAPPED_WITH_LOSS)


def synthetic_files() -> list[Path]:
    return [*sorted(FIXTURES.glob("*.manager")), SAMPLE_ARTIFACT]


def _resolves(checkpoints: dict, dotted: str) -> bool:
    node: object = checkpoints
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def check_manager_content_types_all_classified() -> list[str]:
    """Problems found in the Manager type inventory; an empty list means the check passes."""
    problems: list[str] = []
    for guid, coverage in CONTENT_TYPES.items():
        if coverage == CoverageClass.UNCLASSIFIED:
            problems.append(f"{TYPE_NAMES[guid]} is classified unclassified.")
    present: set[str] = set()
    for path in synthetic_files():
        with ManagerReader(path) as reader:
            found = set(reader.counts_by_type())
        present |= found
        for guid in sorted(found - set(CONTENT_TYPES)):
            problems.append(f"{path.name}: content type {guid} has no classification.")
    checkpoints = json.loads(CHECKPOINTS.read_text())
    type_checkpoints: dict[str, list[str]] = checkpoints.get("type_checkpoints", {})
    for guid, coverage in CONTENT_TYPES.items():
        if coverage not in MAPPED:
            continue
        name = TYPE_NAMES[guid]
        if guid not in present:
            problems.append(f"{name} is {coverage.value} but no synthetic fixture contains it.")
        paths = type_checkpoints.get(name, [])
        if not paths:
            problems.append(f"{name} is {coverage.value} but has no source checkpoint.")
        problems.extend(f"{name}: checkpoint {p} does not exist." for p in paths if not _resolves(checkpoints, p))
    problems.extend(f"Checkpoint names unknown type {name}." for name in type_checkpoints if name not in GUIDS)
    return problems


def probe(path: Path) -> None:
    with ManagerReader(path) as reader:
        print(f"schema version: {reader.schema_version}")
        tables = reader.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        for (table,) in tables:
            columns = [row[1] for row in reader.conn.execute("SELECT * FROM pragma_table_info(?)", (table,))]
            print(f"table {table}: {', '.join(columns)}")
        for guid, count in sorted(reader.counts_by_type().items(), key=lambda item: TYPE_NAMES.get(item[0], item[0])):
            coverage = CONTENT_TYPES.get(guid, CoverageClass.UNCLASSIFIED)
            print(f"{count:>7}  {TYPE_NAMES.get(guid, guid)}  [{coverage.value}]")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: manager_probe.py FILE")
    probe(Path(sys.argv[1]))
