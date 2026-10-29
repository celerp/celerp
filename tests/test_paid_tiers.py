# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The paid tier list has one source: celerp.gateway.state.PAID_TIERS."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LITERAL = re.compile(r"""[(\[{]\s*(['"])cloud\1\s*,\s*(['"])ai\2\s*,\s*(['"])team\3\s*[)\]}]""")


def test_paid_tiers_single_source():
    offenders = []
    for path in [*_ROOT.glob("celerp/**/*.py"), *_ROOT.glob("ui/**/*.py"),
                 *_ROOT.glob("default_modules/*/*/**/*.py")]:
        if path == _ROOT / "celerp/gateway/state.py" or "/tests/" in path.as_posix():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _LITERAL.search(line):
                offenders.append(f"{path.relative_to(_ROOT)}:{n}")
    assert offenders == []
