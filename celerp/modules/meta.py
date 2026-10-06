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


def write_meta(pkg_dir: Path, *, source: str) -> None:
    """Write the provenance sidecar into an installed module folder.

    ``installed_at`` is always a UTC ISO 8601 string so the ordering code can
    compare timestamps without type juggling.
    """
    installed_at = datetime.now(timezone.utc).isoformat()
    payload = {"source": source, "installed_at": installed_at}
    (Path(pkg_dir) / META_FILENAME).write_text(json.dumps(payload))


def read_meta(pkg_dir: Path) -> dict:
    """Return the sidecar contents, or ``{}`` if it is missing or unreadable."""
    path = Path(pkg_dir) / META_FILENAME
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
