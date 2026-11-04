# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Consent to unattended billing and sending: who gave it, what it covers, and what the page says.

An agreement keeps a consent only for a mode that runs, so turning a mode off undoes its
consent and the page stops saying it is authorized. Changing the terms of a running mode
(the catch-up window is part of automatic billing) is a new consent: it needs the mode's
permission and is recorded under the person who made it."""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from agreement_kit import (
    agreement, billed_docs, company_id, freeze_day, member, register, revoke, schedule, template, tick,
)
from ui.routes.subscriptions import _billing_section

pytestmark = pytest.mark.asyncio

AUTHORIZED = {"generation": "Automatic billing authorized", "sending": "Automatic sending authorized"}


def _shown(eid: str, sub: dict) -> set[str]:
    html = to_xml(_billing_section(eid, sub))
    return {mode for mode, text in AUTHORIZED.items() if text in html}


async def test_a_role_without_the_automation_permission_cannot_widen_the_catch_up_window(
        client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    freeze_day(monkeypatch, "2026-07-15")
    await revoke(client, hd, "finalize_documents", "operator")
    op, _ = await member(client, session, hd, "operator")
    widen = await schedule(client, op, eid, catchup_periods=12)
    assert widen.status_code == 403, widen.text
    assert widen.json()["detail"]["params"]["label"]["message_key"] == "perm.name.automate_billing"
    assert (await template(client, hd, eid))["catchup_periods"] == 0
    await tick(session, cid, "2026-07-15")
    assert await billed_docs(session, eid) == []


@pytest.mark.parametrize("terms", [{"catchup_periods": 3}, {"frequency": "weekly"}])
async def test_changing_the_terms_of_running_billing_is_a_new_consent(client, session, monkeypatch, terms):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=1)).status_code == 200
    operator, _ = await member(client, session, hd, "operator")
    manager, manager_id = await member(client, session, hd, "manager")
    if "catchup_periods" in terms:
        assert (await schedule(client, operator, eid, **terms)).status_code == 403
    assert (await schedule(client, manager, eid, **terms)).status_code == 200
    assert (await template(client, hd, eid))["consents"]["generation"]["by"] == manager_id


async def test_the_catch_up_window_of_a_manual_agreement_is_set_without_consent(client, session, monkeypatch):
    """The window bounds automatic billing only once someone turns it on, and that person's
    consent covers the window as it then stands."""
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    operator, _ = await member(client, session, hd, "operator")
    assert (await schedule(client, operator, eid, catchup_periods=4)).status_code == 200
    state = await template(client, hd, eid)
    assert state["catchup_periods"] == 4 and not state.get("consents")


async def test_turning_a_mode_off_undoes_its_consent_and_the_page_says_so(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    manager, manager_id = await member(client, session, hd, "manager")

    async def step(**change) -> dict:
        r = await schedule(client, manager, eid, **change)
        assert r.status_code == 200, r.text
        return await template(client, hd, eid)

    sub = await step(generation_mode="auto_invoice", auto_send=True)
    assert set(sub["consents"]) == _shown(eid, sub) == {"generation", "sending"}
    sub = await step(generation_mode="manual_due")
    assert not sub.get("consents") and _shown(eid, sub) == set()
    assert sub["auto_send"] is True  # the setting stays; nothing sends while billing is by hand
    sub = await step(generation_mode="auto_invoice")
    assert {m: c["by"] for m, c in sub["consents"].items()} == {"generation": manager_id, "sending": manager_id}
    sub = await step(auto_send=False)
    assert set(sub["consents"]) == _shown(eid, sub) == {"generation"}


async def test_turning_on_billing_that_would_send_needs_the_sending_permission(client, session, monkeypatch):
    """Send automatically set on a manual agreement starts sending once billing turns automatic,
    so the person who turns billing on must also be allowed to send."""
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    assert (await schedule(client, hd, eid, auto_send=True)).status_code == 200
    await revoke(client, hd, "automate_sending", "manager")
    manager, _ = await member(client, session, hd, "manager")
    r = await schedule(client, manager, eid, generation_mode="auto_invoice")
    assert r.status_code == 403 and r.json()["detail"]["params"]["label"]["message_key"] == "perm.name.automate_sending"
    assert (await template(client, hd, eid)).get("generation_mode", "manual_due") == "manual_due"


@pytest.mark.parametrize("role", ["owner", "admin", "manager"])
async def test_collection_is_not_a_billing_mode(client, session, monkeypatch, role):
    """Automatic collection is not offered in this release: every role gets the same refusal
    as for any unknown mode, and the page offers only billing by hand or automatically."""
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-04-01")
    who = hd if role == "owner" else (await member(client, session, hd, role))[0]
    r = await schedule(client, who, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 422 and "Unknown generation mode" in r.text
    html = to_xml(_billing_section(eid, await template(client, hd, eid)))
    assert "collect" not in html.lower()


# One value for each schedule setting that differs from what a running automatic agreement starting
# 2026-07-01 holds; every ScheduleBody field is listed, so a new setting has to be classified here.
_CHANGED_TERM = {"anchor_date": "2026-07-05", "frequency": "weekly", "custom_interval_days": 10,
                 "billing_timing": "arrears", "term_start": "2026-07-01", "term_end": "2027-07-01",
                 "cancel_at_period_end": True, "catchup_periods": 3, "generation_mode": "auto_invoice",
                 "auto_send": True}


async def test_every_setting_unattended_billing_reads_is_covered_by_its_consent():
    """Class guard: the consent to a mode covers every setting the mode's own decision reads.
    The decision is read through a state that records each field asked for, so a setting a
    later change makes it read, and that the consent does not cover, fails here."""
    from datetime import date
    from celerp_subscriptions import billing
    from celerp_subscriptions.routes import ScheduleBody

    class Recorded(dict):
        read: set = set()

        def get(self, key, default=None):
            self.read.add(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.read.add(key)
            return super().__getitem__(key)

        def __contains__(self, key):
            self.read.add(key)
            return super().__contains__(key)

    assert set(_CHANGED_TERM) == set(ScheduleBody.model_fields)
    settings = set(ScheduleBody.model_fields)
    for frequency, timing in (("monthly", "advance"), ("custom", "arrears")):
        state = Recorded({"status": "active", "anchor_date": "2026-01-01", "frequency": frequency,
                          "custom_interval_days": 10, "billing_timing": timing, "term_start": "2026-01-01",
                          "term_end": "2027-01-01", "cancel_at_period_end": 30, "generation_mode": "auto_invoice",
                          "catchup_periods": 1, "auto_send": True, "periods": {}})
        Recorded.read = set()
        found = billing.unattended_due(state, date(2026, 6, 1))
        assert found
        billing.outside_window(state, *found, date(2026, 6, 1))
        assert Recorded.read & settings <= set(billing.CONSENT_TERMS["generation"]), Recorded.read
        Recorded.read = set()
        billing.waiting_sends(state, date(2026, 6, 1))
        assert billing.sends_itself(state)
        assert Recorded.read & settings <= {t for m in billing.CONSENT_TERMS.values() for t in m}, Recorded.read


@pytest.mark.parametrize("field", sorted(_CHANGED_TERM))
async def test_a_role_without_the_permission_cannot_change_a_consented_setting(client, session, monkeypatch, field):
    """Every schedule setting of a running automatic agreement is covered by its consent, so a
    role that may not run the mode is refused it, before the first period is recorded too, and
    the setting stays as it was."""
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-07-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    operator, _ = await member(client, session, hd, "operator")
    before = await template(client, hd, eid)
    r = await schedule(client, operator, eid, **{field: _CHANGED_TERM[field]})
    # The one automatic mode chosen again changes nothing, so it needs no consent.
    assert r.status_code == (200 if before.get(field) == _CHANGED_TERM[field] else 403), (field, r.text)
    after = await template(client, hd, eid)
    assert after.get(field) == before.get(field) and after["consents"] == before["consents"]


async def test_an_operator_cannot_make_running_billing_weekly(client, session, monkeypatch):
    """Billing consented as monthly does not bill weekly because a role that may neither bill
    nor automate changed the frequency before the first bill."""
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-07-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    await revoke(client, hd, "finalize_documents", "operator")
    operator, _ = await member(client, session, hd, "operator")
    weekly = await schedule(client, operator, eid, frequency="weekly")
    assert weekly.status_code == 403, weekly.text
    for day in ("2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22", "2026-07-29"):
        freeze_day(monkeypatch, day)
        await tick(session, cid, day)
    assert len(await billed_docs(session, eid)) == 1
