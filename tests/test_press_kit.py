# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Guards for the committed press-kit output the website, README, and external
listings read directly.

These assert the published image set and its consumer references as a contract,
independent of the capture pipeline that produces them (that pipeline lives in a
separate private repo and never runs in this suite). If a regen drops, renames,
mis-formats, or over-sizes a file, or fails to reference a promoted new shot, the
website breaks; these tests fail first.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

_PRESS_KIT = Path(__file__).parent.parent / "docs" / "press-kit"
_SHOTS = _PRESS_KIT / "screenshots"

# The canonical top-level PNGs the root README grid and website hero surfaces read.
_BASE_CANONICAL = {
    "balance-sheet",
    "dashboard",
    "general-ledger",
    "inventory",
    "manufacturing-worksheet",
    "production-planning",
    "rest-api",
    "trial-balance",
}
# New marquee shots promoted to the canonical PNG set (also in each owning vertical).
_PROMOTED = {
    "international-shipping-document",
    "statement-of-account",
    "extended-journal",
    "work-centers",
}
_CANONICAL = _BASE_CANONICAL | _PROMOTED

# The screen set every vertical folder has carried.
_BASE_SCREENS = {
    "audit-log",
    "balance-sheet",
    "chart-of-accounts",
    "dashboard",
    "general-ledger",
    "inventory",
    "invoices",
    "manufacturing-worksheet",
    "permissions",
    "production-planning",
    "rest-api",
    "trial-balance",
    "worksheet-print",
}
# New shots each vertical gains, added to its own gallery.
_NEW_BY_VERTICAL = {
    "apparel": {"work-centers", "invoice-pay", "label-designer", "modules"},
    "coffee": {"international-shipping-document", "extended-journal"},
    "cosmetics": {"connectors"},
    "jewelry": {"statement-of-account", "memo-holdings"},
}
_VERTICALS = sorted(_NEW_BY_VERTICAL)

# Width caps documented for each tier.
_CANONICAL_MAX_W = 1600
_GALLERY_MAX_W = 1280

_EM_DASH = "\u2014"  # em dash, by codepoint so the source stays ASCII
# The attribution name belongs only in the hand-written NOTICE/copyright blocks,
# never in a machine-generated caption or table row. Read it from the copyright
# line rather than restating it, so there is one source for who is attributed.
_ATTRIBUTION_RE = re.compile(r"\(c\) 20\d\d ([A-Z][a-z]+(?: [A-Z][a-z]+)+)")


def _vertical_expected(vertical: str) -> set[str]:
    return _BASE_SCREENS | _NEW_BY_VERTICAL[vertical]


def _stems(directory: Path, suffix: str) -> set[str]:
    return {p.stem for p in directory.glob(f"*{suffix}")}


def _between(text: str, region: str) -> str:
    """Return the text between the PRESS_KIT sentinel anchors for *region*, or ""
    when either anchor is absent (an un-regenerated or missing region)."""
    start = f"<!-- PRESS_KIT:{region} start -->"
    end = f"<!-- PRESS_KIT:{region} end -->"
    i = text.find(start)
    j = text.find(end)
    if i == -1 or j == -1 or j < i:
        return ""
    return text[i + len(start):j]


def test_press_kit_screenshot_set():
    # Canonical PNGs: exactly the expected set, no missing member and no orphan.
    canonical = _stems(_SHOTS, ".png")
    assert canonical == _CANONICAL, (
        f"canonical PNG set mismatch: missing={_CANONICAL - canonical}, "
        f"orphan={canonical - _CANONICAL}"
    )
    for name in _CANONICAL:
        p = _SHOTS / f"{name}.png"
        with Image.open(p) as img:
            assert img.format == "PNG", f"{p.name} is {img.format}, expected PNG"
            assert img.width <= _CANONICAL_MAX_W, (
                f"{p.name} width {img.width} exceeds {_CANONICAL_MAX_W}"
            )

    # Per-vertical WebP galleries: exactly base screens plus that vertical's new shots.
    for vertical in _VERTICALS:
        folder = _SHOTS / vertical
        expected = _vertical_expected(vertical)
        actual = _stems(folder, ".webp")
        assert actual == expected, (
            f"{vertical} gallery set mismatch: missing={expected - actual}, "
            f"orphan={actual - expected}"
        )
        for name in expected:
            p = folder / f"{name}.webp"
            with Image.open(p) as img:
                assert img.format == "WEBP", f"{p} is {img.format}, expected WEBP"
                assert img.width <= _GALLERY_MAX_W, (
                    f"{p} width {img.width} exceeds {_GALLERY_MAX_W}"
                )


def test_press_kit_consumers_reference_new_shots():
    root_readme = (_PRESS_KIT / "README.md").read_text()
    image_locations = (_PRESS_KIT / "IMAGE-LOCATIONS.md").read_text()

    root_grid = _between(root_readme, "grid")
    files_table = _between(image_locations, "files-table")

    # Every promoted shot is referenced by the root README grid and the Files table,
    # each checked independently so a regen that updates one surface but not the other
    # still fails.
    for name in _PROMOTED:
        png = f"{name}.png"
        assert png in root_grid, f"root README grid does not reference {png}"
        assert png in files_table, f"IMAGE-LOCATIONS Files table does not reference {png}"

    # Each vertical gallery README references its own new shots by filename, and its
    # generated screen count matches the folder on disk.
    root_industries = _between(root_readme, "industries")
    industry_table = _between(image_locations, "industry-table")
    for vertical in _VERTICALS:
        gallery = _between((_SHOTS / vertical / "README.md").read_text(), "gallery")
        for name in _NEW_BY_VERTICAL[vertical]:
            webp = f"{name}.webp"
            assert webp in gallery, f"{vertical}/README.md does not reference {webp}"
        count = len(list((_SHOTS / vertical).glob("*.webp")))
        needle = f"{count} screens"
        assert needle in root_industries, (
            f"root README industry list missing '{needle}' for {vertical}"
        )
        assert needle in industry_table, (
            f"IMAGE-LOCATIONS industry table missing '{needle}' for {vertical}"
        )

    # Generated regions are publish-ready: no em dash, no attribution name pulled into
    # a caption or table row (it belongs only to the hand-written copyright blocks).
    attribution = _ATTRIBUTION_RE.search(root_readme)
    assert attribution, "root README copyright line has no recognizable attribution name"
    attributed_name = attribution.group(1)
    generated = [root_grid, root_industries, files_table, industry_table]
    generated += [
        _between((_SHOTS / v / "README.md").read_text(), "gallery") for v in _VERTICALS
    ]
    for region in generated:
        assert _EM_DASH not in region, "generated press-kit copy contains an em dash"
        assert attributed_name not in region, (
            "attribution name appears in a generated caption/row"
        )
