# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fixtures for the fixed assets module: a demo company with its asset bills, started."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from fa_fixtures import FA, admit_zz, start_demo


@pytest.fixture(autouse=True)
def _module_slots():
    """The test app wires this module's routes without loading it, so its slot contributions
    are registered here, as the module loader does, before each test (other tests clear the
    registry), and removed after. Its catalogs are registered by the root conftest."""
    from celerp.modules import slots
    from celerp.modules.loader import _runtime_keys, read_manifest

    pkg = Path(__file__).resolve().parent.parent
    manifest = read_manifest(pkg)
    slots.unregister_module(pkg.name)
    for slot, contribution in manifest["slots"].items():
        for item in contribution if isinstance(contribution, list) else [contribution]:
            slots.register(slot, {**item, **_runtime_keys(pkg.name, True)})
    yield
    slots.unregister_module(pkg.name)


@pytest.fixture
def core_sample_records_only():
    """These tests run on a demo company carrying the asset bills this module seeds."""


@pytest.fixture
def zz(monkeypatch):
    yield from admit_zz(monkeypatch)


@pytest_asyncio.fixture
async def fa(client, session) -> FA:
    return await start_demo(client, session)
