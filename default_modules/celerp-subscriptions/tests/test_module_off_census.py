# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A company that turned the subscriptions module off runs no unattended mode, and nothing the
module adds app-wide keeps it from stopping what billed on its own (P2e-3).

The doors come from the code (module_off_census): every handler or render the manifest
registers and every step the billing pass queues after it commits. Each has a probe that turns
the module off and shows what that door does, or a written reason it cannot act for such a
company. A new door with neither fails the census test."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import module_off_census  # noqa: E402
from agreement_kit import (  # noqa: E402
    agreement, billed_docs, company_id, contact, freeze_day, register, schedule, template, tick,
)

MODULE = "celerp-subscriptions"


async def _turn_off(client, hd, monkeypatch) -> None:
    """The owner turns the module off on Settings > Modules (the module's routes unload)."""
    from celerp.main import app
    from celerp.modules import loader
    from starlette.routing import Match

    scope = {"type": "http", "method": "POST", "path": "/subscriptions/doc:x/generate-due", "root_path": "",
             "headers": [], "query_string": b""}
    monkeypatch.setitem(loader._module_routes, MODULE,
                        [r for r in app.router.routes if r.matches(scope)[0] is Match.FULL])
    r = await client.post(f"/companies/me/modules/{MODULE}/disable", headers=hd)
    assert r.status_code == 200, r.text


async def _void_while_off(client, session, monkeypatch):
    """An agreement bills on its own; with the module off, Documents can still void it."""
    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=6)).status_code == 200
    await tick(session, cid, "2026-01-01")
    await _turn_off(client, hd, monkeypatch)
    freeze_day(monkeypatch, "2026-04-15")
    void = await client.post(f"/docs/{eid}/void", headers=hd, json={"reason": "stop it"})
    assert void.status_code == 200, void.text
    assert (await template(client, hd, eid))["status"] == "void"


async def _pass_while_off(client, session, monkeypatch):
    """With the module off, the dispatcher's pass bills nothing, queues no email and tells the
    company nothing, even for an agreement held for the catch-up page."""
    from sqlalchemy import func, select
    from celerp.models.notification import Notification

    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-01-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    await _turn_off(client, hd, monkeypatch)
    before = await session.scalar(select(func.count()).select_from(Notification))
    await tick(session, cid, "2026-04-15")
    assert await billed_docs(session, eid) == []
    assert await session.scalar(select(func.count()).select_from(Notification)) == before


async def _email_while_off(client, session, monkeypatch):
    """A billed period's email that waits for the next pass is not sent once the module is off."""
    import uuid
    from datetime import date
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    eid = await agreement(client, hd, start="2026-07-01", contact_id=await contact(client, hd, "member@example.test"))
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", auto_send=True)).status_code == 200
    report = await billing.run_due(session=session, company_id=cid, today=date(2026, 7, 1), budget=5)
    assert report.done == 1
    await session.commit()
    await _turn_off(client, hd, monkeypatch)
    sent = await billing.deliver(lambda: billing.same_session(session), cid, eid, 0)
    assert sent == {"state": "pending"}
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "pending"


# Every door of the census: a probe, or why it cannot act for a company that turned the module off.
DOORS = {
    ("doc_status_guard", "celerp_subscriptions.billing:guard_agreement_status"): _void_while_off,
    ("periodic_operation", "celerp_subscriptions.billing:run_due"): _pass_while_off,
    ("billing.run_due", "billing._deliverer"): _email_while_off,
    ("billing.run_due", "billing._expire_attempts_for"):
        "queued only by run_due, which the dispatcher never calls for a company that turned the module off "
        "(celerp_docs.periodic._run_operation); it deletes old send attempts and sends nothing",
    ("doc_status_guard", "celerp_subscriptions.billing:guard_bill_status"):
        "refuses only a bill whose period was let go becoming billing again, never a stop: kept while the "
        "module is off so turning it back on cannot bill a period twice",
    ("on_modules_ready", "celerp_subscriptions.migration:migrate_schedules_hook"):
        "moves stored schedules to the current shape at startup; it bills and sends nothing",
    ("on_company_restored", "celerp_subscriptions.migration:reconcile_restored_company_schedules"):
        "fired only for the modules the restored company uses (celerp.modules.slots._company_hooks)",
    ("on_company_module_enabled", "celerp_subscriptions.migration:reconcile_company_schedules"):
        "fired when the company turns the module on, never while it is off",
    ("doc_detail_badges", "celerp_subscriptions.ui_routes:doc_period_badge"):
        "shown only for the modules the company uses (ui visible_slot_contributions); it reads, never writes",
    ("search_provider", "celerp_subscriptions.search:global_search"):
        "called only for the modules the company uses (celerp.routers.search); it reads, never writes",
}
_CENSUS = module_off_census.slot_doors() + module_off_census.after_commit_steps()


def test_every_door_has_a_probe_or_a_reason():
    """The census and DOORS name the same doors: a new slot entry or after-commit step fails
    here until it has a module-off probe or a written reason."""
    assert sorted(_CENSUS) == sorted(DOORS)


@pytest.mark.parametrize("door", [d for d in _CENSUS if callable(DOORS.get(d))], ids=lambda d: d[1])
async def test_a_door_does_nothing_for_a_company_that_turned_the_module_off(client, session, monkeypatch, door):
    await DOORS[door](client, session, monkeypatch)
