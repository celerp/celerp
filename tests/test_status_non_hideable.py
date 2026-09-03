# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""status is a non-hideable field.

status drives lifecycle membership filtering (sold / archived / draft) that runs
before field visibility in the item list. A company that restricts status through a
visible_to_roles override would therefore turn the status filter into a membership
oracle for a role that cannot see the field: probing ?status=sold against the default
view would disclose the hidden value. get_effective_field_schema forces status to
carry an empty visible_to_roles regardless of any base or category override, so the
pre-visibility status filter is always safe and the field is simply visible.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from celerp.services.field_schema import get_effective_field_schema

from test_helpers import create_item as _create_item, perm_setup as _perm_setup


def _session_for(settings: dict):
    session = AsyncMock()
    company = MagicMock()
    company.settings = settings
    session.get.return_value = company
    return session


async def test_status_override_ignored_in_effective_schema():
    """A company that sets a restrictive visible_to_roles on status through a category
    schema (or the base item schema) gets that restriction ignored for status only; every
    other override is honored as before."""
    session = _session_for({
        "item_schema": [
            # A restriction on a non-status field is honored (control).
            {"key": "cost_price", "label": "Cost", "type": "money", "visible_to_roles": ["admin"]},
            # A base-schema attempt to hide status is ignored.
            {"key": "status", "label": "Status", "type": "status", "visible_to_roles": ["admin", "manager"]},
        ],
        "category_schemas": {
            # A category attempt to hide status is ignored too.
            "Widgets": [{"key": "status", "label": "Status", "type": "status",
                         "visible_to_roles": ["admin", "manager"]}],
        },
    })

    base = {f["key"]: f for f in await get_effective_field_schema(session, uuid.uuid4())}
    assert base["status"]["visible_to_roles"] == []
    # The non-status restriction still applies, proving the rule is scoped to status.
    assert base["cost_price"]["visible_to_roles"] == ["admin"]

    cat = {f["key"]: f for f in await get_effective_field_schema(session, uuid.uuid4(), category="Widgets")}
    assert cat["status"]["visible_to_roles"] == []


async def _hide_status_for_category(client, admin_h, category: str) -> None:
    """Admin restricts status to admin/manager for one category, the realistic path an
    operator role would be denied the field through."""
    r = await client.patch(
        f"/companies/me/category-schema/{category}",
        json={"fields": [{"key": "status", "label": "Status", "type": "status",
                          "visible_to_roles": ["admin", "manager"]}]},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text


async def _seed_lifecycle_items(client, admin_h, location_id: str, category: str) -> dict:
    """Create an available, a sold, and an archived item in one category; return skus."""
    skus = {"available": "NH-AVAIL", "sold": "NH-SOLD", "archived": "NH-ARCH"}
    ids: dict[str, str] = {}
    for label, sku in skus.items():
        r = await client.post(
            "/items",
            json={"sku": sku, "name": f"NH {label}", "quantity": 1, "sell_by": "piece",
                  "status": "available", "category": category, "location_id": location_id},
            headers=admin_h,
        )
        assert r.status_code == 200, r.text
        ids[label] = r.json()["id"]
    for label in ("sold", "archived"):
        sr = await client.post(f"/items/{ids[label]}/status",
                               json={"new_status": label}, headers=admin_h)
        assert sr.status_code == 200, sr.text
    return skus


async def _probe_skus(client, headers, status_param: str, own: set[str]) -> set[str]:
    url = "/items" if status_param is None else f"/items?status={status_param}"
    r = await client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    return {i["sku"] for i in r.json()["items"] if i["sku"] in own}


async def test_hidden_status_attempt_is_not_a_membership_oracle(client, session):
    """An admin's attempt to hide status from an operator must change nothing the operator
    can observe: the status filter runs before visibility, so membership under every probe
    is identical to the never-hidden case, and status stays visible on every returned item
    (there is no hidden value to recover)."""
    ctx = await _perm_setup(client, session)
    admin_h, operator_h, location_id = ctx["admin_h"], ctx["operator_h"], ctx["location_id"]
    category = "Widgets"
    skus = await _seed_lifecycle_items(client, admin_h, location_id, category)
    own = set(skus.values())

    probes = [None, "all", "sold", "available", "archived"]
    baseline = {p: await _probe_skus(client, operator_h, p, own) for p in probes}
    # Sanity: the seeded lifecycle is what the probes are meant to separate.
    assert baseline["available"] == {skus["available"]}
    assert baseline["sold"] == {skus["sold"]}
    assert baseline["archived"] == {skus["archived"]}
    assert baseline[None] == {skus["available"]}  # default hides sold + archived

    await _hide_status_for_category(client, admin_h, category)

    for p in probes:
        after = await _probe_skus(client, operator_h, p, own)
        assert after == baseline[p], f"probe {p!r} membership changed after hide attempt"

    # Red-first anchor: status stays present on every item the operator receives, so the
    # value the admin tried to hide is simply visible rather than leaked through a probe.
    r = await client.get("/items?status=all", headers=operator_h)
    mine = [i for i in r.json()["items"] if i["sku"] in own]
    assert len(mine) == 3
    for item in mine:
        assert "status" in item, "status stripped from operator view - membership oracle"


async def test_bogus_status_probe_gives_no_oracle(client, session):
    """A garbage ?status value returns a neutral empty set for the operator whose status the
    admin tried to hide, identical to the owner's - no value can be teased out through an
    unmatched status - while a valid probe still shows status on the returned items."""
    ctx = await _perm_setup(client, session)
    admin_h, operator_h, location_id = ctx["admin_h"], ctx["operator_h"], ctx["location_id"]
    category = "Widgets"
    skus = await _seed_lifecycle_items(client, admin_h, location_id, category)
    own = set(skus.values())

    await _hide_status_for_category(client, admin_h, category)

    bogus = "zzz-not-a-status"
    assert await _probe_skus(client, operator_h, bogus, own) == set()
    assert await _probe_skus(client, admin_h, bogus, own) == set()

    # Red-first anchor: a real probe still carries the status field to the operator.
    r = await client.get("/items?status=all", headers=operator_h)
    mine = [i for i in r.json()["items"] if i["sku"] in own]
    assert mine, "operator saw none of the seeded items"
    for item in mine:
        assert "status" in item
