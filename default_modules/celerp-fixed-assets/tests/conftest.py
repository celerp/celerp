# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fixtures for the fixed assets module: a demo company with its asset bills, started."""
from __future__ import annotations

import pytest_asyncio

from fa_fixtures import FA, start_demo


@pytest_asyncio.fixture
async def fa(client, session) -> FA:
    return await start_demo(client, session)
