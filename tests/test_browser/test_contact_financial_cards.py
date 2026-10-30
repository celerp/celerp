# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The money tiles on a contact page keep every label inside its own tile in English and
German: a long German word never runs into the next tile, and no word is split mid-letter."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser

# Each tile's label box against its tile box, and each tile against the next one.
_TILES_JS = r"""() => Array.from(document.querySelectorAll('.financial-card')).map(card => {
  const c = card.getBoundingClientRect();
  const l = card.querySelector('.financial-card-label');
  const words = Array.from(l.querySelectorAll('span')).map(s => {
    const r = document.createRange(); r.selectNodeContents(s);
    const b = r.getBoundingClientRect(); return {left: b.left, right: b.right};
  });
  // Words the browser broke across lines: a whole word sits on one line, so its rendered
  // boxes share one top; a word split mid-letter has boxes on two lines.
  const split = [], walker = document.createTreeWalker(l, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    for (const m of n.data.matchAll(/\S+/g)) {
      const r = document.createRange(); r.setStart(n, m.index); r.setEnd(n, m.index + m[0].length);
      const tops = new Set(Array.from(r.getClientRects()).filter(b => b.width > 0).map(b => Math.round(b.top)));
      if (tops.size > 1) split.push(m[0]);
    }
  }
  return {text: l.innerText, left: c.left, right: c.right, top: c.top, bottom: c.bottom, words, split};
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
        assert not t["split"], f"{t['split']} split mid-letter in {t['text']!r}: {t}"
    for a, b in zip(tiles, tiles[1:]):
        same_row = a["top"] < b["bottom"] and b["top"] < a["bottom"]
        assert not (same_row and a["right"] > b["left"] + 0.5), f"{a['text']!r} overlaps {b['text']!r}"
