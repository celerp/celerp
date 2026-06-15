# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("celerp")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"
