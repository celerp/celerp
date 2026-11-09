# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The updater's release order (electron/update-state.js compareVersions) has one meaning,
packaging.version's, which decides whether a release can open the data. Both read the same
pairs: this test proves the pairs are packaging's order, the Electron test proves the updater
follows them."""

import json
from pathlib import Path

import pytest
from packaging.version import InvalidVersion, Version

PAIRS = json.loads((Path(__file__).parent / "fixtures" / "version_order.json").read_text())["pairs"]


def _sign(a: str, b: str) -> int | None:
    try:
        x, y = Version(a), Version(b)
    except InvalidVersion:
        return None
    return (x > y) - (x < y)


@pytest.mark.parametrize(("a", "b", "sign"), PAIRS, ids=[f"{a}|{b}" for a, b, _ in PAIRS])
def test_version_order_pairs_are_packaging_order(a: str, b: str, sign: int | None) -> None:
    assert _sign(a, b) == sign
