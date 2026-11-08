# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An agreement with no lines has nothing to bill: every bill it made would be refused for
having no line. So no door makes one billable, and each refusal names the way to give it
lines, which that state allows.

The rule sits in the status guard (billing.refuse_billing_nothing), which every status change
by any writer reaches (test_doc_status_guard_door). The doors of the module are read from the
code (consent_census.billable_routes): a door found here with no scenario fails, as does a
status that becomes billable from one the guard's table does not name."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agreement_kit import LINE, agreement, freeze_day, register, template  # noqa: E402
from consent_census import billable_routes  # noqa: E402

from celerp_subscriptions import billing  # noqa: E402
from ui.i18n import t  # noqa: E402

DAY = "2026-03-01"


def _refusal(r) -> tuple[str | None, str | None]:
    detail = r.json().get("detail") if r.status_code == 409 else None
    return (detail.get("message_key"), detail.get("message")) if isinstance(detail, dict) else (None, r.text)


def _import(eid: str, status: str, lines: list) -> dict:
    data = {"doc_type": "subscription_invoice", "ref_id": eid.split(":")[1], "status": status,
            "contact_id": "contact:test-001", "frequency": "monthly", "start_date": DAY, "anchor_date": DAY,
            "line_items": lines}
    return {"entity_id": eid, "event_type": "doc.created", "data": data, "source": "import",
            "idempotency_key": str(uuid.uuid4())}


async def _page(ui, eid: str) -> str:
    r = await ui.get(f"/subscriptions/{eid}")
    assert r.status_code == 200, r.text
    return r.text


async def _activate_door(client, hd, ui) -> None:
    """Activate on a draft with no lines names + Add item, which its page offers; a line added
    there, Activate accepts."""
    eid = await agreement(client, hd, start=DAY, activate=False, line_items=[])
    ref = (await template(client, hd, eid))["ref_id"]
    assert _refusal(await client.post(f"/subscriptions/{eid}/activate", headers=hd)) == (
        "subscriptions.err_no_lines_draft", t("subscriptions.err_no_lines_draft", "en", ref=ref))
    assert (await template(client, hd, eid))["status"] == "draft"
    assert t("btn._add_item", "en") in await _page(ui, eid)
    r = await client.patch(f"/docs/{eid}", headers=hd, json={"fields_changed": {
        "line_items": {"old": [], "new": [dict(LINE)]}}})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/subscriptions/{eid}/activate", headers=hd)).status_code == 200


async def _resume_door(client, hd, ui) -> None:
    """Resume on a paused agreement with no lines (an import brings one in; a paused one bills
    nothing) names New Subscription, which the Subscriptions page offers and which starts one."""
    eid = f"doc:SUBI-N-{uuid.uuid4().hex[:8]}"
    assert (await client.post("/docs/import", headers=hd, json=_import(eid, "paused", []))).status_code == 200
    assert _refusal(await client.post(f"/subscriptions/{eid}/resume", headers=hd)) == (
        "subscriptions.err_no_lines_running", t("subscriptions.err_no_lines_running", "en", ref=eid.split(":")[1]))
    assert (await template(client, hd, eid))["status"] == "paused"
    listing = await ui.get("/subscriptions?direction=sales")
    assert t("page.new_subscription", "en") in listing.text and 'hx-post="/subscriptions/new?direction=sales"' in listing.text
    assert "HX-Redirect" in (await ui.post("/subscriptions/new?direction=sales")).headers


BILLABLE_DOORS = {"activate_subscription": _activate_door, "resume_subscription": _resume_door}


def test_every_door_that_makes_an_agreement_billable_has_its_scenario():
    assert set(billable_routes()) == set(BILLABLE_DOORS)


@pytest.mark.parametrize("door", sorted(BILLABLE_DOORS))
async def test_each_billable_door_refuses_an_agreement_with_no_lines_and_names_the_way_on(
        client, owner_ui, monkeypatch, door):
    freeze_day(monkeypatch, DAY)
    await BILLABLE_DOORS[door](client, owner_ui.api.headers, owner_ui)


async def test_an_import_brings_no_billable_agreement_without_lines_and_its_draft_takes_them(
        client, owner_ui, monkeypatch):
    """The import refused names importing it as a draft; that import is taken, and the draft's
    page offers + Add item."""
    freeze_day(monkeypatch, DAY)
    hd, eid = owner_ui.api.headers, f"doc:SUBI-M-{uuid.uuid4().hex[:8]}"
    assert _refusal(await client.post("/docs/import", headers=hd, json=_import(eid, "active", []))) == (
        "subscriptions.err_no_lines_import", t("subscriptions.err_no_lines_import", "en", ref=eid.split(":")[1]))
    assert (await client.get(f"/docs/{eid}", headers=hd)).status_code == 404
    assert (await client.post("/docs/import", headers=hd, json=_import(eid, "draft", []))).status_code == 200
    assert t("btn._add_item", "en") in await _page(owner_ui, eid)
    with_lines = f"doc:SUBI-M-{uuid.uuid4().hex[:8]}"
    assert (await client.post("/docs/import", headers=hd,
                              json=_import(with_lines, "active", [dict(LINE)]))).status_code == 200


async def test_new_subscription_then_activate_with_no_line_is_refused_where_the_user_is(owner_ui, monkeypatch):
    """The journey through the pages: New Subscription, then Activate before any line. The refusal
    shows on the agreement's page, which stays a draft offering + Add item."""
    freeze_day(monkeypatch, DAY)
    r = await owner_ui.post("/subscriptions/new?direction=sales")
    page = r.headers["HX-Redirect"]
    eid = page.rsplit("/", 1)[1]
    ref = (await owner_ui.api.get(f"/docs/{eid}")).json()["ref_id"]
    r = await owner_ui.post(f"/subscriptions/{eid}/activate")
    assert "HX-Redirect" not in r.headers
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == t("subscriptions.err_no_lines_draft", "en", ref=ref), toast
    assert (await owner_ui.api.get(f"/docs/{eid}")).json()["status"] == "draft"
    assert t("btn._add_item", "en") in await _page(owner_ui, eid)


_ON = {"anchor_date": DAY, "frequency": "monthly", "doc_type": "subscription_invoice", "ref_id": "SUBI-T"}


@pytest.mark.parametrize("was", [None, *sorted(billing.AGREEMENT_STATUSES - {"active"})])
async def test_whatever_status_it_leaves_a_person_never_makes_an_agreement_with_no_lines_billable(was):
    """Every status an agreement can leave for active (and none, an import) is refused with the
    refusal its own state allows; the same change with a line, or made by nobody (the upgrade, a
    restore), is taken."""
    before = {**_ON, "status": was} if was else {}
    after = {**_ON, "status": "active"}
    person, nobody = SimpleNamespace(actor_id=uuid.uuid4(), entity_id="doc:SUBI-T"), SimpleNamespace(
        actor_id=None, entity_id="doc:SUBI-T")
    with pytest.raises(HTTPException) as refused:
        billing.refuse_billing_nothing(before, after, person)
    key = {"draft": "subscriptions.err_no_lines_draft", None: "subscriptions.err_no_lines_import"}.get(
        was, "subscriptions.err_no_lines_running")
    assert refused.value.status_code == 409 and refused.value.detail["message_key"] == key
    assert refused.value.detail["message"] == t(key, "en", ref="SUBI-T")
    billing.refuse_billing_nothing(before, {**after, "line_items": [dict(LINE)]}, person)
    billing.refuse_billing_nothing(before, after, nobody)


async def test_a_change_that_leaves_billable_as_it_was_is_never_refused_for_no_lines():
    """Already billing, or still not billing after the change: the rule is about making it billable."""
    person = SimpleNamespace(actor_id=uuid.uuid4(), entity_id="doc:SUBI-T")
    running = {**_ON, "status": "active"}
    billing.refuse_billing_nothing(running, {**running, "frequency": "weekly"}, person)
    billing.refuse_billing_nothing(running, {**running, "status": "paused"}, person)
    billing.refuse_billing_nothing({**_ON, "status": "draft"}, {**_ON, "status": "void"}, person)
    billing.refuse_billing_nothing({"status": "paused"}, {"status": "active", "doc_type": "subscription_invoice"}, person)
