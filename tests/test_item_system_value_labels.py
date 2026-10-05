# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An item's status badge and its system units (piece, gram, ...) read in the user's
language on the item page while the stored values stay the canonical keys; a unit
the company added itself shows exactly as named."""
from __future__ import annotations

import re

import pytest

from celerp.services.units import DEFAULT_UNITS
from ui import i18n

_DE = {"Accept-Language": "de"}
# Every status an item can be in: the import starting states plus the ones the
# stock events move it to.
_ITEM_STATUSES = ("available", "draft", "archived", "reserved", "sold", "memo_out",
                  "expired", "disposed", "merged")
_UNIT_SPANS = re.compile(r'class="paired-(?:primary|secondary|tertiary)[^"]*"[^>]*>([^<]+)<')


async def _item_page(owner_ui, **item) -> str:
    r = await owner_ui.api.post("/items", json={"sku": "SYS-1", "name": "Stone", "quantity": 2, **item})
    assert r.status_code in (200, 201), r.text
    page = await owner_ui.get(f"/inventory/{r.json()['id']}", headers=_DE)
    assert page.status_code == 200
    return page.text


@pytest.mark.asyncio
async def test_status_badge_and_system_units_show_in_the_users_language(owner_ui):
    html = await _item_page(owner_ui, sell_by="piece", purchase_unit="gram", weight=1.5, weight_unit="gram")
    assert re.search(r'class="badge badge--draft">Entwurf<', html), "the status badge"
    units = set(_UNIT_SPANS.findall(html))
    assert {"Stück", "Gramm"} <= units and not {"piece", "gram"} & units, units


@pytest.mark.asyncio
async def test_a_company_unit_shows_as_named(owner_ui):
    units = (await owner_ui.api.get("/companies/me/units")).json()
    r = await owner_ui.api.put("/companies/me/units", json={"units": [
        *units, {"name": "bale", "label": "Bale", "decimals": 0, "unit_type": "pieces"}]})
    assert r.status_code == 200, r.text
    assert "bale" in _UNIT_SPANS.findall(await _item_page(owner_ui, sell_by="bale"))


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
def test_every_item_status_and_system_unit_has_a_label_in_every_locale(lang):
    cat = i18n._cached_load(lang)
    assert [s for s in _ITEM_STATUSES if not cat.get(f"enum.item_status.{s}")] == []
    assert [u["name"] for u in DEFAULT_UNITS if not cat.get(f"unit.{u['name']}")] == []
