# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""AI module test fixtures.

Autouse fixture ensures quota check never hits the real relay,
regardless of settings state left by other tests.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, patch

import pytest

# Guarantee celerp-ai src is on sys.path before any test imports.
_ai_src = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "default_modules", "celerp-ai")
)
if _ai_src not in sys.path:
    sys.path.insert(0, _ai_src)


@pytest.fixture(autouse=True)
def _no_ai_quota():
    """Prevent quota checks from calling the relay in unit tests.

    Patches the name as used in celerp_ai.routes (import site), since
    the route handler looks up 'check_ai_quota' in its own module globals.
    """
    import celerp_ai.routes  # ensure loaded before patching
    with patch("celerp_ai.routes.check_ai_quota", new=AsyncMock(return_value=None)):
        yield
