# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Per-module provenance sidecar (``.celerp-meta.json``).

The importer writes one of these into every module folder at install time. It
records where the module came from and when it landed, and it is the single
source for two user-facing features on the modules page: the source label and
shield next to each module name, and the newest-imported-first ordering. Both are
display only; nothing reads the sidecar to decide what a module may do.

The sidecar is advisory: a folder without one (a pre-existing import, or a
default module re-seeded by the desktop app) is simply treated as unknown
provenance. Reads never raise - a missing or corrupt file degrades to ``{}``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

META_FILENAME = ".celerp-meta.json"

# The sources an uploaded package may record. "marketplace" is written only by
# the Marketplace install, and a default module is known by its content (see the
# loader), never by a sidecar.
IMPORT_SOURCES = {"community", "sideloaded"}

# Every source the module list reports; a sidecar without one reads as "sideloaded".
SOURCES = IMPORT_SOURCES | {"marketplace"}


def write_meta(pkg_dir: Path, *, source: str) -> None:
    """Write the provenance sidecar into an installed module folder.

    ``installed_at`` is always a UTC ISO 8601 string so the ordering code can
    compare timestamps without type juggling.
    """
    installed_at = datetime.now(timezone.utc).isoformat()
    payload = {"source": source, "installed_at": installed_at}
    (Path(pkg_dir) / META_FILENAME).write_text(json.dumps(payload))


def read_meta(pkg_dir: Path) -> dict:
    """Return the sidecar's fields that hold a value of the right type: ``source``
    when it is one of :data:`SOURCES`, ``installed_at`` when it is a string. Any
    other field or value is left out, as is the whole file when it is missing,
    unreadable or not a JSON object."""
    path = Path(pkg_dir) / META_FILENAME
    try:
        meta = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    fields = {}
    if isinstance(meta.get("source"), str) and meta["source"] in SOURCES:
        fields["source"] = meta["source"]
    if isinstance(meta.get("installed_at"), str):
        fields["installed_at"] = meta["installed_at"]
    return fields
