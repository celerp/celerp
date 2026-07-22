# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Standard-library shims for the supported interpreter floor.

pyproject sets ``requires-python >= 3.10``, but a handful of names the codebase
relies on landed in 3.11. Import them from here so the floor is held in one
place rather than re-implemented at each use site.
"""
from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # Python 3.10: enum.StrEnum arrived in 3.11.
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of enum.StrEnum for 3.10: members are real ``str``, and
        ``str(member)`` yields the member value (not ``'Class.MEMBER'``), which
        is the behaviour 3.11+ code depends on."""

        __str__ = str.__str__


__all__ = ["StrEnum"]
