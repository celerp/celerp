# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Write the committed synthetic Manager files: the test fixtures and Celerp's shipped sample.

Run from the repository root: python -m tests.fixtures.manager_io.build
"""

from __future__ import annotations

from pathlib import Path

from celerp.importers.sample import SAMPLE_ARTIFACT, SAMPLE_COMPANY_NAME
from . import specs

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASIC = HERE / "basic.manager"
FX = HERE / "fx.manager"


def build_all() -> list[Path]:
    return [
        specs.build_basic(BASIC),
        specs.build_fx(FX),
        specs.build_basic(SAMPLE_ARTIFACT, company=SAMPLE_COMPANY_NAME),
    ]


if __name__ == "__main__":
    for path in build_all():
        print(path.relative_to(ROOT))
