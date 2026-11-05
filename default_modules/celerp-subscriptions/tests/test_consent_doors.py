# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every door through which automatic billing can come into force asks for its consent.

A mode comes into force where an agreement becomes active with it chosen, or where a consent
term of a running mode changes, so the check sits in the one writer of agreement fields
(billing.write_agreement) and the status guard keeps every other writer from changing the
status of an agreement that runs a mode before or after the change. The doors are read from the code (consent_census): each subscriptions
route that writes status or a consent term is driven here by a role that may neither bill nor
automate, so a new door fails until it is given a case below."""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import (
    agreement, billed_docs, company_id, freeze_day, member, register, revoke, schedule, template, tick,
)
from consent_census import marker_writers, outside_writes, route_doors, status_routes


async def _automatic(client, session, monkeypatch, *, activate: bool = True):
    """An agreement from 2026-01-01 that the owner set to bill automatically (window 6), and
    an operator without finalize_documents (operators never hold automate_billing)."""
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01", activate=activate)
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=6)).status_code == 200
    await revoke(client, hd, "finalize_documents", "operator")
    operator, _ = await member(client, session, hd, "operator")
    return hd, cid, eid, operator


def _refused_for_automation(r) -> None:
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["params"]["label"]["message_key"] == "perm.name.automate_billing", r.text


async def _resume(client, session, monkeypatch):
    hd, cid, eid, operator = await _automatic(client, session, monkeypatch)
    await tick(session, cid, "2026-01-01")
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    freeze_day(monkeypatch, "2026-07-15")
    billed = len(await billed_docs(session, eid))
    _refused_for_automation(await client.post(f"/subscriptions/{eid}/resume", headers=operator))
    assert (await template(client, hd, eid))["status"] == "paused"
    await tick(session, cid, "2026-07-15")
    assert len(await billed_docs(session, eid)) == billed


async def _activate(client, session, monkeypatch):
    hd, cid, eid, operator = await _automatic(client, session, monkeypatch, activate=False)
    freeze_day(monkeypatch, "2026-07-15")
    _refused_for_automation(await client.post(f"/subscriptions/{eid}/activate", headers=operator))
    assert (await template(client, hd, eid))["status"] == "draft"
    await tick(session, cid, "2026-07-15")
    assert await billed_docs(session, eid) == []


async def _change_schedule(client, session, monkeypatch):
    hd, cid, eid, operator = await _automatic(client, session, monkeypatch)
    freeze_day(monkeypatch, "2026-07-15")
    _refused_for_automation(await schedule(client, operator, eid, catchup_periods=12))
    assert (await template(client, hd, eid))["catchup_periods"] == 6


async def _stops(client, session, monkeypatch, route: str, status: str):
    """Pausing or cancelling only stops a mode, so any role that may edit does it, and the
    consent goes with the mode: nothing bills unattended until someone consents again."""
    hd, cid, eid, operator = await _automatic(client, session, monkeypatch)
    await tick(session, cid, "2026-01-01")
    assert (await client.post(f"/subscriptions/{eid}/{route}", headers=operator)).status_code == 200
    after = await template(client, hd, eid)
    assert after["status"] == status and not after.get("consents"), after
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
# Agreement writes in the module that bypass write_agreement, and why none brings a mode into force.
NOT_A_DOOR = {
    "_migrate_template": "moves an earlier release's template: one it ran bills by hand from then on, "
                         "and a draft or paused one runs nothing",
}


def test_the_census_reads_every_route_door():
    doors = route_doors()
    assert {name: u for name, (_, _, u) in doors.items() if u} == {}
    assert {"resume_subscription", "activate_subscription", "change_schedule"} <= set(doors)


@pytest.mark.parametrize("door", sorted(route_doors()))
async def test_every_door_that_writes_status_or_a_consent_term_asks_for_consent(client, session, monkeypatch, door):
    await DOORS[door](client, session, monkeypatch)


def test_every_write_outside_write_agreement_is_named_with_why_it_runs_nothing():
    assert {name for name, keys in outside_writes().items() if keys} == set(NOT_A_DOOR)


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
    hd, cid, eid, _ = await _automatic(client, session, monkeypatch)
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


async def test_whoever_brings_billing_back_consents_to_it(client, session, monkeypatch):
    """Resume by a role that holds both permissions brings billing back under its own consent."""
    hd, cid, eid, _ = await _automatic(client, session, monkeypatch)
    await tick(session, cid, "2026-01-01")
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    manager, manager_id = await member(client, session, hd, "manager")
    freeze_day(monkeypatch, "2026-03-15")
    assert (await client.post(f"/subscriptions/{eid}/resume", headers=manager)).status_code == 200
    assert (await template(client, hd, eid))["consents"]["generation"]["by"] == manager_id
    await tick(session, cid, "2026-03-15")
    assert len(await billed_docs(session, eid)) == 3
