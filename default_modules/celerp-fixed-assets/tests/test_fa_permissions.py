# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Which permission each fixed-asset action needs.

Anything that posts to the ledger or changes how an asset is measured needs Manage
accounting. Manage fixed assets covers what posts nothing: adding a physical record,
descriptive edits, moves, readings, files, upkeep and asking for a correction. A sale
is a document, so it needs Create & edit documents with Manage fixed assets. Recording a
check needs Record asset checks; a custodian confirms or reports on their own assets.
"""
from __future__ import annotations

import importlib

import pytest

EXPECTED = {
    ('manage_accounting',): {
        "POST /start",
        "PATCH /settings",
        "POST /classes",
        "PATCH /classes/{class_id}",
        "POST /register",
        "POST /not-acquisition",
        "POST /keep-line",
        "POST /link-depreciation",
        "POST /sources/undo",
        "POST /opening/finish",
        "POST /imports",
        "POST /imports/{batch_id}/rows",
        "POST /imports/{batch_id}/undo",
        "POST /reconciliation/acknowledge",
        "POST /assets/{asset_id}/undo",
        "POST /assets/{asset_id}/financial-details",
        "POST /runs",
        "POST /runs/{month}/reverse",
        "POST /assets/{asset_id}/skip",
        "DELETE /assets/{asset_id}/skip/{month}",
        "POST /assets/{asset_id}/dispose",
        "POST /dispose",
        "POST /assets/{asset_id}/undo-disposal",
        "POST /assets/{asset_id}/correct",
        "POST /corrections/{correction_id}/apply",
        "POST /corrections/{correction_id}/undo",
        "POST /assets/{asset_id}/revalue",
        "POST /assets/{asset_id}/impair",
        "POST /assets/{asset_id}/reverse-impairment",
        "POST /classes/{class_id}/revalue",
        "POST /assets/{asset_id}/reviews",
        "POST /assets/{asset_id}/reviews/{review_id}/withdraw",
        "POST /assets/{asset_id}/add",
        "POST /assets/{asset_id}/place-in-service",
        "POST /assets/{asset_id}/undo-latest",
        "POST /assets/{asset_id}/reclassify",
        "POST /assets/{asset_id}/transfer",
        "POST /classes/{class_id}/apply-to-assets",
        "POST /classes/{class_id}/fix-account",
        "POST /classes/{class_id}/fix-account/undo",
        "POST /class-table",
        "POST /class-table/{op_key}/undo",
        "PATCH /tax-books/{asset_id}/{regime}",
        "POST /tax-years/{regime}/{year}/close",
        "POST /tax-years/{regime}/{year}/reopen",
        "POST /year-end/{year}/marks",
        },
    ('manage_fixed_assets',): {
        "POST /assets",
        "PATCH /assets/{asset_id}",
        "POST /assets/{asset_id}/readings",
        "POST /assets/{asset_id}/corrections",
        "POST /corrections/{correction_id}/withdraw",
        "POST /assets/{asset_id}/move",
        "POST /move",
        "POST /assets/{asset_id}/undo-move",
        "POST /assets/{asset_id}/reminders",
        "POST /reminders/{reminder_id}/done",
        "POST /reminders/{reminder_id}/postpone",
        "PATCH /reminders/{reminder_id}",
        "POST /reminders/{reminder_id}/remove",
        "POST /reminders/{reminder_id}/undo",
        "POST /assets/{asset_id}/maintenance",
        "PATCH /maintenance/{maint_id}",
        "POST /maintenance/{maint_id}/remove",
        "POST /maintenance/{maint_id}/restore",
        "POST /maintenance/{maint_id}/files",
        "POST /assets/{asset_id}/files",
        "POST /assets/{asset_id}/files/{file_id}/tag",
        "PATCH /assets/{asset_id}/files/{file_id}/description",
        "POST /classes/{class_id}/valuer-report",
        "POST /classes/{class_id}/valuer-report/remove",
        "DELETE /assets/{asset_id}/files/{file_id}",
        },
    ('edit_documents', 'manage_fixed_assets'): {
        "POST /assets/{asset_id}/sell",
        "POST /assets/{asset_id}/sale/{action}",
        },
    ('check_fixed_assets',): {
        "POST /checks",
        "DELETE /checks/{check_id}",
        },
    (): {
        "POST /mine/{asset_id}/confirm",
        "POST /mine/{asset_id}/problems",
        },
}


def _keys(dependant, out: set) -> set:
    key = getattr(dependant.call, "required_permission", None)
    if key:
        out.add(key)
    for dep in dependant.dependencies:
        _keys(dep, out)
    return out


def test_l2_every_write_needs_the_permission_of_its_kind():
    got: dict[tuple, set] = {}
    for name in ("routes", "upkeep", "files", "checks", "custody"):
        for route in importlib.import_module(f"celerp_fixed_assets.{name}").router.routes:
            for method in route.methods - {"GET"}:
                got.setdefault(tuple(sorted(_keys(route.dependant, set()))), set()).add(
                    f"{method} {route.path.removeprefix('/fixed-assets/api')}")
    assert got == {k: set(v) for k, v in EXPECTED.items()}


@pytest.mark.asyncio
async def test_l2_operator_is_told_what_disposing_needs(fa):
    """An operator holds Manage fixed assets and not Manage accounting by default: a
    disposal is refused naming the permission it needs."""
    from test_runs import _contributed, _managed_from

    await _managed_from(fa)
    car = await _contributed(fa)
    operator = await fa.role_headers("operator")
    r = await fa.post(f"/assets/{car}/dispose", {"date": "2026-05-01", "reason": "lost", "preview": True,
                                                 "op_key": "l2"}, operator)
    assert r.status_code == 403 and "Manage accounting" in r.json()["detail"], r.text


@pytest.mark.asyncio
async def test_l3_a_role_outside_the_known_levels_cannot_be_given_and_reaches_nothing(fa):
    """No route assigns a role outside the known levels; a membership holding one anyway
    is refused every fixed asset read and write, never granted by accident."""
    from test_runs import _contributed, _managed_from

    r = await fa.client.post("/companies/me/users", headers=fa.h, json={
        "email": "l3@test.co", "name": "L3", "role": "staff", "password": "pass1234"})
    assert r.status_code == 400 and "Invalid role" in r.text, r.text
    uid, _ = await fa.member("operator")
    r = await fa.client.patch(f"/companies/me/users/{uid}", headers=fa.h, json={"role": "staff"})
    assert r.status_code == 400 and "Invalid role" in r.text, r.text

    await _managed_from(fa)
    car = await _contributed(fa)
    staff = await fa.role_headers("staff")
    assert (await fa.client.get("/fixed-assets/api/assets", headers=staff)).status_code == 403
    r = await fa.post(f"/assets/{car}/move", {"location": "Back room", "op_key": "l3"}, staff)
    assert r.status_code == 403, r.text
    r = await fa.post(f"/assets/{car}/dispose", {"date": "2026-05-01", "reason": "lost", "preview": True,
                                                 "op_key": "l3"}, staff)
    assert r.status_code == 403, r.text


async def test_activity_and_the_ledger_leave_out_asset_events_for_a_role_that_cannot_view_assets(fa):
    from test_runs import _contributed, _managed_from

    await _managed_from(fa)
    car = await _contributed(fa)
    entry = next(e for e in (await fa.client.get("/ledger", headers=fa.h)).json()["items"]
                 if e["event_type"].startswith("fa."))
    for role, sees in (("viewer", False), ("operator", True)):
        headers = await fa.role_headers(role)
        feeds = [(await fa.client.get("/dashboard/activity", headers=headers)).json()["activities"],
                 (await fa.client.get("/dashboard/activity/search", headers=headers)).json()["activities"],
                 (await fa.client.get("/ledger", headers=headers)).json()["items"]]
        for feed in feeds:
            assert any(e["event_type"].startswith("fa.") for e in feed) is sees, (role, feed)
            assert any(e["entity_id"] == car for e in feed) is sees, role
        one = await fa.client.get(f"/ledger/{entry['id']}", headers=headers)
        assert one.status_code == (200 if sees else 404), (role, one.text)
