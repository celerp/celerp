# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Asset tags printed through celerp-labels (the label_source slot in the manifest, which
names the row keys and the preset template): each asset's number, tag, name, class,
location and in-service date, with its tag (else its number) as the barcode. No money is
ever on a tag."""
from __future__ import annotations

from celerp_fixed_assets import setup
from celerp_fixed_assets.custody import _locations
from celerp_fixed_assets.store import _entities


async def asset_tag_rows(*, session, company_id, ids: list[str]) -> list[dict]:
    """One row per asset among ``ids`` still on the register."""
    wanted = set(ids)
    names = {c["id"]: c.get("name") for c in await setup.classes(session, company_id, include_archived=True)}
    locations = await _locations(session, company_id)
    return [{"id": eid, "number": s["number"], "tag": s.get("tag"), "name": s.get("name"),
             "class": names.get(s.get("class_id")), "location": locations.get(str(s.get("location_id"))),
             "in_service": s.get("available_date"), "barcode": s.get("tag") or s["number"]}
            for eid, s in await _entities(session, company_id, "fa_asset")
            if eid in wanted and s.get("number") and s.get("status") != "undone"]
