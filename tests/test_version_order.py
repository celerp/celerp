# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The updater's release order (electron/update-state.js compareVersions) has one meaning,
packaging.version's, which decides whether a release can open the data. Both read the same
pairs: this test proves the pairs are packaging's order, the Electron test proves the updater
follows them."""

import json
import re
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


_UPDATER = (Path(__file__).parent.parent / "electron" / "update-state.js").read_text()


def _spellings() -> list[str]:
    """Every pre-release spelling the updater ranks (PRE_RANK) and every post spelling its parser reads."""
    pre = re.findall(r"(\w+):", re.search(r"const PRE_RANK = \{([^}]*)\}", _UPDATER).group(1))
    post = re.search(r"\|\[-_\.\]\?\(([a-z|]+)\)", _UPDATER).group(1).split("|")
    assert pre and post == ["post", "rev", "r"]
    return pre + post


@pytest.mark.parametrize("spelling", _spellings())
def test_every_pre_rank_spelling_has_a_fixture_pair(spelling: str) -> None:
    # The spelling written right after a release number, not inside a longer word ("pre" in "preview").
    token = re.compile(rf"\d[-_.]?{spelling}(?![a-z])", re.IGNORECASE)
    assert any(token.search(v) for a, b, _ in PAIRS for v in (a, b))
