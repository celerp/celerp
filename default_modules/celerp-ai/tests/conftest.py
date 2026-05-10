# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""AI module test fixtures.

Ensures celerp_ai.routes is loaded via the same sys.modules entry used by the app,
so unittest.mock.patch targets are consistent regardless of test ordering.
"""
from __future__ import annotations

import sys
import os

# Guarantee celerp-ai src is on the path (root conftest may have done this already,
# but be explicit to avoid ordering issues).
_ai_src = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "default_modules", "celerp-ai")
)
if _ai_src not in sys.path:
    sys.path.insert(0, _ai_src)

# Force-load celerp_ai.routes now so sys.modules has it before any test patches.
import celerp_ai.routes  # noqa: F401, E402
