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
    out, offset = [], 0
    while True:
        r = await client.get("/ledger", params={"limit": 500, "offset": offset}, headers=headers)
        assert r.status_code == 200, r.text
        page = r.json()["items"]
        out += page
        if len(page) < 500:
            return out
        offset += 500


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
        assert r.json() == {"items": [], "total": 0}
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
async def test_a_disabled_module_keeps_its_events_behind_its_permission(client, session, monkeypatch):
    """Fixed asset events stay hidden from a viewer when the module that owns them is not enabled."""
    from celerp.modules import slots

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

    assert _of(await _events(client, viewer), ("fa.",)) == []
    assert (await client.get(f"/ledger/{assets[0]['id']}", headers=viewer)).status_code == 404
    assert _of(await _events(client, operator), ("fa.",))  # operators hold view_fixed_assets


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
    assert installed["je."] == "view_financial_reports" and installed["item."] == "view_inventory"


@pytest.mark.parametrize("permission", [None, "", "no_such_permission"])
def test_a_projection_handler_without_a_read_permission_is_refused(permission):
    from celerp.modules import slots

    entry = {"prefix": "zz.", "handler": "celerp.projections.engine:_merge", "_module": "zz", "_first_party": False}
    if permission is not None:
        entry["permission"] = permission
    with pytest.raises(ValueError):
        slots.check("projection_handler", entry)
