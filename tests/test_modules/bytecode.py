# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Plant executable bytecode that Python would run in place of a module's source."""
from __future__ import annotations

import importlib.util
import py_compile
import sys
from pathlib import Path


def plant_bytecode(source: Path, code: str, *, pycache_prefix: str | None = None) -> Path:
    """Write valid bytecode for *source* that runs *code* instead of the source, and
    return its path. It is unchecked, so Python never compares it to the source. The
    path is where Python looks for it with sys.pycache_prefix set to *pycache_prefix*
    (beside the source when None)."""
    previous, sys.pycache_prefix = sys.pycache_prefix, pycache_prefix
    try:
        cache = Path(importlib.util.cache_from_source(str(source)))
    finally:
        sys.pycache_prefix = previous
    cache.parent.mkdir(parents=True, exist_ok=True)
    stand_in = cache.parent / f"stand_in_{cache.stem.split('.')[0]}.py"
    stand_in.write_text(code)
    try:
        py_compile.compile(str(stand_in), cfile=str(cache), dfile=str(source), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    finally:
        stand_in.unlink()
    return cache
