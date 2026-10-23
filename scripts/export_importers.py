# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Export the list of sources Celerp imports from, read from the importer registry.

The release workflow attaches the output as importers.json to each release, beside
openapi.json, so the website lists the sources a release imports from without
keeping its own copy of that list.

    python scripts/export_importers.py               # write importers.json in place
    python scripts/export_importers.py --out path     # write elsewhere
"""
from __future__ import annotations

import argparse
import json

from celerp import __version__
from celerp.importers.adapters.registry import list_adapters


def importers() -> dict:
    """Every registered source in registry order: its key, display name and the file
    formats it accepts."""
    return {
        "version": __version__,
        "importers": [
            {
                "key": adapter.key,
                "display_name": adapter.display_name,
                "formats": sorted({ext for spec in adapter.artifact_specs for ext in spec.extensions}),
            }
            for adapter in list_adapters()
        ],
    }


def export(path: str) -> dict:
    """Write the list to ``path`` and return it."""
    data = importers()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="importers.json", help="output path")
    args = parser.parse_args()
    data = export(args.out)
    print(f"Wrote {args.out}: {len(data['importers'])} importers")
