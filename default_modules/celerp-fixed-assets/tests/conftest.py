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
    catalogs are registered here, as the module loader does, before each test (other
    tests clear the catalog registry)."""
    from celerp.modules.loader import _register_locales, read_manifest

    pkg = Path(__file__).resolve().parent.parent
    _register_locales(pkg.name, pkg, read_manifest(pkg))


@pytest_asyncio.fixture
async def fa(client, session) -> FA:
    return await start_demo(client, session)
