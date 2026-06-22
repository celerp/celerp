# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""AI module test fixtures."""
from __future__ import annotations

import sys
import os

# Guarantee celerp-ai src is on sys.path before any test imports.
_ai_src = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "default_modules", "celerp-ai")
)
if _ai_src not in sys.path:
    sys.path.insert(0, _ai_src)
