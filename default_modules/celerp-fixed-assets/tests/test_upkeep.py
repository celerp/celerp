# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Maintenance, warranty and insurance on an asset: the maintenance log, and reminders that
tell their responsible person once when an item comes within its lead time and again on
its due date, then are marked done (the next one scheduled when recurring), postponed with
a reason, edited, removed and undone, every change kept in the item's history.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from celerp_fixed_assets.setup import today_in
from fa_fixtures import FA
from test_files_tags import PDF, _ui
from test_runs import _contributed, _managed_from, _tick

pytestmark = pytest.mark.asyncio

# Today for a company with no business timezone set, as every test company is.
TODAY = today_in(None)


def _on(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


async def _remind(fa: FA, asset_id: str, headers=None, **body):
    return await fa.post(f"/assets/{asset_id}/reminders", body, headers=headers)


async def _reminder(fa: FA, asset_id: str, **body) -> dict:
    return await fa.ok(await _remind(fa, asset_id, **body))


async def _upkeep(fa: FA, asset_id: str) -> dict:
    return await fa.ok(await fa.get(f"/assets/{asset_id}/upkeep"))


async def _addressed(fa: FA) -> list[tuple]:
    """The reminder notices: (addressee, title, body), oldest first."""
    from celerp.models.notification import Notification

    fa.session.expire_all()
    rows = (await fa.session.execute(select(Notification).where(
        Notification.company_id == fa.auth["company_id"], Notification.category == "fixed_assets",
        Notification.title == "Asset reminders").order_by(Notification.created_at))).scalars()
    return [(str(n.user_id) if n.user_id else None, n.title, n.body) for n in rows]


async def _owner(fa: FA) -> str:
    from test_runs import _owner_id

    return await _owner_id(fa)


# ── Reminders ────────────────────────────────────────────────────────────────


async def test_reminders_of_each_kind_with_their_fields(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    service = await _reminder(fa, car, kind="maintenance", work="Oil change", due=_on(40), every_months=6)
    warranty = await _reminder(fa, car, kind="warranty", provider="Dealer", due=_on(200))
    cover = await _reminder(fa, car, kind="insurance", insurer="Mutual", policy="P-77", insured_value="11000",
                            due=_on(300), every_months=12, lead_days=45)
    listed = (await _upkeep(fa, car))["reminders"]
    assert [r["id"] for r in listed] == [service["id"], warranty["id"], cover["id"]], "soonest first"
    got = {r["kind"]: r for r in listed}
    assert (got["maintenance"]["work"], got["maintenance"]["every_months"], got["maintenance"]["lead_days"]) == (
        "Oil change", 6, 30)
    assert (got["warranty"]["provider"], got["warranty"]["every_months"]) == ("Dealer", None)
    assert (got["insurance"]["insurer"], got["insurance"]["policy"], got["insurance"]["insured_value"],
            got["insurance"]["lead_days"]) == ("Mutual", "P-77", "11000.00", 45)
    assert [h["action"] for h in got["warranty"]["history"]] == ["created"]


async def test_reminder_refusals_are_named(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    cases = [({"kind": "service", "due": _on(5)}, "Choose maintenance, warranty or insurance"),
             ({"kind": "maintenance", "due": _on(5)}, "Describe the maintenance work"),
             ({"kind": "warranty", "due": _on(5)}, "Name the warranty provider"),
             ({"kind": "insurance", "due": _on(5)}, "Name the insurer"),
             ({"kind": "warranty", "provider": "Dealer", "due": "soon"}, "Enter the due date"),
             ({"kind": "warranty", "provider": "Dealer", "due": _on(5), "every_months": 0},
              "whole number of months"),
             ({"kind": "warranty", "provider": "Dealer", "due": _on(5), "lead_days": 400}, "between 0 and 365"),
             ({"kind": "insurance", "insurer": "Mutual", "due": _on(5), "insured_value": "lots"},
              "insured value as an amount"),
             ({"kind": "warranty", "provider": "Dealer", "due": _on(5), "responsible_user_id": "nobody"},
              "not a member of this company")]
    for body, message in cases:
        r = await _remind(fa, car, **body)
        assert r.status_code == 422 and message in r.json()["detail"], (body, r.text)
    viewer = await fa.role_headers("viewer")
    r = await _remind(fa, car, headers=viewer, kind="warranty", provider="Dealer", due=_on(5))
    assert r.status_code == 403


async def test_done_schedules_the_next_when_recurring_and_closes_otherwise(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    service = await _reminder(fa, car, kind="maintenance", work="Oil change", due=_on(3), every_months=6)
    done = await fa.ok(await fa.post(f"/reminders/{service['id']}/done", {"date": "2026-10-10"}))
    assert done["due"] == "2027-04-10" and done["status"] == "open"
    assert [(h["action"], h.get("date")) for h in done["history"]][:1] == [("done", "2026-10-10")]
    log = (await _upkeep(fa, car))["maintenance"]
    assert [(m["date"], m["work"], m["next_due"], m["reminder_id"]) for m in log] == [
        ("2026-10-10", "Oil change", "2027-04-10", service["id"])]
    assert log[0]["by"] == "Owner"

    warranty = await _reminder(fa, car, kind="warranty", provider="Dealer", due=_on(3))
    closed = await fa.ok(await fa.post(f"/reminders/{warranty['id']}/done", {"date": _on(0)}))
    assert closed["status"] == "done"
    again = await fa.post(f"/reminders/{warranty['id']}/done", {"date": _on(0)})
    assert again.status_code == 422 and "already done" in again.json()["detail"]
    undated = await fa.post(f"/reminders/{service['id']}/done", {})
    assert undated.status_code == 422 and "date the work was done" in undated.json()["detail"]


async def test_postpone_needs_a_later_date_and_a_reason_and_edits_are_kept(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    cover = await _reminder(fa, car, kind="insurance", insurer="Mutual", due="2026-11-01")
    path = f"/reminders/{cover['id']}"
    r = await fa.post(f"{path}/postpone", {"to": "2026-11-20"})
    assert r.status_code == 422 and "Say why" in r.json()["detail"]
    r = await fa.post(f"{path}/postpone", {"to": "2026-10-01", "reason": "Broker away"})
    assert r.status_code == 422 and "after 2026-11-01" in r.json()["detail"]
    moved = await fa.ok(await fa.post(f"{path}/postpone", {"to": "2026-11-20", "reason": "Broker away"}))
    assert moved["due"] == "2026-11-20"
    edited = await fa.ok(await fa.patch(path, {"changes": {"policy": "P-9", "insured_value": "9000"}}))
    assert (edited["policy"], edited["insured_value"]) == ("P-9", "9000.00")
    bad = await fa.patch(path, {"changes": {"kind": "warranty"}})
    assert bad.status_code == 422 and "kind" in bad.json()["detail"]
    history = edited["history"]
    assert [h["action"] for h in history] == ["edited", "postponed", "created"], "newest first"
    assert (history[1]["reason"], history[1]["before"]["due"], history[1]["after"]["due"]) == (
        "Broker away", "2026-11-01", "2026-11-20")
    assert history[0]["before"] == {"policy": None, "insured_value": None}


async def test_undo_reverts_the_latest_change_and_a_done_maintenance_log(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    service = await _reminder(fa, car, kind="maintenance", work="Tyres", due="2026-11-01", every_months=12)
    path = f"/reminders/{service['id']}"
    await fa.ok(await fa.post(f"{path}/done", {"date": "2026-10-30"}))
    undone = await fa.ok(await fa.post(f"{path}/undo", {}))
    assert (undone["due"], undone["status"]) == ("2026-11-01", "open")
    assert [h["action"] for h in undone["history"]] == ["undone", "done", "created"]
    assert (await _upkeep(fa, car))["maintenance"] == []

    removed = await fa.ok(await fa.post(f"{path}/remove", {}))
    assert removed["status"] == "removed"
    assert [r["id"] for r in (await _upkeep(fa, car))["reminders"] if r["status"] != "removed"] == []
    restored = await fa.ok(await fa.post(f"{path}/undo", {}))
    assert restored["status"] == "open"
    nothing = await fa.post(f"{path}/undo", {})
    assert nothing.status_code == 422 and "Nothing to undo" in nothing.json()["detail"]


async def test_due_soon_list_and_register_card(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    near = await _reminder(fa, car, kind="maintenance", work="Oil change", due=_on(10))
    late = await _reminder(fa, car, kind="warranty", provider="Dealer", due=_on(-2))
    await _reminder(fa, car, kind="insurance", insurer="Mutual", due=_on(60))
    due = (await fa.ok(await fa.get("/reminders")))["items"]
    assert [(r["id"], r["overdue"]) for r in due] == [(late["id"], True), (near["id"], False)]
    assert due[0]["number"] == (await fa.asset(car))["number"]

    from ui.i18n import t

    page = await fa.page("/fixed-assets")
    assert t("fixed_assets.due_soon_title") in page.text and 'href="/fixed-assets/reminders"' in page.text
    listing = await fa.page("/fixed-assets/reminders")
    assert listing.status_code == 200 and "Oil change" in listing.text and "Dealer" in listing.text
    assert "Mutual" not in listing.text and "fixed_assets." not in listing.text


# ── Notices ──────────────────────────────────────────────────────────────────


async def test_grouped_notice_once_within_the_lead_time_and_once_on_the_due_date(fa):
    await _managed_from(fa)
    owner = await _owner(fa)
    car = await _contributed(fa)
    number = (await fa.asset(car))["number"]
    await _reminder(fa, car, kind="maintenance", work="Oil change", due="2026-11-20")
    await _reminder(fa, car, kind="warranty", provider="Dealer", due="2026-11-25")
    await _reminder(fa, car, kind="insurance", insurer="Mutual", due="2027-03-01")
    await _tick(fa, date(2026, 10, 15))
    assert await _addressed(fa) == []
    await _tick(fa, date(2026, 10, 26))
    told = await _addressed(fa)
    assert len(told) == 1 and told[0][0] == owner
    assert "Oil change" in told[0][2] and "Dealer" in told[0][2] and number in told[0][2]
    assert "Mutual" not in told[0][2]
    await _tick(fa, date(2026, 10, 27))
    assert len(await _addressed(fa)) == 1, "an item is told once within its lead time"
    await _tick(fa, date(2026, 11, 20))
    told = await _addressed(fa)
    assert len(told) == 2 and "Oil change" in told[1][2] and "Dealer" not in told[1][2]
    await _tick(fa, date(2026, 11, 21))
    assert len(await _addressed(fa)) == 2


async def test_each_responsible_person_gets_their_own_notice(fa):
    await _managed_from(fa)
    keeper, _ = await fa.member("manager")
    other, _ = await fa.member("manager")
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    await fa.ok(await fa.patch(f"/assets/{van}", {"changes": {"responsible_user_id": str(keeper)},
                                                  "previous": {"responsible_user_id": None}}))
    await _reminder(fa, car, kind="maintenance", work="Car service", due="2026-11-01")
    await _reminder(fa, van, kind="maintenance", work="Van service", due="2026-11-01")
    await _reminder(fa, van, kind="warranty", provider="Dealer", due="2026-11-01", responsible_user_id=str(other))
    await _tick(fa, date(2026, 10, 20))
    told = {who: body for who, _, body in await _addressed(fa)}
    assert "Van service" in told[str(keeper)] and "Dealer" not in told[str(keeper)]
    assert "Dealer" in told[str(other)] and "Van service" not in told[str(other)]
    managers = [who for who, _, body in await _addressed(fa) if "Car service" in body]
    owner = await _owner(fa)
    assert owner in managers and str(keeper) in managers, "no one responsible: everyone who manages fixed assets"


# ── Maintenance log ──────────────────────────────────────────────────────────


async def test_maintenance_log_entry_with_next_due_and_a_file(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    entry = await fa.ok(await fa.post(f"/assets/{car}/maintenance", {
        "date": "2026-10-01", "work": "Brake pads", "by": "City Garage", "next_due": "2027-10-01",
        "every_months": 12}))
    upkeep = await _upkeep(fa, car)
    assert [(m["work"], m["by"], m["next_due"], m["amount"]) for m in upkeep["maintenance"]] == [
        ("Brake pads", "City Garage", "2027-10-01", None)]
    assert [(r["kind"], r["work"], r["due"], r["every_months"]) for r in upkeep["reminders"]] == [
        ("maintenance", "Brake pads", "2027-10-01", 12)]
    r = await fa.client.post(f"/fixed-assets/api/maintenance/{entry['id']}/files", headers=fa.h,
                             files={"file": ("receipt.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    log = (await _upkeep(fa, car))["maintenance"][0]
    assert [f["filename"] for f in log["files"]] == ["receipt.pdf"]
    assert [f["document_tag"] for f in (await fa.asset(car))["files"]] == ["maintenance"]
    edited = await fa.ok(await fa.patch(f"/maintenance/{entry['id']}", {"changes": {"by": "Dealer workshop"}}))
    assert edited["by"] == "Dealer workshop"
    r = await fa.post(f"/assets/{car}/maintenance", {"date": "2026-10-01"})
    assert r.status_code == 422 and "Describe the maintenance work" in r.json()["detail"]
    removed = await fa.ok(await fa.post(f"/maintenance/{entry['id']}/remove", {}))
    assert removed["status"] == "removed"
    restored = await fa.ok(await fa.post(f"/maintenance/{entry['id']}/restore", {}))
    assert restored["status"] == "active"


async def test_maintenance_from_a_bill_shows_its_cost_read_only(fa):
    van = await fa.line("Delivery van")
    laptops = await fa.line("Laptop")
    asset_id = (await fa.register(van["source"], key="r1", mode="separate"))["assets"][0]["id"]
    await fa.operate("/register", {"source": laptops["source"], "mode": "maintenance", "asset_id": asset_id,
                                   "expense_account": "6950"}, key="mt")
    log = (await _upkeep(fa, asset_id))["maintenance"]
    assert len(log) == 1 and log[0]["amount"] == "4350.00" and log[0]["bill_ref"]
    entry = log[0]["id"]
    await fa.ok(await fa.patch(f"/maintenance/{entry}", {"changes": {"work": "Laptop repairs"}}))
    r = await fa.patch(f"/maintenance/{entry}", {"changes": {"date": "2026-01-01"}})
    assert r.status_code == 422 and "comes from the bill" in r.json()["detail"]
    r = await fa.patch(f"/maintenance/{entry}", {"changes": {"amount": "1"}})
    assert r.status_code == 422
    r = await fa.post(f"/maintenance/{entry}/remove", {})
    assert r.status_code == 422 and "undo its registration" in r.json()["detail"]
    clerk = await fa.role_headers("operator")
    seen = (await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{asset_id}/upkeep", headers=clerk)))
    assert seen["maintenance"][0]["amount"] is None, "money only for roles that see it"


# ── Pages ────────────────────────────────────────────────────────────────────


async def test_upkeep_card_on_the_asset_page_done_postpone_and_history(fa):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.upkeep_title") in page.text and t("fixed_assets.add_reminder") in page.text
    added = await fa.submit(f"/fixed-assets/assets/{car}/reminders", {
        "kind": "maintenance", "detail": "Oil change", "due": "2026-11-01", "every_months": "6", "lead_days": ""})
    assert added.status_code == 200 and "HX-Redirect" in added.headers, added.text
    rid = (await _upkeep(fa, car))["reminders"][0]["id"]
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert "Oil change" in page.text and "2026-11-01" in page.text and "fixed_assets." not in page.text
    assert f'hx-get="/fixed-assets/reminders/{rid}/field/due/edit"' in page.text
    cell = await fa._ui("PATCH", f"/fixed-assets/reminders/{rid}/field/due", data={"value": "2026-11-05"})
    assert cell.status_code == 200 and "2026-11-05" in cell.text
    moved = await fa.submit(f"/fixed-assets/reminders/{rid}/postpone", {"to": "2026-11-30", "reason": "Away"})
    assert "HX-Redirect" in moved.headers, moved.text
    refused = await fa.submit(f"/fixed-assets/reminders/{rid}/postpone", {"to": "2026-12-30", "reason": ""})
    assert "Say why" in refused.headers.get("HX-Trigger", "") + refused.text
    done = await fa.submit(f"/fixed-assets/reminders/{rid}/done", {"date": "2026-11-28"})
    assert "HX-Redirect" in done.headers, done.text
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert "2027-05-28" in page.text and "Away" in page.text
    assert t("fixed_assets.history.postponed") in page.text and t("fixed_assets.history.done") in page.text
    assert t("fixed_assets.maintenance_log") in page.text


async def test_maintenance_log_card_records_an_entry(fa):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    added = await fa.submit(f"/fixed-assets/assets/{car}/maintenance", {
        "date": "2026-10-01", "work": "Brake pads", "by": "City Garage", "next_due": "", "every_months": ""})
    assert "HX-Redirect" in added.headers, added.text
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert "Brake pads" in page.text and "City Garage" in page.text
    entry = (await _upkeep(fa, car))["maintenance"][0]["id"]
    assert f'hx-get="/fixed-assets/maintenance/{entry}/field/work/edit"' in page.text
    assert t("fixed_assets.attach_file") in page.text


async def test_custodian_without_manage_cannot_add_reminders(fa):
    uid, headers = await fa.member("viewer")
    car = await _contributed(fa)
    r = await _ui(fa, "POST", f"/fixed-assets/assets/{car}/reminders", headers,
                  data={"kind": "warranty", "detail": "Dealer", "due": _on(5)})
    assert "HX-Redirect" not in r.headers and "error" in r.headers.get("HX-Trigger", "")
    assert (await _upkeep(fa, car))["reminders"] == []
