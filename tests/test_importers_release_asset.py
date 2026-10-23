# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The importer list each release carries names every registered source, read from
the registry, so a source added to the registry reaches the list without a second
copy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from celerp import __version__
from celerp.importers.adapters.registry import list_adapters

_SCRIPT = Path(__file__).parent.parent / "scripts" / "export_importers.py"


def test_release_importer_list_names_every_registered_source(tmp_path):
    out = tmp_path / "importers.json"
    subprocess.run([sys.executable, str(_SCRIPT), "--out", str(out)], check=True, capture_output=True)
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data["version"] == __version__
    expected = [
        {
            "key": adapter.key,
            "display_name": adapter.display_name,
            "formats": sorted({ext for spec in adapter.artifact_specs for ext in spec.extensions}),
        }
        for adapter in list_adapters()
    ]
    assert data["importers"] == expected
    assert [entry["key"] for entry in data["importers"]] == [adapter.key for adapter in list_adapters()]
    assert len({entry["key"] for entry in data["importers"]}) == len(expected) > 0
    for entry in data["importers"]:
        assert entry["display_name"].strip()
        assert entry["formats"] and all(fmt.startswith(".") for fmt in entry["formats"])
