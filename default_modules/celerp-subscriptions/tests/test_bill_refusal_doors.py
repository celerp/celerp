# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Nothing on an agreement changes once it is active, so the one way out of a bill that is refused
is to cancel the agreement and create a new one. Every refusal the bill path raises (_bill: the
document it makes and finalize's checks, its stock included) reaches the user as
subscriptions.err_bill_refused naming that way out. billing.bill_refusal is the same path run
inside a savepoint that is rolled back, so the doors that make an agreement bill (Activate,
Resume, Bill automatically) refuse exactly what its bills would. The check is at those doors
only: recording what happened to a bill already made is never refused.

The doors are read from the code: every route that writes an agreement's status as active
(consent_census.billable_routes) or chooses how it bills (generation_mode) calls bill_refusal."""
from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agreement_kit import (  # noqa: E402
    LINE, agreement, billed_docs, company_id, contact, freeze_day, generate_due, register, schedule, summary,
    template, tick)
from consent_census import billable_routes, route_doors  # noqa: E402

from celerp.services.email import EmailResult  # noqa: E402
from celerp_subscriptions import billing, routes  # noqa: E402
from celerp_subscriptions.billing import write_agreement  # noqa: E402
from ui.i18n import t  # noqa: E402

DAY = "2026-03-01"
UNTAXED = [{**LINE, "taxes": None}]
REFUSED = "subscriptions.err_bill_refused"

# Agreement content each bill check refuses, with the reason the refusal carries.
MAKERS = {
    "no lines": ("subscription_invoice", {"line_items": []}, "documents.err_finalize_no_lines"),
    "tax unset": ("subscription_invoice", {"line_items": UNTAXED}, "documents.err_line_tax_unset"),
    "unpriced": ("subscription_po", {"line_items": [{"description": "Hosting", "quantity": 1, "taxes": []}]},
                 "docs.unpriced_line"),
    "purchase tax": ("subscription_invoice", {"line_items": [{**LINE, "taxes": [{"code": "WHT", "rate": -3}]}]},
                     "documents.err_tax_purchase_only"),
    "no account": ("subscription_po", {"line_items": [{**LINE, "taxes": [], "account_code": "9999"}]},
                   "posting.destination.not_in_chart"),
    "no rate": ("subscription_invoice", {"currency": "EUR"}, "error.rate_required"),
    "units": ("subscription_invoice", {"line_items": [{**LINE, "unit": "piece", "quantity": 1.5}]},
              "units.template_finer_than_unit"),
}


def _key(r) -> str | None:
    detail = r.json().get("detail") if r.status_code in (404, 409, 422) else None
    return detail.get("message_key") if isinstance(detail, dict) else None


def _reason(r) -> str | None:
    return r.json()["detail"]["params"]["reason"]["message_key"]


def _says(r, ref: str) -> None:
    """The refusal names the reason inside, then the one way out: cancel it and create a new one."""
    detail = r.json()["detail"]
    reason = detail["params"]["reason"]
    assert r.status_code == 409 and detail["message_key"] == REFUSED, r.text
    assert detail["message"] == t(REFUSED, "en", ref=ref, reason=t(reason["message_key"], "en", **reason["params"]))


async def _stored(session, client, hd, eid: str, **fields) -> None:
    """The agreement's content as an earlier release left it (a write nobody made keeps it)."""
    await write_agreement(session, uuid.UUID(await company_id(client, hd)), eid, fields, None)
    await session.commit()


async def _hand_edited(session, client, hd, eid: str, lines) -> None:
    """The agreement's lines as a hand edit of the database left them, past every write's checks."""
    from celerp.models.projections import Projection

    proj = await session.get(Projection, {"company_id": uuid.UUID(await company_id(client, hd)), "entity_id": eid})
    proj.state = {**proj.state, "line_items": lines}
    await session.commit()


async def _page(ui, eid: str) -> str:
    r = await ui.get(f"/subscriptions/{eid}")
    assert r.status_code == 200, r.text
    return r.text


def _calls(fn, name: str) -> bool:
    return any(isinstance(c, ast.Call) and (getattr(c.func, "attr", None) or getattr(c.func, "id", None)) == name
               for c in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))))


def test_every_door_that_makes_an_agreement_bill_asks_the_bill_path():
    """Activate, Resume and Bill automatically, and any route added later that activates an
    agreement or chooses how it bills, ask bill_refusal; bill_refusal runs _bill itself."""
    doors = set(billable_routes()) | {name for name, (_, sets, _) in route_doors().items() if "generation_mode" in sets}
    assert doors == {"activate_subscription", "resume_subscription", "change_schedule"}
    source = inspect.getsource(routes._build_router)  # the routes are closures of _build_router
    for door in doors:
        body = source.split(f"async def {door}(", 1)[1].split("\n    @router.", 1)[0]
        assert "billing.bill_refusal(" in body, door
    assert _calls(billing.bill_refusal, "_bill")
    assert not _calls(billing.write_agreement, "bill_refusal")
    assert not _calls(billing.guard_agreement_status, "bill_refusal")


async def test_the_line_edit_of_an_active_agreement_is_gone(client, monkeypatch):
    freeze_day(monkeypatch, DAY)
    hd = await register(client)
    eid = await agreement(client, hd, start=DAY)
    r = await client.patch(f"/subscriptions/{eid}/lines", headers=hd, json={"fields_changed": {}})
    assert r.status_code in (404, 405), r.text


@pytest.mark.parametrize("maker", sorted(MAKERS))
async def test_every_refusal_of_the_bill_names_cancel_and_new_at_every_door(client, session, owner_ui, monkeypatch, maker):
    """Each check's refusal: Generate due, Bill automatically and Resume all answer err_bill_refused
    with that reason inside; the agreement stays as it was, and its page offers Cancel."""
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    doc_type, fields, reason = MAKERS[maker]
    eid = await agreement(client, hd, start=DAY, doc_type=doc_type)
    await _stored(session, client, hd, eid, **fields)
    ref = (await template(client, hd, eid))["ref_id"]
    for r in (await generate_due(client, hd, eid), await schedule(client, hd, eid, generation_mode="auto_invoice")):
        _says(r, ref)
        assert _reason(r) == reason
    assert (await template(client, hd, eid)).get("generation_mode") != "auto_invoice"
    assert f'hx-post="/subscriptions/{eid}/cancel"' in await _page(owner_ui, eid)
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    r = await client.post(f"/subscriptions/{eid}/resume", headers=hd)
    _says(r, ref)
    assert _reason(r) == reason and (await template(client, hd, eid))["status"] == "paused"
    assert (await client.post(f"/subscriptions/{eid}/cancel", headers=hd)).status_code == 200
    assert await billed_docs(session, eid) == []


async def test_a_lot_sold_elsewhere_refuses_every_door_naming_cancel_and_new(client, session, owner_ui, monkeypatch):
    """A line bound to a lot of one piece: the first bill ships it, so finalize refuses the next
    bill for that lot (a refusal of finalize's stock checks, not of the document's content).
    Generate due, Bill automatically and Resume answer err_bill_refused with it, the pass stores
    it as the failure, and Cancel then a new subscription bills."""
    hd = owner_ui.api.headers
    item = (await client.post("/items", headers=hd, json={"sku": "LOT-1", "name": "Lot", "sell_by": "piece",
                                                         "quantity": 1})).json()["id"]
    await client.post("/items/bulk/make-available", headers=hd, json={"entity_ids": [item]})
    freeze_day(monkeypatch, "2026-01-01")
    eid = await agreement(client, hd, start="2026-01-01",
                          line_items=[{**LINE, "taxes": [], "item_id": item, "quantity": 1, "unit": "piece"}])
    first = await generate_due(client, hd, eid)
    assert first.status_code == 200, first.text
    shipped = await client.post(f"/docs/{first.json()['doc_id']}/fulfill-lines", headers=hd,
                                json={"line_entity_ids": [item]})
    assert shipped.status_code == 200, shipped.text
    freeze_day(monkeypatch, "2026-02-01")
    ref = (await template(client, hd, eid))["ref_id"]
    r = await generate_due(client, hd, eid)
    _says(r, ref)
    assert _reason(r) == "item.invoice_not_available_on"
    _says(await schedule(client, hd, eid, generation_mode="auto_invoice"), ref)
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    _says(await client.post(f"/subscriptions/{eid}/resume", headers=hd), ref)
    assert len(await billed_docs(session, eid)) == 1
    assert (await client.post(f"/subscriptions/{eid}/cancel", headers=hd)).status_code == 200
    fresh = await agreement(client, hd, start="2026-02-01")
    assert (await generate_due(client, hd, fresh)).status_code == 200


async def test_a_bill_failed_by_the_pass_stores_the_same_refusal(client, session, monkeypatch):
    """The automatic pass meets the bill path's refusal as Generate due words it."""
    freeze_day(monkeypatch, DAY)
    hd = await register(client)
    eid = await agreement(client, hd, start=DAY)
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    await _stored(session, client, hd, eid, line_items=UNTAXED)
    await tick(session, await company_id(client, hd), DAY)
    failure = (await template(client, hd, eid))["billing_failure"]
    assert failure["reason"]["message_key"] == REFUSED
    assert failure["reason"]["params"]["reason"]["message_key"] == "documents.err_line_tax_unset"


async def test_asking_the_bill_path_keeps_nothing(client, session, monkeypatch):
    """An accepted Resume ran the whole bill: no document, no number taken, no period claimed,
    and the next bill takes the number it would have taken anyway."""
    freeze_day(monkeypatch, DAY)
    hd = await register(client)
    eid = await agreement(client, hd, start=DAY)
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    before = (await client.get("/docs/sequences", headers=hd)).json()
    assert (await client.post(f"/subscriptions/{eid}/resume", headers=hd)).status_code == 200
    assert (await client.get("/docs/sequences", headers=hd)).json() == before
    assert await billed_docs(session, eid) == [] and not (await template(client, hd, eid)).get("periods")
    assert (await generate_due(client, hd, eid)).status_code == 200


@pytest.mark.parametrize("stored", [None, 0, 7, "x", {}, {"a": 1}, [None], ["x"], [[]], False], ids=repr)
async def test_lines_stored_as_anything_but_mappings_bill_nothing_and_are_refused_as_none(
        client, session, monkeypatch, stored):
    """Lines an earlier release or a hand edit stored as something other than a list of mappings
    hold no line: the bill is refused for having none (never an error), and a mapping beside
    them still bills."""
    freeze_day(monkeypatch, DAY)
    hd = await register(client)
    eid = await agreement(client, hd, start=DAY)
    await _hand_edited(session, client, hd, eid, stored)
    r = await generate_due(client, hd, eid)
    assert _key(r) == REFUSED and _reason(r) == "documents.err_finalize_no_lines", r.text
    mixed = await agreement(client, hd, start=DAY)
    await _hand_edited(session, client, hd, mixed, [{**LINE, "taxes": []}, *(stored if isinstance(stored, list) else [stored])])
    assert (await generate_due(client, hd, mixed)).status_code == 200


async def test_activate_refuses_a_draft_its_bill_refuses(client, owner_ui, monkeypatch):
    """A draft whose line has no tax chosen: Activate answers err_bill_refused, the draft stays a
    draft (its lines still change on its page), and once they bill Activate accepts."""
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    eid = await agreement(client, hd, start=DAY, activate=False, line_items=UNTAXED)
    ref = (await template(client, hd, eid))["ref_id"]
    _says(await client.post(f"/subscriptions/{eid}/activate", headers=hd), ref)
    assert (await template(client, hd, eid))["status"] == "draft"
    assert t("btn._add_item", "en") in await _page(owner_ui, eid)
    doc = await template(client, hd, eid)
    r = await client.patch(f"/docs/{eid}", headers=hd, json={"expected_version": doc["version"], "fields_changed": {
        "line_items": {"old": doc["line_items"], "new": [{**LINE, "taxes": []}]}}})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/subscriptions/{eid}/activate", headers=hd)).status_code == 200


async def test_an_id_holding_a_nul_is_not_found(client, monkeypatch):
    """Postgres stores no NUL, so no agreement has one in its id: refused as not found, never a 500."""
    hd = await register(client)
    for r in (await client.post("/subscriptions/doc:A%00B/resume", headers=hd),
              await client.post("/subscriptions/doc:A%00B/pause", headers=hd)):
        assert r.status_code == 404 and _key(r) == "subscriptions.err_not_found", r.text


async def test_new_subscription_then_activate_with_no_line_is_refused_where_the_user_is(owner_ui, monkeypatch):
    """The journey through the pages: New Subscription, then Activate before any line. The refusal
    shows on the agreement's page, which stays a draft offering + Add item."""
    freeze_day(monkeypatch, DAY)
    r = await owner_ui.post("/subscriptions/new?direction=sales")
    eid = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    ref = (await owner_ui.api.get(f"/docs/{eid}")).json()["ref_id"]
    r = await owner_ui.post(f"/subscriptions/{eid}/activate")
    assert "HX-Redirect" not in r.headers
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == t(REFUSED, "en", ref=ref, reason=t("documents.err_finalize_no_lines", "en")), toast
    assert (await owner_ui.api.get(f"/docs/{eid}")).json()["status"] == "draft"
    assert t("btn._add_item", "en") in await _page(owner_ui, eid)


# --- Recording the send of a bill already made is never refused for the next bill ---

class _Mail:
    def __init__(self, result=EmailResult(True, "accepted"), during=None):
        self.result, self.sent, self.during = result, [], during

    async def __call__(self, to, subject, body_html, body_text="", **kwargs):
        self.sent.append(to)
        if self.during is not None:
            await self.during()
        return self.result


async def _sending(client, monkeypatch):
    """An agreement on Bill and Send automatically whose line tax the owner can later make
    purchase only, which refuses its next bill."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    tax = {"name": "Dues VAT", "rate": 7.0, "tax_type": "both"}
    assert (await client.patch("/companies/me/taxes", headers=hd, json={"taxes": [tax]})).status_code == 200
    line = {"description": "Dues", "quantity": 1, "unit_price": 100, "taxes": [{"code": tax["name"], "rate": tax["rate"]}]}
    eid = await agreement(client, hd, start="2026-07-01", line_items=[line],
                          contact_id=await contact(client, hd, "member@example.test"))
    r = await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0, auto_send=True)
    assert r.status_code == 200, r.text

    async def purchase_only():
        r = await client.patch("/companies/me/taxes", headers=hd, json={"taxes": [{**tax, "tax_type": "purchase"}]})
        assert r.status_code == 200, r.text
    return hd, await company_id(client, hd), eid, purchase_only


async def test_a_waiting_email_goes_out_after_the_next_bill_is_refused(client, session, monkeypatch):
    """The pass billed, the app stopped before the email; then the next bill became refused. The
    next pass sends the waiting email and records it."""
    from celerp.services import email
    hd, cid, eid, purchase_only = await _sending(client, monkeypatch)
    real = billing._deliverer
    monkeypatch.setattr(billing, "_deliverer", lambda *a: (lambda factory: __import__("asyncio").sleep(0)))
    await tick(session, cid, "2026-07-01")
    monkeypatch.setattr(billing, "_deliverer", real)
    await purchase_only()
    mail = _Mail()
    monkeypatch.setattr(email, "send_email", mail)
    await tick(session, cid, "2026-07-02")
    assert mail.sent == ["member@example.test"]
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "sent"


async def test_an_email_accepted_while_the_next_bill_becomes_refused_is_recorded_sent(client, session, monkeypatch):
    from celerp.services import email
    hd, cid, eid, purchase_only = await _sending(client, monkeypatch)
    mail = _Mail(during=purchase_only)
    monkeypatch.setattr(email, "send_email", mail)
    await tick(session, cid, "2026-07-01")
    assert mail.sent == ["member@example.test"]
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "sent"
    assert (await billed_docs(session, eid))[0]["status"] == "sent"


async def test_a_retry_of_a_failed_send_goes_out_after_the_next_bill_is_refused(client, session, monkeypatch):
    from celerp.services import email
    hd, cid, eid, purchase_only = await _sending(client, monkeypatch)
    mail = _Mail(EmailResult(False, "550", reason="email.err_smtp_refused"))
    monkeypatch.setattr(email, "send_email", mail)
    await tick(session, cid, "2026-07-01")
    [row] = [r for r in (await summary(client, hd))["rows"] if r["kind"] == "send_failed"]
    await purchase_only()
    mail.result = EmailResult(True, "accepted")
    r = await client.post(row["retry_url"], headers=hd, json={"attempt": row["attempt"]})
    assert r.status_code == 200, r.text
    assert len(mail.sent) == 2
    assert (await template(client, hd, eid))["periods"]["0"]["send"]["state"] == "sent"
