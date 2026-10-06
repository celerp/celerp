# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Per-module provenance sidecar (``.celerp-meta.json``).

The importer writes one of these into every module folder at install time. It
records where the module came from and when it landed. The modules page orders
by its install time and tells a community import from a plain sideload by its
source; a Marketplace install is known from the importer's own record instead.

The sidecar is advisory: a folder without one (a pre-existing import, or a
default module re-seeded by the desktop app) is simply treated as unknown
provenance. Reads never raise - a missing or corrupt file degrades to ``{}``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

META_FILENAME = ".celerp-meta.json"

# The sources an uploaded package may record. A Marketplace install is known from
# the importer's own record (importer.MARKETPLACE_RECORD), never from a sidecar,
# and a default module by its content (see the loader).
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
