# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Consent to automatic billing belongs to whoever set its terms.

Whatever the agreement's status (draft, active, paused, cancelled, void), a write that chooses an
automatic mode, or changes a term of one chosen, needs the mode's permissions from the writer's
role and records that writer's consent; it happens in the one writer of agreement fields
(billing.write_agreement), which reads the terms as configured, never whether they run now.
Resume and Activate change the status only, keep the consent where it is, and say what now runs
automatically and under whose authorization. A change that brings a chosen mode into force (a status
that makes the agreement bill, whoever writes it) also asks whoever makes it for that mode's
permissions, at write_agreement for the module's writes and at the status guard for every other.
The doors are read from the code (consent_census): each subscriptions route that writes status or
a consent term is driven here by a role that may neither bill nor automate, so a new door fails
until it is given a case below, and the grids at write_agreement and the status guard are
generated over every status, every pair of statuses and every automatic field."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from agreement_kit import (
    agreement, billed_docs, company_id, freeze_day, member, register, revoke, schedule, template, tick,
)
from consent_census import (
    agreement_writes, automatic_fields, automatic_writers, consent_writers, documents_gaps, marker_writers,
    outside_writes, route_doors, status_routes,
)

from celerp_subscriptions.billing import AGREEMENT_STATUSES  # noqa: E402

# The route doors that write a status, read from the code.
STATUS_DOORS = sorted(name for name, (_, keys, _) in route_doors().items() if "status" in keys)

WRITE_AGREEMENT = "default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::write_agreement"


async def _configured(client, session, monkeypatch, *, status: str = "active", send: bool = False):
    """An agreement from 2026-01-01 whose automatic terms the owner set (window 6, sending if
    ``send``) and that then stands ``status`` through its own page, and an operator without
    finalize_documents (operators never hold automate_billing or automate_sending)."""
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01", activate=status != "draft")
    r = await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=6, auto_send=send)
    assert r.status_code == 200, r.text
    if status == "paused":
        await tick(session, cid, "2026-01-01")
        assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    await revoke(client, hd, "finalize_documents", "operator")
    operator, _ = await member(client, session, hd, "operator")
    return hd, cid, eid, operator


def _refused(r, permission: str = "automate_billing") -> None:
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["params"]["label"]["message_key"] == f"perm.name.{permission}", r.text


async def _user(client, hd, role: str) -> dict:
    users = (await client.get("/companies/me/users", headers=hd)).json()["items"]
    return next(u for u in users if u["role"] == role)


def _uid(user: dict) -> uuid.UUID:
    return uuid.UUID(str(user.get("id") or user.get("user_id")))


async def _owner_name(client, hd) -> str:
    return (await _user(client, hd, "owner"))["name"]


async def _resume(client, session, monkeypatch):
    """Resuming brings the chosen modes back into force, so it asks whoever presses it for their
    permissions: a role that may not bill is refused and nothing changes. The owner resumes; the
    consent stays, billing catches up under it, and the answer names what now runs and whose
    authorization it runs under."""
    hd, cid, eid, operator = await _configured(client, session, monkeypatch, status="paused", send=True)
    before = (await template(client, hd, eid))["consents"]
    freeze_day(monkeypatch, "2026-03-15")
    _refused(await client.post(f"/subscriptions/{eid}/resume", headers=operator))
    assert (await template(client, hd, eid))["status"] == "paused"
    r = await client.post(f"/subscriptions/{eid}/resume", headers=hd)
    assert r.status_code == 200, r.text
    after = await template(client, hd, eid)
    assert after["status"] == "active" and after["consents"] == before
    name = await _owner_name(client, hd)
    assert r.json()["runs"] == ["generation", "sending"] and r.json()["lapsed"] == []
    assert name in r.json()["message"] and "email" in r.json()["message"], r.json()
    await tick(session, cid, "2026-03-15")
    assert len(await billed_docs(session, eid)) == 3


async def _activate(client, session, monkeypatch):
    hd, cid, eid, operator = await _configured(client, session, monkeypatch, status="draft")
    before = (await template(client, hd, eid))["consents"]
    freeze_day(monkeypatch, "2026-03-15")
    _refused(await client.post(f"/subscriptions/{eid}/activate", headers=operator))
    assert (await template(client, hd, eid))["status"] == "draft"
    r = await client.post(f"/subscriptions/{eid}/activate", headers=hd)
    assert r.status_code == 200, r.text
    after = await template(client, hd, eid)
    assert after["status"] == "active" and after["consents"] == before
    assert r.json()["runs"] == ["generation"] and await _owner_name(client, hd) in r.json()["message"], r.json()
    await tick(session, cid, "2026-03-15")
    assert len(await billed_docs(session, eid)) == 3


async def _change_schedule(client, session, monkeypatch):
    hd, cid, eid, operator = await _configured(client, session, monkeypatch)
    _refused(await schedule(client, operator, eid, catchup_periods=12))
    assert (await template(client, hd, eid))["catchup_periods"] == 6


async def _stops(client, session, monkeypatch, route: str, status: str):
    """Pausing or cancelling only stops billing, so any role that may edit does it; the consent
    stays with whoever set the terms, and nothing bills while the agreement is not active."""
    hd, cid, eid, operator = await _configured(client, session, monkeypatch)
    await tick(session, cid, "2026-01-01")
    before = (await template(client, hd, eid))["consents"]
    assert (await client.post(f"/subscriptions/{eid}/{route}", headers=operator)).status_code == 200
    after = await template(client, hd, eid)
    assert after["status"] == status and after["consents"] == before, after
    freeze_day(monkeypatch, "2026-03-15")
    await tick(session, cid, "2026-03-15")
    assert len(await billed_docs(session, eid)) == 1


async def _pause(client, session, monkeypatch):
    await _stops(client, session, monkeypatch, "pause", "paused")


async def _cancel(client, session, monkeypatch):
    await _stops(client, session, monkeypatch, "cancel", "cancelled")


# One case per census route door, by route function.
DOORS = {
    "resume_subscription": _resume,
    "activate_subscription": _activate,
    "change_schedule": _change_schedule,
    "pause_subscription": _pause,
    "cancel_subscription": _cancel,
}


def test_the_census_reads_every_route_door():
    doors = route_doors()
    assert {name: u for name, (_, _, u) in doors.items() if u} == {}
    assert {"resume_subscription", "activate_subscription", "change_schedule"} <= set(doors)


@pytest.mark.parametrize("door", sorted(route_doors()))
async def test_every_door_that_writes_status_or_a_consent_term_asks_for_consent(client, session, monkeypatch, door):
    await DOORS[door](client, session, monkeypatch)


def test_no_agreement_write_outside_write_agreement_touches_a_mode():
    assert {name: keys for name, keys in outside_writes().items() if keys} == {}


@pytest.mark.parametrize("status", ["active", "paused", "draft"])
def test_a_moved_template_runs_no_mode(status):
    from celerp_subscriptions import billing, migration

    state = {"status": status, "frequency": "monthly", "start_date": "2026-01-01", "next_run_date": "2026-03-01",
             "generation_mode": "auto_invoice", "auto_send": True}
    assert billing.modes_in_force({**state, **migration.legacy_schedule(state)}) == []


def test_only_write_agreement_marks_an_agreement_write():
    """The status guard lets an agreement's status change only inside write_agreement."""
    assert marker_writers() == {"default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::write_agreement"}


def test_the_status_guard_watches_every_documents_status_route():
    from celerp.modules.slots import get as get_slot

    assert "celerp_subscriptions.billing:guard_agreement_status" in {c["handler"] for c in get_slot("doc_status_guard")}
    assert "POST /{entity_id}/unvoid (unvoid_doc)" in status_routes()


async def test_a_documents_route_cannot_bring_an_agreement_back_into_force(client, session, monkeypatch):
    """Void then unvoid would restore an automatic agreement to active outside its schedule."""
    hd, cid, eid, _ = await _configured(client, session, monkeypatch)
    r = await client.post(f"/docs/{eid}/void", headers=hd, json={"reason": "x", "idempotency_key": str(uuid.uuid4())})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "subscriptions.err_agreement_status_elsewhere", r.text
    assert (await template(client, hd, eid))["status"] == "active"


async def test_an_agreement_is_not_imported_billing_on_its_own(client):
    """An import that claims automatic billing, with a consent it names, is refused whole."""
    hd = await register(client)
    eid = f"doc:{uuid.uuid4()}"
    record = {"entity_id": eid, "event_type": "doc.created", "source": "subscription",
              "idempotency_key": uuid.uuid4().hex,
              "data": {"doc_type": "subscription_invoice", "contact_id": "contact:test-001", "status": "active",
                       "frequency": "monthly", "start_date": "2026-03-01", "generation_mode": "auto_invoice",
                       "consents": {"generation": {"by": str(uuid.uuid4()), "at": "2026-03-01T00:00:00Z"}}}}
    r = await client.post("/docs/import", json=record, headers=hd)
    assert r.status_code in (409, 422), r.text
    assert (await client.get(f"/docs/{eid}", headers=hd)).status_code == 404


async def test_a_manual_agreement_keeps_its_documents_actions(client, session, monkeypatch):
    """Only an agreement that runs a mode is held to its own page: a manual one voids as before."""
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-01-01")
    r = await client.post(f"/docs/{eid}/void", headers=hd, json={"reason": "x", "idempotency_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    assert (await template(client, hd, eid))["status"] == "void"


# The finding's door: terms set on an agreement that is not running, by a role without the permission.
_SET = {"generation_mode": ({"generation_mode": "auto_invoice"}, "automate_billing"),
        "auto_send": ({"auto_send": True}, "automate_sending"),
        "catchup_periods": ({"catchup_periods": 12}, "automate_billing")}


@pytest.mark.parametrize("field", sorted(_SET))
@pytest.mark.parametrize("status", ["draft", "paused", "active"])
async def test_a_role_without_the_permission_cannot_set_automatic_terms_in_any_status(
        client, session, monkeypatch, status, field):
    hd, cid, eid, operator = await _configured(client, session, monkeypatch, status=status)
    if field == "generation_mode":
        assert (await schedule(client, hd, eid, generation_mode="manual_due")).status_code == 200
    before = await template(client, hd, eid)
    change, permission = _SET[field]
    _refused(await schedule(client, operator, eid, **change), permission)
    after = await template(client, hd, eid)
    assert {f: after.get(f) for f in automatic_fields()} == {f: before.get(f) for f in automatic_fields()}
    assert after.get("consents") == before.get("consents")


async def _write(session, cid, eid, change: dict, actor) -> None:
    from celerp_subscriptions import billing

    await billing.locked_company(session, uuid.UUID(cid))
    await billing.write_agreement(session, uuid.UUID(cid), eid, change, actor)


# A value each automatic field can be given that differs from the one _configured leaves (and,
# for a mode switch, chooses the mode). A new automatic field fails until it has one.
_NEW = {"anchor_date": "2026-01-05", "frequency": "weekly", "custom_interval_days": 10, "billing_timing": "arrears",
        "term_start": "2025-12-01", "term_end": "2027-07-01", "cancel_at_period_end": 0, "catchup_periods": 3,
        "generation_mode": "auto_invoice", "auto_send": True}
# The terms a field is set from: a mode switch is set from the mode off.
_FROM = {"generation_mode": {"generation_mode": "manual_due", "auto_send": False}, "auto_send": {"auto_send": False}}


@pytest.mark.parametrize("field", sorted(automatic_fields()))
@pytest.mark.parametrize("status", sorted(AGREEMENT_STATUSES))
async def test_every_automatic_field_in_every_status_is_the_writers_consent(client, session, monkeypatch, status, field):
    """Class guard at the owning boundary, generated over every agreement status and every field
    a consent covers: a writer without the permission (an operator, or no member at all) is
    refused and nothing changes; a manager's write records the manager's consent."""
    hd, cid, eid, _ = await _configured(client, session, monkeypatch, send=True)
    owner = _uid(await _user(client, hd, "owner"))
    if field in _FROM:
        await _write(session, cid, eid, _FROM[field], owner)
    await _write(session, cid, eid, {"status": status}, owner)
    await session.commit()
    before = await template(client, hd, eid)
    for actor in (_uid(await _user(client, hd, "operator")), None):
        with pytest.raises(HTTPException) as refused:
            await _write(session, cid, eid, {field: _NEW[field]}, actor)
        assert refused.value.status_code == 403, (actor, refused.value.detail)
    assert await template(client, hd, eid) == before
    _, manager_id = await member(client, session, hd, "manager")
    await _write(session, cid, eid, {field: _NEW[field]}, uuid.UUID(manager_id))
    await session.commit()
    after = await template(client, hd, eid)
    from celerp_subscriptions import billing

    covering = {m for m, terms in billing.CONSENT_TERMS.items() if field in terms} & set(billing.configured_modes(after))
    assert covering and {m: after["consents"][m]["by"] for m in covering} == {m: manager_id for m in covering}
    assert after["status"] == status


async def test_a_status_change_never_moves_consent(client, session, monkeypatch):
    """Status and automatic terms are written apart: a write that would do both is refused, so
    no status route can record anyone's consent."""
    hd, cid, eid, _ = await _configured(client, session, monkeypatch, status="paused")
    _, manager_id = await member(client, session, hd, "manager")
    with pytest.raises(ValueError):
        await _write(session, cid, eid, {"status": "active", "catchup_periods": 3}, uuid.UUID(manager_id))


async def test_resume_says_when_the_authorization_has_lapsed(client, session, monkeypatch):
    """The member who set automatic billing lost the permission while it was paused: Resume
    still resumes, and its answer says automatic billing does not run and whose authorization lapsed."""
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01")
    manager, manager_id = await member(client, session, hd, "manager")
    assert (await schedule(client, manager, eid, generation_mode="auto_invoice")).status_code == 200
    await tick(session, cid, "2026-01-01")
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    await revoke(client, hd, "automate_billing", "manager")
    freeze_day(monkeypatch, "2026-03-15")
    r = await client.post(f"/subscriptions/{eid}/resume", headers=hd)
    assert r.status_code == 200, r.text
    assert r.json()["runs"] == [] and r.json()["lapsed"] == ["generation"], r.json()
    assert (await template(client, hd, eid))["consents"]["generation"]["by"] == manager_id
    await tick(session, cid, "2026-03-15")
    assert len(await billed_docs(session, eid)) == 1


async def test_resume_of_a_manual_agreement_says_it_is_billed_by_hand(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    r = await client.post(f"/subscriptions/{eid}/resume", headers=hd)
    assert r.status_code == 200, r.text
    assert r.json()["runs"] == [] and r.json()["lapsed"] == [] and r.json()["message"], r.json()


def test_consent_is_written_only_where_every_automatic_field_is():
    """The consent writers are the automatic-field writers' one sink: only write_agreement writes
    consents, every module function writing an automatic field reaches it (census, nothing
    unresolved, nothing outside it), and no Documents door takes an automatic field."""
    assert consent_writers() == {WRITE_AGREEMENT}
    writers = automatic_writers()
    assert writers and {name: u for name, (_, u) in writers.items() if u} == {}
    assert {name: keys for name, keys in outside_writes().items() if set(keys) & automatic_fields()} == {}
    assert documents_gaps() == {}


def test_every_agreement_write_asks_the_one_rule():
    """Census of every write_agreement call: each one's keys are read from the code and each is
    reached from a route or a manifest handler, and no agreement write in the module goes around
    it, so the rule below (write_agreement asks the writer for every mode a write brings into
    force) is asked at every door."""
    rows = agreement_writes()
    assert rows and {where: unresolved for where, _, _, unresolved, _ in rows if unresolved} == {}
    assert {where: fn for where, fn, _, _, entries in rows if not entries} == {}
    assert outside_writes() == {}


async def _standing(client, session, monkeypatch, status: str):
    """An agreement whose automatic terms (billing and sending) the owner set, standing ``status``
    through its own page, with the owner's and an operator's ids."""
    hd, cid, eid, operator = await _configured(client, session, monkeypatch, send=True)
    owner = _uid(await _user(client, hd, "owner"))
    await _write(session, cid, eid, {"status": status}, owner)
    await session.commit()
    return hd, cid, eid, operator, owner


def _running(state: dict) -> set[str]:
    from celerp_subscriptions import billing

    return set(billing.modes_in_force(state))


@pytest.mark.parametrize("status", sorted(AGREEMENT_STATUSES))
@pytest.mark.parametrize("door", STATUS_DOORS)
async def test_no_status_door_brings_a_mode_into_force_for_a_role_without_its_permissions(
        client, session, monkeypatch, door, status):
    """Generated over every route that writes a status (census) and every status it can start
    from: a role that may neither bill nor automate never leaves more running than before."""
    hd, cid, eid, operator, _ = await _standing(client, session, monkeypatch, status)
    before = await template(client, hd, eid)
    method, path = route_doors()[door][0].split(" ", 1)
    r = await client.request(method, "/subscriptions" + path.format(entity_id=eid), headers=operator, json={})
    after = await template(client, hd, eid)
    assert not _running(after) - _running(before), (door, status, r.status_code, r.text)
    assert after.get("consents") == before.get("consents")


@pytest.mark.parametrize(("was", "to"), [(was, to) for was in sorted(AGREEMENT_STATUSES - {"void"})
                                         for to in sorted(AGREEMENT_STATUSES - {was})])
async def test_a_status_write_asks_the_writer_for_every_mode_it_brings_into_force(
        client, session, monkeypatch, was, to):
    """Class guard at write_agreement, generated over every pair of statuses: a status write that
    makes an agreement with automatic terms bill (it becomes active) is refused to a writer
    without the permissions (an operator, or no member) and changes nothing; the owner makes it,
    and the consent stays with whoever set the terms. Every other status write is anyone's, from
    any status but void (test_no_status_write_moves_an_agreement_out_of_void)."""
    hd, cid, eid, operator, owner = await _standing(client, session, monkeypatch, was)
    operator_id = _uid(await _user(client, hd, "operator"))
    before = await template(client, hd, eid)
    if to == "active":
        for actor in (operator_id, None):
            with pytest.raises(HTTPException) as refused:
                await _write(session, cid, eid, {"status": to}, actor)
            assert refused.value.status_code == 403, (was, actor, refused.value.detail)
        assert await template(client, hd, eid) == before
        await _write(session, cid, eid, {"status": to}, owner)
    else:
        await _write(session, cid, eid, {"status": to}, operator_id)
    await session.commit()
    after = await template(client, hd, eid)
    assert after["status"] == to and after["consents"] == before["consents"], (was, to)


@pytest.mark.parametrize("door", STATUS_DOORS)
async def test_no_status_door_moves_a_void_agreement(client, session, monkeypatch, door):
    """Generated over every route that writes a status (census): pressed by the owner, who holds
    every permission, on a void agreement, each answers 409 and the agreement stays void exactly
    as it was, so Documents unvoid remains the one way out of void."""
    hd, cid, eid, _, owner = await _standing(client, session, monkeypatch, "void")
    before = await template(client, hd, eid)
    method, path = route_doors()[door][0].split(" ", 1)
    r = await client.request(method, "/subscriptions" + path.format(entity_id=eid), headers=hd, json={})
    assert r.status_code == 409, (door, r.status_code, r.text)
    assert await template(client, hd, eid) == before, door


@pytest.mark.parametrize("to", sorted(AGREEMENT_STATUSES - {"void"}))
async def test_no_status_write_moves_an_agreement_out_of_void(client, session, monkeypatch, to):
    """Class guard at write_agreement, generated over every status: a status write on a void
    agreement is refused (409 naming unvoid) whoever makes it, and changes nothing."""
    hd, cid, eid, _, owner = await _standing(client, session, monkeypatch, "void")
    before = await template(client, hd, eid)
    for actor in (owner, _uid(await _user(client, hd, "operator")), None):
        with pytest.raises(HTTPException) as refused:
            await _write(session, cid, eid, {"status": to}, actor)
        assert refused.value.status_code == 409, (to, actor, refused.value.detail)
        assert refused.value.detail.key == "subscriptions.err_status_void", refused.value.detail
    assert await template(client, hd, eid) == before


@pytest.mark.parametrize("module_on", [True, False])
async def test_the_status_guard_asks_the_actor_for_every_mode_a_change_brings_into_force(
        client, session, monkeypatch, module_on):
    """Every status change outside write_agreement (each Documents status route, an import) goes
    through the status guard (test_doc_status_guard_door). Generated over every pair of statuses,
    with the module on or off: a change that makes an agreement with automatic terms bill never
    passes for an actor without the modes' permissions; with the module off, the owner's does."""
    from types import SimpleNamespace

    from celerp_subscriptions import billing
    from test_module_off_census import _turn_off

    hd, cid, eid, _ = await _configured(client, session, monkeypatch, send=True)
    if not module_on:
        await _turn_off(client, hd, monkeypatch)
    state = await template(client, hd, eid)
    owner, operator = _uid(await _user(client, hd, "owner")), _uid(await _user(client, hd, "operator"))
    for was in sorted(AGREEMENT_STATUSES):
        for to in sorted(AGREEMENT_STATUSES - {was}):
            transition = SimpleNamespace(before={**state, "status": was}, after={**state, "status": to})
            answers = {}
            for name, actor in (("operator", operator), ("none", None), ("owner", owner)):
                entry = SimpleNamespace(entity_id=eid, company_id=uuid.UUID(cid), actor_id=actor)
                try:
                    await billing.guard_agreement_status(session=session, entry=entry, transition=transition)
                    answers[name] = 200
                except HTTPException as exc:
                    answers[name] = exc.status_code
            if to == "active":
                assert answers["operator"] != 200 and answers["none"] != 200, (was, to, answers)
                if was == "void" or (not module_on and was in ("paused", "cancelled")):
                    assert answers == {"operator": 403, "none": 403, "owner": 200}, (was, to, answers)


async def test_documents_unvoid_with_the_module_off_asks_for_the_modes_permissions(client, session, monkeypatch):
    """With the module off, an agreement that billed on its own is voided. A role that may unvoid
    (finalize_documents) but may not turn automatic billing on cannot bring it back into force,
    so turning the module on again cannot bill under the old consent; the owner can."""
    from test_module_off_census import _turn_off

    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=6)).status_code == 200
    await tick(session, cid, "2026-01-01")
    await _turn_off(client, hd, monkeypatch)
    void = await client.post(f"/docs/{eid}/void", headers=hd, json={"reason": "stop it"})
    assert void.status_code == 200, void.text
    manager, _ = await member(client, session, hd, "manager")
    await revoke(client, hd, "automate_billing", "manager")
    r = await client.post(f"/docs/{eid}/unvoid", headers=manager, json={})
    _refused(r)
    assert (await template(client, hd, eid))["status"] == "void"
    assert (await client.post(f"/docs/{eid}/unvoid", headers=hd, json={})).status_code == 200
    assert (await template(client, hd, eid))["status"] == "active"


async def test_unvoid_with_the_module_on_again_asks_for_the_modes_permissions_only(client, session, monkeypatch):
    """Voided in Documents while the module was off, an automatic agreement is unvoided once the
    module is on again. Unvoid is the only way out of void (its own page writes no status on a
    void agreement), so it is not sent back to that page: it asks only for the modes' permissions."""
    from test_module_off_census import MODULE, _turn_off

    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=6)).status_code == 200
    await tick(session, cid, "2026-01-01")
    await _turn_off(client, hd, monkeypatch)
    void = await client.post(f"/docs/{eid}/void", headers=hd, json={"reason": "stop it"})
    assert void.status_code == 200, void.text
    assert (await client.post(f"/companies/me/modules/{MODULE}/enable", headers=hd)).status_code == 200
    monkeypatch.undo()
    freeze_day(monkeypatch, "2026-01-01")
    manager, _ = await member(client, session, hd, "manager")
    await revoke(client, hd, "automate_billing", "manager")
    _refused(await client.post(f"/docs/{eid}/unvoid", headers=manager, json={}))
    assert (await template(client, hd, eid))["status"] == "void"
    r = await client.post(f"/docs/{eid}/unvoid", headers=hd, json={})
    assert r.status_code == 200, r.text
    assert (await template(client, hd, eid))["status"] == "active"
