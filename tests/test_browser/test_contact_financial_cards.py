# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The money tiles on a contact page keep every label inside its own tile in English and
German: a long German word never runs into the next tile, and no word is split mid-letter."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser

# Each tile's label box against its tile box, and each tile against the next one.
_TILES_JS = """() => Array.from(document.querySelectorAll('.financial-card')).map(card => {
  const c = card.getBoundingClientRect();
  const l = card.querySelector('.financial-card-label');
  const words = Array.from(l.querySelectorAll('span')).map(s => {
    const r = document.createRange(); r.selectNodeContents(s);
    const b = r.getBoundingClientRect(); return {left: b.left, right: b.right};
  });
  // Widest single word of the label against the room the tile gives it.
  const cs = getComputedStyle(l), ctx = document.createElement('canvas').getContext('2d');
  ctx.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
  const ls = parseFloat(cs.letterSpacing) || 0;
  const widest = Math.max(...l.innerText.toUpperCase().split(/\s+/).filter(Boolean)
    .map(w => ctx.measureText(w).width + ls * w.length));
  return {text: l.innerText, left: c.left, right: c.right, top: c.top, bottom: c.bottom, words,
          widest, room: l.clientWidth};
})"""


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [1024, 1280])
def test_contact_money_tile_labels_stay_inside_their_tiles(page: Page, fresh_company, ui_server, lang, width):
    r = fresh_company.post("/crm/contacts", json={"name": "Tile Customer", "contact_type": "customer"})
    assert r.status_code in (200, 201), r.text
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"/contacts/{r.json()['id']}")
    page.wait_for_selector(".financial-card", state="visible")

    tiles = page.evaluate(_TILES_JS)
    assert len(tiles) == 6, tiles
    for t in tiles:
        for w in t["words"]:
            assert w["left"] >= t["left"] - 0.5 and w["right"] <= t["right"] + 0.5, \
                f"label {t['text']!r} spills out of its tile: {w} vs {t}"
        assert t["widest"] <= t["room"] + 1, f"a word of {t['text']!r} is split mid-letter: {t}"
    for a, b in zip(tiles, tiles[1:]):
        same_row = a["top"] < b["bottom"] and b["top"] < a["bottom"]
        assert not (same_row and a["right"] > b["left"] + 0.5), f"{a['text']!r} overlaps {b['text']!r}"
