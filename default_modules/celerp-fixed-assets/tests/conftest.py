# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fixtures for the fixed assets module: a demo company with its asset bills, started."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from fa_fixtures import FA, start_demo


@pytest.fixture(autouse=True)
def _module_catalogs():
    """The test app wires this module's routes without loading it, so its translation
    catalogs and slot contributions are registered here, as the module loader does,
    before each test (other tests clear both registries), and its slots removed after."""
    from celerp.modules import slots
    from celerp.modules.loader import _register_locales, _runtime_keys, read_manifest

    pkg = Path(__file__).resolve().parent.parent
    manifest = read_manifest(pkg)
    _register_locales(pkg.name, pkg, manifest)
    slots.unregister_module(pkg.name)
    for slot, contribution in manifest["slots"].items():
        for item in contribution if isinstance(contribution, list) else [contribution]:
            slots.register(slot, {**item, **_runtime_keys(pkg.name, True)})
    yield
    slots.unregister_module(pkg.name)


ZZ = Path(__file__).resolve().parent / "fixtures" / "celerp_l10n_zz"


@pytest.fixture
def zz(monkeypatch):
    """The test-only country module ZZ, admitted, registered and listed as loaded as the
    module loader does for a module that is not Celerp's own."""
    import sys

    from celerp.modules import loader, slots
    from celerp.modules.loader import _check_slot_contracts, _register_locales, _runtime_keys, read_manifest

    if str(ZZ.parent) not in sys.path:
        sys.path.insert(0, str(ZZ.parent))
    manifest = read_manifest(ZZ)
    _check_slot_contracts(ZZ, manifest["slots"], first_party=False)
    _register_locales(ZZ.name, ZZ, manifest)
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, manifest])
    for item in manifest["slots"]["fixed_asset_rules"]:
        slots.register("fixed_asset_rules", {**item, **_runtime_keys(ZZ.name, False)})
    yield ZZ
    slots.unregister_module(ZZ.name)


@pytest_asyncio.fixture
async def fa(client, session) -> FA:
    return await start_demo(client, session)
