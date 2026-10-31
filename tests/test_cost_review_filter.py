# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GET /items?filter=cost_review lists the lots whose goods cost is worth checking, and none
to a role that cannot see costs.

Listed: a lot whose goods cost is unknown; a lot holding units whose value is freight only
(a goods cost cleared while it carried freight, before clearing left it unknown); a merge
result whose cost is still the merge's own though a part had no goods cost (merged before
such a merge made its cost unknown). Lots costed before goods costs could be unknown keep
their value: the filter lists them, it does not revalue them."""
from __future__ import annotations

from sqlalchemy import select

from celerp.models.projections import Projection
from test_merge_unknown_cost import MARK, _item, _merge, _set_cost
from test_landed_cost_removals import _seed
from ui.routes.inventory import COST_REVIEW_FILTER


async def _listed(client, headers) -> set[str]:
    r = await client.get("/items", headers=headers, params={"filter": COST_REVIEW_FILTER, "limit": 500})
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


async def _as_before_this_change(session, auth, item_id: str, **state) -> None:
    """Rewrite a lot's state to what an earlier release left it as."""
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == item_id))).scalar_one()
    fresh = {k: v for k, v in {**row.state, **state}.items() if v is not None}
    row.state = fresh
    await session.commit()


async def test_cost_review_lists_lots_to_check_and_leaves_their_value(client, session, auth):
    await _seed(session, auth)
    known = await _item(client, auth, qty=2, cost_total=100.0)
    unknown_part = await _item(client, auth, qty=3)
    unknown_merge = await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=40.0),
                                                await _item(client, auth, qty=1)])
    old_merge = await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=50.0), unknown_part])
    await _as_before_this_change(session, auth, old_merge, **{MARK: None})
    freight_only = await _item(client, auth, qty=4, cost_total=30.0)
    await _as_before_this_change(session, auth, freight_only, landed_costs={"bill:old": 30.0}, cost_base=0.0)

    listed = await _listed(client, auth["headers"])
    assert {unknown_merge, old_merge, freight_only} <= listed, listed
    assert known not in listed

    before = (await client.get(f"/items/{old_merge}", headers=auth["headers"])).json()["cost_total"]
    assert before == 50.0
    await _set_cost(client, auth, old_merge, 80.0)
    assert old_merge not in await _listed(client, auth["headers"])


async def test_cost_review_lists_nothing_to_a_role_that_cannot_see_costs(client, session, auth):
    from test_company_settings_owners import _user_with_role
    unknown = await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=40.0),
                                          await _item(client, auth, qty=1)])
    assert unknown in await _listed(client, auth["headers"])
    operator = await _user_with_role(client, session, auth["headers"], "operator")
    assert await _listed(client, operator) == set()
