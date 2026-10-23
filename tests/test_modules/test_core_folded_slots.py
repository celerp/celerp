# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The modules folded into core (CORE_FOLDED) are never loaded by the module loader,
so their manifest slots are registered at app construction. Checked in a fresh
process, outside the test harness (whose own slot setup would hide a gap), for the API
app and the UI app each, as production starts them."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_PROBE = """
import json, sys
import {app}
from celerp.modules.slots import get
out = {{slot: [{{k: v for k, v in c.items() if k in ("prefix", "id", "label", "_module", "form_action")}}
               for c in get(slot)] for slot in ("projection_handler", "catalog_channel", "bulk_action",
                                                 "item_action", "nav")}}
sys.stdout.write("SLOTS" + json.dumps(out))
"""


def _slots(app: str) -> dict:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", _PROBE.format(app=app)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.rsplit("SLOTS", 1)[1])


def _from(slots: dict, slot: str, module: str) -> list[dict]:
    return [c for c in slots[slot] if c.get("_module") == module]


@pytest.mark.timeout(300)
def test_production_app_registers_connector_slots():
    api = _slots("celerp.main")
    prefixes = [c["prefix"] for c in _from(api, "projection_handler", "celerp-connectors")]
    assert sorted(prefixes) == ["mp.", "shop.sync."]

    ui = _slots("ui.app")
    assert [c["id"] for c in _from(ui, "catalog_channel", "celerp-connectors")] == ["woocommerce", "shopify"]
    bulk = _from(ui, "bulk_action", "celerp-connectors")
    assert bulk and len(bulk) == len({c["form_action"] for c in bulk})
    assert _from(ui, "item_action", "celerp-connectors")
    assert [c["label"] for c in _from(ui, "nav", "celerp-ai")]
