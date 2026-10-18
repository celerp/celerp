# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Finding assets by what is written on them: the register's search box and the global
search bar match the same fields the same way."""
from __future__ import annotations

from celerp_fixed_assets import setup
from celerp_fixed_assets.store import _entities

# What a search matches, without case.
SEARCHED = ("number", "tag", "name", "serial")


def matching(rows: list[tuple[str, dict]], q: str | None) -> list[tuple[str, dict]]:
    """The assets on the register (an undone asset never was) matching ``q``, newest first."""
    needle = (q or "").strip().casefold()
    found = [(eid, s) for eid, s in rows if s.get("number") and s.get("status") != "undone"
             and (not needle or any(needle in str(s.get(k) or "").casefold() for k in SEARCHED))]
    return sorted(found, key=lambda r: r[1]["number"], reverse=True)


async def global_search(session, company_id, role: str, q: str, limit: int) -> dict:  # noqa: ARG001
    """Global search provider: the assets matching ``q``, without money."""
    names = {c["id"]: c.get("name") for c in await setup.classes(session, company_id, include_archived=True)}
    found = matching(await _entities(session, company_id, "fa_asset"), q)[:limit]
    return {"items": [{"id": eid, "label": f"{s['number']} {s.get('name') or ''}".strip(),
                       "href": f"/fixed-assets/assets/{eid}",
                       "subtitle": " · ".join(str(v) for v in (s.get("tag"), names.get(s.get("class_id"))) if v)}
                      for eid, s in found]}
