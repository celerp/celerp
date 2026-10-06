# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every page that belongs to a module is registered by that module.

A module's settings tab and its import, reconciliation and dashboard pages are
served by the module's own UI routes, so a company that turned the module off is
refused them like any other page of that module, and an installation that does
not run the module does not serve them at all.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from test_helpers import REPO_ROOT

_OWNED = {
    "/settings/contacts": "celerp-contacts",
    "/settings/contacts/tags": "celerp-contacts",
    "/settings/accounting": "celerp-accounting",
    "/settings/accounting/chart/new": "celerp-accounting",
    "/accounting/import/chart": "celerp-accounting",
    "/accounting/reconcile/start": "celerp-accounting",
    "/settings/inventory": "celerp-inventory",
    "/settings/sales": "celerp-docs",
    "/settings/purchasing": "celerp-docs",
    "/docs/import": "celerp-docs",
    "/lists/import": "celerp-docs",
    "/docs/received": "celerp-docs",
    "/dashboard": "celerp-dashboard",
}

_SCRIPT = textwrap.dedent("""
    import json, os, sys
    import celerp.modules.outcome as outcome
    running = sorted(os.environ["ENABLED_MODULES"].split(","))
    outcome.reported_by_api = lambda *a, **k: {"running": running}
    import ui.app as uiapp
    from celerp.modules import loader
    paths = json.loads(sys.argv[1])
    out = {}
    for p in paths:
        method = "POST" if p.endswith(("/tags", "/start")) else "GET"
        scope = {"type": "http", "method": method, "path": p, "root_path": "",
                 "query_string": b"", "headers": []}
        served = any(r.matches(scope)[0].name == "FULL" for r in uiapp.app.router.routes)
        out[p] = {"module": loader.route_module(scope), "served": served}
    print("RESULT" + json.dumps(out))
""")


def _routes(enabled: set[str]) -> dict:
    env = {**os.environ, "ENABLED_MODULES": ",".join(sorted(enabled)),
           "MODULE_DIR": str(REPO_ROOT / "default_modules")}
    proc = subprocess.run([sys.executable, "-c", _SCRIPT, json.dumps(list(_OWNED))],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-4000:]
    return json.loads(line[len("RESULT"):])


def _default_modules() -> set[str]:
    return {p.name for p in (REPO_ROOT / "default_modules").iterdir()
            if (p / "__init__.py").is_file() and p.name.startswith("celerp-")}


def test_each_module_page_is_served_as_part_of_its_module():
    got = _routes(_default_modules())
    wrong = {p: got[p] for p, owner in _OWNED.items()
             if got[p] != {"module": owner, "served": True}}
    assert not wrong, wrong


@pytest.mark.parametrize("off", sorted(set(_OWNED.values())))
def test_a_module_the_installation_does_not_run_serves_none_of_its_pages(off):
    got = _routes(_default_modules() - {off})
    served = [p for p, owner in _OWNED.items() if owner == off and got[p]["served"]]
    assert not served, served
