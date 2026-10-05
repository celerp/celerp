# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The UI process starts with every default module turned on, and each page is
registered exactly once: a module's pages come from the module, never also from
a second copy wired by the UI app itself."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from test_helpers import REPO_ROOT

# Boots ui.app as the desktop does (MODULE_DIR plus the enabled list), with the
# API's module record stubbed to "everything enabled is running" so no API
# process is needed.
_BOOT = textwrap.dedent("""
    import json, sys
    import celerp.modules.outcome as outcome
    from celerp.modules.loader import admit_modules
    outcome.reported_by_api = lambda api_url, database_url: {
        "running": sorted(m.name for m in admit_modules(sys.argv[1], set(sys.argv[2].split(","))).admitted)}
    try:
        import ui.app
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:600]})); raise SystemExit(0)
    from celerp.modules.loader import _route_keys
    seen, dupes = set(), set()
    for r in ui.app.app.router.routes:
        for key in _route_keys(r):
            (dupes if key in seen else seen).add(key)
    print(json.dumps({"dupes": sorted(f"{m} {p}" for p, m in dupes)}))
""")


def _default_module_names() -> list[str]:
    from celerp.modules.loader import is_core_folded
    base = REPO_ROOT / "default_modules"
    return sorted(p.name for p in base.iterdir()
                  if (p / "__init__.py").exists() and not is_core_folded(p.name))


def test_ui_starts_with_every_default_module_and_registers_each_page_once(tmp_path):
    module_dir = str(REPO_ROOT / "default_modules")
    enabled = ",".join(_default_module_names())
    env = {**os.environ, "MODULE_DIR": module_dir, "ENABLED_MODULES": enabled,
           "CELERP_DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.run([sys.executable, "-c", _BOOT, module_dir, enabled],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in out, out["error"]
    assert out["dupes"] == []


# Boots ui.app as the documented dev run does: no MODULE_DIR, no ENABLED_MODULES,
# the enabled list read from config.toml. The API's record is stubbed to "every
# module given is running".
_DEV_BOOT = textwrap.dedent("""
    import json, sys
    import celerp.modules.outcome as outcome
    outcome.reported_by_api = lambda api_url, database_url: {"running": sys.argv[1].split(",")}
    import ui.app
    print(json.dumps(sorted({getattr(r, "path", "") for r in ui.app.app.router.routes})))
""")


def test_dev_run_without_module_dir_loads_the_default_modules(tmp_path):
    """`uvicorn ui.app:app` with MODULE_DIR unset searches the module trees
    `celerp start` gives it, so the document and label pages are there."""
    enabled = _default_module_names()
    config = tmp_path / "config.toml"
    config.write_text("[modules]\nenabled = [" + ", ".join(f'"{m}"' for m in enabled) + "]\n")
    env = {k: v for k, v in os.environ.items() if k not in ("MODULE_DIR", "ENABLED_MODULES")}
    env.update(CELERP_CONFIG=str(config), CELERP_DATA_DIR=str(tmp_path / "data"))
    proc = subprocess.run([sys.executable, "-c", _DEV_BOOT, ",".join(enabled)],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    paths = set(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert "/docs" in paths
    assert "/settings/labels" in paths
