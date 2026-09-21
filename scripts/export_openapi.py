# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Export the served OpenAPI schema to a file, without booting a server.

The release workflow attaches the output as openapi.json to each release, and the
website renders a static API reference from it. This is byte-for-byte the schema
/openapi.json serves when exposure is enabled: the exposure setting controls only
whether FastAPI registers that route, while the schema metadata is always baked
into the app (celerp.main._openapi_settings). app.openapi() builds the document
without a running server.

    python scripts/export_openapi.py               # write openapi.json in place
    python scripts/export_openapi.py --out path     # write elsewhere
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# The JWT guard in celerp.config raises at import under production settings; the
# schema export never signs a token, so allow the insecure default like the
# press-kit capture harness and the openapi tests do.
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import celerp.main


def _register_default_modules() -> None:
    """Attach the first-party module routers, the same ones a fully provisioned
    instance serves.

    The core app registers only its own routes at import; the business modules
    (inventory, docs, accounting, contacts, manufacturing, and the rest) attach
    their routers during startup, keyed off the enabled-module set. app.openapi()
    reads whatever is registered, so without this the exported schema would omit
    most of the API. Route registration imports the modules and mounts their
    routers; it touches no database (the startup migration phase is a
    Postgres-only step that does not shape the schema)."""
    module_dir = os.environ.get("MODULE_DIR") or str(
        Path(celerp.main.__file__).resolve().parent.parent / "default_modules"
    )
    lock = Path(module_dir) / "first_party.lock.json"
    enabled = set(json.loads(lock.read_text(encoding="utf-8")).keys())

    from celerp.modules.loader import load_all, register_api_routes

    loaded = load_all(module_dir, enabled)
    register_api_routes(celerp.main.app, loaded)


def export(path: str) -> dict:
    """Write the OpenAPI schema to ``path`` and return it."""
    _register_default_modules()
    # openapi() caches on the app; clear it so a re-export after registration
    # rebuilds the document against the now-complete route set.
    celerp.main.app.openapi_schema = None
    schema = celerp.main.app.openapi()
    internal = sorted(
        route for route in schema.get("paths", {}) if route.startswith("/__celerp/")
    )
    if internal:
        raise RuntimeError(
            "OpenAPI export contains internal routes: " + ", ".join(internal)
        )
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
