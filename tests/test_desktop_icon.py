# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The desktop app icon is the Celerp shield: the window and Linux png, the Windows
ico and the macOS icns each carry the shield on a transparent background at the
sizes their platform needs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

_ASSETS = Path(__file__).resolve().parent.parent / "electron" / "assets"
_ICO_SIZES = {16, 24, 32, 48, 64, 128, 256}
_ICNS_SIZES = {16, 32, 64, 128, 256, 512, 1024}
_OLD_ICON_MD5 = {"159ad80a7f712ece825effe1569a49a3", "593ae339b0cf4c3ca3c3e17180ab5777"}


def _assert_shield(im: Image.Image) -> None:
    """Transparent corners, and the opaque pixels are mostly the shield's blue field."""
    im = im.convert("RGBA")
    w, h = im.size
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        assert im.getpixel(xy)[3] == 0, f"corner {xy} is not transparent"
    opaque = [p for p in im.getdata() if p[3] == 255]
    assert opaque, "no opaque pixels"
    blue = sum(1 for r, g, b, _ in opaque if b > r + 40 and b > g)
    assert blue / len(opaque) > 0.3, "opaque area is not the shield's blue field"


@pytest.mark.parametrize("name", ["icon.png", "icon.ico", "icon.icns"])
def test_desktop_icon_is_not_the_old_image(name):
    assert hashlib.md5((_ASSETS / name).read_bytes()).hexdigest() not in _OLD_ICON_MD5


def test_window_and_linux_icon_is_512_rgba_shield():
    with Image.open(_ASSETS / "icon.png") as im:
        assert (im.format, im.size, im.mode) == ("PNG", (512, 512), "RGBA")
        _assert_shield(im)


def test_windows_ico_carries_every_size():
    with Image.open(_ASSETS / "icon.ico") as im:
        assert im.format == "ICO"
        assert {w for w, h in im.ico.sizes()} == _ICO_SIZES
        for size in _ICO_SIZES:
            frame = im.ico.getimage((size, size))
            assert frame.size == (size, size)
            if size >= 48:
                _assert_shield(frame)


def test_macos_icns_carries_every_size():
    with Image.open(_ASSETS / "icon.icns") as im:
        assert im.format == "ICNS"
        assert {w * s for w, h, s in im.info["sizes"]} >= _ICNS_SIZES
        im.load()  # Pillow opens an icns at its largest entry
        assert im.size == (1024, 1024)
        _assert_shield(im)
