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


async def _statements(session, auth) -> tuple[set[str], list[tuple[str, int]]]:
    """cost_review_ids for the company, and each SQL statement it ran with its parameter count."""
    from celerp_inventory.services import cost_review_ids
    from sqlalchemy import event
    engine = session.bind.sync_engine
    ran: list[tuple[str, int]] = []

    def record(conn, cursor, statement, parameters, *args):
        if not statement.startswith(("SAVEPOINT", "RELEASE")):
            ran.append((statement, len(parameters or ())))

    event.listen(engine, "before_cursor_execute", record)
    try:
        review = await cost_review_ids(session, auth["company_id"])
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return review, ran


async def test_cost_review_reads_lots_in_fixed_statements_whatever_their_number(client, session, auth):
    """The statements and their parameters do not grow with the lots, so a company of any
    size gets its list (a list of ids bound one parameter each stops at the driver's limit)."""
    await _seed(session, auth)
    merges: list[str] = []
    runs = []
    for _ in range(2):
        for _ in range(3):
            merge = await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=50.0),
                                                await _item(client, auth, qty=3)])
            await _as_before_this_change(session, auth, merge, **{MARK: None})
            merges.append(merge)
        review, ran = await _statements(session, auth)
        assert set(merges) <= review
        runs.append(ran)
    assert runs[0] == runs[1], runs


async def test_cost_review_lists_a_company_past_the_bind_parameter_limit(client, session, auth):
    """More lots than PostgreSQL drivers can bind as parameters (32767) still list."""
    import uuid
    from datetime import datetime, timezone
    from sqlalchemy import insert
    unknown = await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=40.0),
                                          await _item(client, auth, qty=1)])
    rows = [{"company_id": auth["company_id"], "entity_id": f"item:{uuid.uuid4()}", "entity_type": "item",
             "state": {"quantity": 1, "cost_total": 1.0, "cost_base": 1.0}, "version": 1,
             "updated_at": datetime.now(timezone.utc)} for _ in range(32768)]
    for i in range(0, len(rows), 4000):
        await session.execute(insert(Projection), rows[i:i + 4000])
    await session.commit()
    assert unknown in await _listed(client, auth["headers"])


async def test_cost_review_page_tells_a_role_that_cannot_see_costs_why_its_list_is_empty(client, session, auth):
    """The list is withheld from a role without view_inventory_costs, so its page says that
    instead of claiming no lot needs checking; a role that sees costs and has none to check
    reads the ordinary empty message."""
    import re
    from test_company_settings_owners import _user_with_role
    from test_list_old_rule_total_ui import _browser
    from ui.i18n import t
    from ui.routes.inventory import _FILTER_SCOPES

    async def _page(headers) -> str:
        async with _browser(headers["Authorization"].split()[1]) as ui:
            return re.sub(r"<[^>]+>", "", (await ui.get(f"/inventory?filter={COST_REVIEW_FILTER}")).text)

    assert t("inventory.no_cost_review_items") in await _page(auth["headers"])
    await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=40.0), await _item(client, auth, qty=1)])
    operator = await _user_with_role(client, session, auth["headers"], "operator")
    page = await _page(operator)
    assert t("inventory.cost_review_withheld") in page
    assert t("inventory.no_cost_review_items") not in page
    # Every scope a permission can empty has a message of its own for the role without it.
    for _label, _basis, empty, permission, withheld in _FILTER_SCOPES.values():
        assert bool(permission) == bool(withheld) and (not withheld or t(withheld) != t(empty))
