# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Who can read which ledger events: one policy for every reader.

Each event is read with the permission of the module that owns it (accounting entries with
view_financial_reports, fixed assets with view_fixed_assets, and so on), whether that module is
enabled or not. The event list, a single event, the dashboard activity and every History panel
apply the same policy, and every projection handler must name the permission its events are
read with."""
from __future__ import annotations

import pytest

from test_helpers import invite_user, register_admin

_ACCOUNTING = ("je.", "acc.", "account.")


async def _demo(client, session):
    owner = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await client.post("/companies/me/demo/reseed", headers=owner)
    assert r.status_code == 200, r.text
    viewer = {"Authorization": f"Bearer {await invite_user(client, session, owner, 'viewer@perm.example', 'viewer')}"}
    operator = {"Authorization": f"Bearer {await invite_user(client, session, owner, 'op@perm.example', 'operator')}"}
    return owner, viewer, operator


async def _events(client, headers) -> list[dict]:
    out, cursor = [], {}
    while True:
        r = await client.get("/ledger", params={"limit": 500, **cursor}, headers=headers)
        assert r.status_code == 200, r.text
        out += r.json()["items"]
        if not r.json()["has_older"]:
            return out
        cursor = {"before": out[-1]["id"]}


def _of(events, prefixes) -> list[dict]:
    return [e for e in events if e["event_type"].startswith(prefixes)]


async def _activity(client, headers) -> list[str]:
    r = await client.get("/dashboard/activity", params={"limit": 100}, headers=headers)
    assert r.status_code == 200, r.text
    return [a.get("event_type") or "" for a in r.json()["activities"]]


@pytest.mark.asyncio
async def test_a_role_without_financial_reports_reads_no_accounting_entry(client, session):
    owner, viewer, operator = await _demo(client, session)
    entries = _of(await _events(client, owner), _ACCOUNTING)
    assert entries, "the demo company posts journal entries"
    for headers in (viewer, operator):
        seen = await _events(client, headers)
        assert _of(seen, _ACCOUNTING) == []
        assert (await client.get(f"/ledger/{entries[0]['id']}", headers=headers)).status_code == 404
        r = await client.get("/ledger", params={"event_type": entries[0]["event_type"]}, headers=headers)
        assert r.json()["items"] == [] and r.json()["total"] == 0
        assert not [t for t in await _activity(client, headers) if t.startswith(_ACCOUNTING)]
        total = (await client.get("/ledger", params={"limit": 1}, headers=headers)).json()["total"]
        assert total == len(seen)
    assert (await client.get(f"/ledger/{entries[0]['id']}", headers=owner)).status_code == 200


@pytest.mark.asyncio
async def test_a_custom_grant_opens_accounting_entries_and_its_revocation_closes_them(client, session):
    owner, _viewer, operator = await _demo(client, session)
    entry = _of(await _events(client, owner), _ACCOUNTING)[0]

    async def toggle(granted: bool):
        r = await client.patch("/companies/me/role-permissions", headers=owner, json={
            "perm_key": "view_financial_reports", "role_key": "operator", "granted": granted})
        assert r.status_code == 200, r.text

    await toggle(True)
    assert _of(await _events(client, operator), _ACCOUNTING)
    assert (await client.get(f"/ledger/{entry['id']}", headers=operator)).status_code == 200
    await toggle(False)
    assert _of(await _events(client, operator), _ACCOUNTING) == []
    assert (await client.get(f"/ledger/{entry['id']}", headers=operator)).status_code == 404


@pytest.mark.asyncio
async def test_fund_and_schedule_events_read_with_financial_reports_and_without_costs(client, session):
    """Fund and revenue schedule events are accounting events: a role granted financial reports
    reads them, with every cost amount gone from their data and metadata when it cannot see costs;
    a role without financial reports reads none of them."""
    import uuid

    from sqlalchemy import select

    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry

    owner, viewer, operator = await _demo(client, session)
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    session.add_all([LedgerEntry(
        company_id=company_id, entity_id=entity_id, entity_type=entity_type, event_type=event_type,
        data={"name": "Building fund", "line_items": [{"name": "Bricks", "cost_price": 48213.57}]},
        metadata_={"reason": "board_vote", "valued": {"unit_cost": 39102.46}}, source="test",
        idempotency_key=f"fund-read-{uuid.uuid4()}")
        for entity_id, entity_type, event_type in (("fund:read", "fund", "fund.created"),
                                                   ("rev_schedule:read", "rev_schedule", "rev_schedule.created"))])
    await session.flush()
    r = await client.patch("/companies/me/role-permissions", headers=owner, json={
        "perm_key": "view_financial_reports", "role_key": "operator", "granted": True})
    assert r.status_code == 200, r.text

    seen = _of(await _events(client, operator), ("fund.", "rev_schedule."))
    assert sorted(e["event_type"] for e in seen) == ["fund.created", "rev_schedule.created"]
    for e in seen:
        assert e["data"] == {"name": "Building fund", "line_items": [{"name": "Bricks"}]}
        assert e["metadata"] == {"reason": "board_vote", "valued": {}}
        single = await client.get(f"/ledger/{e['id']}", headers=operator)
        assert single.status_code == 200 and "48213.57" not in single.text and "39102.46" not in single.text
    assert _of(await _events(client, viewer), ("fund.", "rev_schedule.")) == []
    whole = _of(await _events(client, owner), ("fund.", "rev_schedule."))
    assert all(e["data"]["line_items"][0]["cost_price"] == 48213.57 for e in whole) and len(whole) == 2


@pytest.mark.asyncio
async def test_a_module_taken_out_leaves_its_events_to_company_admins(client, session, monkeypatch):
    """Fixed asset events fall to the company-admin default once the module that owns them
    is no longer loaded: its permission no longer opens them."""
    from celerp.modules import loader, slots

    owner, viewer, operator = await _demo(client, session)
    started = await client.post("/fixed-assets/api/start", headers=owner, json={})
    assert started.status_code == 200, started.text
    body = {"kind": "physical", "name": "Ladder", "op_key": "ladder"}
    preview = await client.post("/fixed-assets/api/assets", headers=owner, json={**body, "preview": True})
    assert preview.status_code == 200, preview.text
    done = await client.post("/fixed-assets/api/assets", headers=owner,
                             json={**body, "preview": False, "fingerprint": preview.json()["fingerprint"]})
    assert done.status_code == 200, done.text
    assets = _of(await _events(client, owner), ("fa.",))
    assert assets
    monkeypatch.setattr(slots, "_slots", {k: list(v) for k, v in slots._slots.items()})
    slots.unregister_module("celerp-fixed-assets")
    assert not [c for c in slots.get("projection_handler") if c["prefix"] == "fa."]
    monkeypatch.setattr(loader, "_event_permissions", {})  # as a load or take-out leaves it

    for role in (viewer, operator):  # operators hold view_fixed_assets
        assert _of(await _events(client, role), ("fa.",)) == []
        assert (await client.get(f"/ledger/{assets[0]['id']}", headers=role)).status_code == 404
    assert _of(await _events(client, owner), ("fa.",))


@pytest.mark.asyncio
async def test_another_company_reads_none_of_these_events(client, session):
    owner, _viewer, _operator = await _demo(client, session)
    entry = (await _events(client, owner))[0]
    r = await client.post("/companies", json={"name": "Other Co"}, headers=owner)
    assert r.status_code == 200, r.text
    other = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get(f"/ledger/{entry['id']}", headers=other)).status_code == 404
    assert entry["id"] not in {e["id"] for e in await _events(client, other)}


@pytest.mark.asyncio
async def test_an_event_no_module_owns_is_read_by_company_admins_only(client, session):
    from celerp.services.activity_redaction import can_read_event
    assert can_read_event("zz.unknown.recorded", {}, "admin")
    assert not can_read_event("zz.unknown.recorded", {}, "manager")
    assert can_read_event("sys.older_stock.placed", {}, "viewer")
    assert not can_read_event("sys.user.created", {}, "manager")


def test_every_projection_handler_names_the_permission_its_events_are_read_with():
    from celerp.modules import slots
    from celerp.modules.loader import event_read_permissions
    from celerp.services.permissions import is_permission_key

    registered = slots.get("projection_handler")
    assert registered
    assert [c for c in registered if not is_permission_key(c.get("permission"))] == []
    installed = event_read_permissions()
    assert {c["prefix"] for c in registered} <= set(installed)
    assert installed["acc."] == "view_financial_reports" and installed["item."] == "view_inventory"


def test_the_kept_event_permissions_are_the_loaded_modules_own():
    """What every ledger read uses is the permission on each projection handler the loaded
    modules registered, and nothing from a module that is only installed."""
    from celerp.modules import loader
    from celerp.modules.slots import get as get_slot

    registered = {c["prefix"]: c["permission"] for c in get_slot("projection_handler") if "permission" in c}
    assert registered and loader.event_read_permissions() == registered


@pytest.mark.asyncio
async def test_ledger_history_and_dashboard_answer_beside_malformed_installed_modules(client, session, tmp_path,
                                                                                     monkeypatch):
    """An installed module that is turned off or refused, its manifest malformed, changes no
    reader: every one still answers."""
    from celerp.modules import loader
    from test_modules.test_admission import _write_module

    owner, viewer, operator = await _demo(client, session)
    for i, slots in enumerate((["projection_handler"], {"projection_handler": "x.:apply"},
                               {"projection_handler": ["x."]}, {"projection_handler": [{"handler": "x:y"}]})):
        _write_module(tmp_path, f"acme-bad-{i}", {"name": f"acme-bad-{i}", "version": "1.0.0", "slots": slots})
    monkeypatch.setenv("MODULE_DIR", str(tmp_path))
    before = await client.get("/ledger", params={"limit": 5}, headers=owner)
    loader._event_permissions.clear()
    entity = before.json()["items"][0]["entity_id"]
    for headers in (owner, operator):
        for path, params in (("/ledger", {"limit": 5}), ("/ledger", {"entity_id": entity}),
                             ("/dashboard/activity", {}), ("/dashboard/activity/search", {"q": "a"})):
            r = await client.get(path, params=params, headers=headers)
            assert r.status_code == 200, (path, r.text[:300])
