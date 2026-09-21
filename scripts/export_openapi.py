# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Export the served OpenAPI schema to a file, without booting a server.

The release workflow attaches the output as openapi.json to each release, and the
website renders a static API reference from it. This is byte-for-byte the schema
/openapi.json serves when expose_openapi_schema is on: the description and tag
metadata are always baked into the app (celerp.main._openapi_settings), only the
served route is gated, and app.openapi() builds the document without a running
server.

    python scripts/export_openapi.py               # write openapi.json in place
    python scripts/export_openapi.py --out path     # write elsewhere
"""
from __future__ import annotations

import argparse
import json
import os

# The JWT guard in celerp.config raises at import under production settings; the
# schema export never signs a token, so allow the insecure default like the
# press-kit capture harness and the openapi tests do.
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import celerp.main


def export(path: str) -> dict:
    """Write the exposed OpenAPI schema to ``path`` and return it."""
    # Turn the flag on so the export matches the exposed schema exactly and the
    # non-default exposure is logged, the same as when a server serves it.
    celerp.main.settings.expose_openapi_schema = True
    schema = celerp.main.app.openapi()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return schema


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="openapi.json", help="output path")
    args = parser.parse_args()
    schema = export(args.out)
    ops = sum(
        1
        for methods in schema.get("paths", {}).values()
        for method in methods
        if method in ("get", "post", "put", "patch", "delete")
    )
    print(f"Wrote {args.out}: OpenAPI {schema.get('openapi')}, {ops} operations")
