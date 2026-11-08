# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An agreement bills only what its bill can be made from: billing.bill_refusal runs the checks
Generate due's bill meets on the agreement's content (its units, then every content refusal
finalize raises) on the document it would make. No person's write leaves an agreement billable
while that check refuses (billing.refuse_bill_refused, in the status guard every writer reaches),
and each refusal names the way out its state offers. One already in that state (made by an
earlier release) is paused, takes its line fix on its page, and resumes.

The census is read from the code: every check bill_refusal reaches has a maker here (MAKERS),
and every door of the module that makes an agreement billable (consent_census.billable_routes)
has its scenario, as do the writers outside it (Bill automatically, an edit through Documents,
an import, an unvoid)."""
from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agreement_kit import (  # noqa: E402
    LINE, agreement, billed_docs, company_id, freeze_day, generate_due, register, schedule, template)
from consent_census import billable_routes  # noqa: E402

from celerp_subscriptions import billing  # noqa: E402
from celerp_subscriptions.billing import write_agreement  # noqa: E402
from ui.i18n import t  # noqa: E402

DAY = "2026-03-01"
UNTAXED = [{**LINE, "taxes": None}]


def _checks(fn, seen: frozenset = frozenset()) -> set[str]:
    """The refusals ``fn`` raises on the document: each refuse_/require_ function it calls (the
    ones of celerp_docs.routes it reaches walked into) and each catalog key it raises itself."""
    from celerp_docs import routes as docs_routes

    found = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if isinstance(node, ast.Raise):
            found |= {c.args[0].value for c in ast.walk(node) if isinstance(c, ast.Call) and c.args
                      and isinstance(c.args[0], ast.Constant) and str(c.args[0].value).startswith("documents.")}
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if not name.lstrip("_").startswith(("refuse_", "require_")) or name in seen:
                continue
            if name == "refuse_unfinalizable":
                found |= _checks(getattr(docs_routes, name), seen | {name})
            else:
                found.add(name)
    return found


# Per check bill_refusal reaches: the agreement content it refuses, and the refusal it gives.
MAKERS = {
    "documents.err_finalize_no_lines": ("subscription_invoice", {"line_items": []}, "documents.err_finalize_no_lines"),
    "refuse_unset_line_tax": ("subscription_invoice", {"line_items": UNTAXED}, "documents.err_line_tax_unset"),
    "refuse_unpriced_lines": ("subscription_po", {"line_items": [{"description": "Hosting", "quantity": 1, "taxes": []}]},
                              "docs.unpriced_line"),
    "refuse_unissuable_taxes": ("subscription_invoice", {"line_items": [
        {**LINE, "taxes": [{"code": "WHT", "rate": -3}]}]}, "documents.err_tax_purchase_only"),
    "require_line_destinations": ("subscription_po", {"line_items": [{**LINE, "taxes": [], "account_code": "9999"}]},
                                  "posting.destination.not_in_chart"),
    "_require_doc_rate_http": ("subscription_invoice", {"currency": "EUR"}, "error.rate_required"),
    "require_moving_fits_units": ("subscription_invoice", {"line_items": [{**LINE, "unit": "piece", "quantity": 1.5}]},
                                  "units.template_finer_than_unit"),
}

# The refusal per status the agreement held before the write (None: an import).
WAS = {"active": "subscriptions.err_bill_refused_running", "paused": "subscriptions.err_bill_refused_paused",
       "draft": "subscriptions.err_bill_refused_draft", None: "subscriptions.err_bill_refused_import",
       "void": "subscriptions.err_bill_refused_ended", "cancelled": "subscriptions.err_bill_refused_ended"}


def test_every_check_the_bill_meets_has_its_maker():
    assert _checks(billing.bill_refusal) == set(MAKERS)


def test_every_status_an_agreement_can_hold_has_its_refusal():
    assert set(WAS) == billing.AGREEMENT_STATUSES | {None}


def _state(maker: str, status: str) -> dict:
    doc_type, fields, _ = MAKERS[maker]
    return {"doc_type": doc_type, "ref_id": "SUBI-T", "status": status, "contact_id": "contact:test-001",
            "frequency": "monthly", "start_date": DAY, "anchor_date": DAY, "line_items": [{**LINE, "taxes": []}], **fields}


@pytest.mark.parametrize("maker", sorted(MAKERS))
async def test_whatever_status_it_held_no_person_leaves_an_agreement_billing_what_its_bill_refuses(
        client, session, monkeypatch, maker):
    """Every check x every status before the write: refused with that status's refusal, the
    check's own reason inside; the agreement fixed, paused, or written by nobody (the upgrade, a
    restore) is taken."""
    freeze_day(monkeypatch, DAY)
    cid = uuid.UUID(await company_id(client, await register(client)))
    person = SimpleNamespace(actor_id=uuid.uuid4(), entity_id="doc:SUBI-T", company_id=cid)
    nobody = SimpleNamespace(actor_id=None, entity_id="doc:SUBI-T", company_id=cid)
    after = _state(maker, "active")
    for was, key in WAS.items():
        before = {**after, "status": was} if was else {}
        with pytest.raises(HTTPException) as refused:
            await billing.refuse_bill_refused(session, before, after, person)
        detail = refused.value.detail
        assert refused.value.status_code == 409 and detail["message_key"] == key, (was, detail)
        reason = detail["params"]["reason"]
        assert reason["message_key"] == MAKERS[maker][2], detail
        assert detail["message"] == t(key, "en", ref="SUBI-T", reason=t(reason["message_key"], "en", **reason["params"]))
        await billing.refuse_bill_refused(session, before, after, nobody)
        await billing.refuse_bill_refused(session, before, {**after, "status": "paused"}, person)
    fixed = {k: v for k, v in after.items() if k != "currency"} | {"line_items": [{**LINE, "taxes": []}]}
    await billing.refuse_bill_refused(session, {}, fixed, person)


@pytest.mark.parametrize("stored", [None, 0, 7, "x", {}, {"a": 1}, [None], ["x"], [[]], False], ids=repr)
async def test_lines_stored_as_anything_but_mappings_bill_nothing_and_are_refused_as_none(
        client, session, monkeypatch, stored):
    """Lines an earlier release or a hand edit stored as something other than a list of mappings
    hold no line: the bill is refused for having none (never an error), and a mapping beside
    them still bills."""
    freeze_day(monkeypatch, DAY)
    cid = uuid.UUID(await company_id(client, await register(client)))
    person = SimpleNamespace(actor_id=uuid.uuid4(), entity_id="doc:SUBI-T", company_id=cid)
    after = _state("documents.err_finalize_no_lines", "active") | {"line_items": stored}
    with pytest.raises(HTTPException) as refused:
        await billing.refuse_bill_refused(session, {}, after, person)
    assert refused.value.detail["params"]["reason"]["message_key"] == "documents.err_finalize_no_lines"
    mixed = [{**LINE, "taxes": []}, *(stored if isinstance(stored, list) else [stored])]
    await billing.refuse_bill_refused(session, {}, after | {"line_items": mixed}, person)


def _key(r) -> str | None:
    detail = r.json().get("detail") if r.status_code in (404, 409, 422) else None
    return detail.get("message_key") if isinstance(detail, dict) else None


async def _broken(session, client, hd, eid: str, lines=UNTAXED) -> None:
    """The agreement's lines as an earlier release left them (a write nobody made keeps them)."""
    await write_agreement(session, uuid.UUID(await company_id(client, hd)), eid, {"line_items": lines}, None)
    await session.commit()


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


async def _activate_door(client, session, hd, ui) -> None:
    """Activate on a draft whose line has no tax chosen names the fix on its page, which edits its
    lines; the line fixed there, Activate accepts."""
    eid = await agreement(client, hd, start=DAY, activate=False, line_items=UNTAXED)
    assert _key(await client.post(f"/subscriptions/{eid}/activate", headers=hd)) == "subscriptions.err_bill_refused_draft"
    assert (await template(client, hd, eid))["status"] == "draft"
    assert t("btn._add_item", "en") in await _page(ui, eid)
    doc = await template(client, hd, eid)
    r = await client.patch(f"/docs/{eid}", headers=hd, json={"expected_version": doc["version"], "fields_changed": {
        "line_items": {"old": doc["line_items"], "new": [{**LINE, "taxes": []}]}}})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/subscriptions/{eid}/activate", headers=hd)).status_code == 200


async def _resume_door(client, session, hd, ui) -> None:
    """The way out for an agreement an earlier release left billing a line with no tax: its bill
    fails; Pause; Resume names the fix on its page, which edits a paused agreement's lines; the
    line fixed there, Resume accepts and Generate due bills it."""
    eid = await agreement(client, hd, start=DAY)
    await _broken(session, client, hd, eid)
    gd = await client.post(f"/subscriptions/{eid}/generate-due", headers=hd, json={"request_key": "broken"})
    assert _key(gd) == "subscriptions.err_bill_refused_running" and t("btn.pause", "en") in await _page(ui, eid)
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    assert _key(await client.post(f"/subscriptions/{eid}/resume", headers=hd)) == "subscriptions.err_bill_refused_paused"
    page = await _page(ui, eid)
    assert f'window._CELERP_LINES_URL = "/subscriptions/{eid}/lines"' in page
    doc = await template(client, hd, eid)
    saved = await ui.post(f"/subscriptions/{eid}/lines", json={
        "expected_version": doc["version"], "line_items": [{**LINE, "taxes": []}]})
    assert saved.status_code == 200 and saved.json()["ok"], saved.text
    assert (await client.post(f"/subscriptions/{eid}/resume", headers=hd)).status_code == 200
    gd = await client.post(f"/subscriptions/{eid}/generate-due", headers=hd, json={"request_key": "fixed"})
    assert gd.status_code == 200, gd.text
    assert [d["line_items"][0]["taxes"] for d in await billed_docs(session, eid)] == [[]]


BILLABLE_DOORS = {"activate_subscription": _activate_door, "resume_subscription": _resume_door}


def test_every_door_that_makes_an_agreement_billable_has_its_scenario():
    assert set(billable_routes()) == set(BILLABLE_DOORS)


@pytest.mark.parametrize("door", sorted(BILLABLE_DOORS))
async def test_each_billable_door_refuses_what_the_bill_refuses_and_its_way_out_accepts(
        client, session, owner_ui, monkeypatch, door):
    freeze_day(monkeypatch, DAY)
    await BILLABLE_DOORS[door](client, session, owner_ui.api.headers, owner_ui)


async def test_no_edit_through_documents_leaves_a_running_agreement_billing_nothing(client, owner_ui, monkeypatch):
    """Every line taken off an active agreement through PATCH /docs is refused naming Pause, and
    the agreement keeps its lines."""
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    eid = await agreement(client, hd, start=DAY)
    doc = await template(client, hd, eid)
    r = await client.patch(f"/docs/{eid}", headers=hd, json={"expected_version": doc["version"], "fields_changed": {
        "line_items": {"old": doc["line_items"], "new": []}}})
    assert _key(r) == "subscriptions.err_bill_refused_running", r.text
    assert r.json()["detail"]["params"]["reason"]["message_key"] == "documents.err_finalize_no_lines"
    assert (await template(client, hd, eid))["line_items"] == doc["line_items"]


async def test_bill_automatically_on_an_agreement_whose_bill_is_refused_is_refused(client, session, owner_ui, monkeypatch):
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    eid = await agreement(client, hd, start=DAY)
    await _broken(session, client, hd, eid)
    r = await schedule(client, hd, eid, generation_mode="auto_invoice")
    assert _key(r) == "subscriptions.err_bill_refused_running", r.text
    assert (await template(client, hd, eid)).get("generation_mode") != "auto_invoice"


async def test_an_import_brings_no_billable_agreement_its_bill_refuses_and_its_draft_takes_the_fix(
        client, owner_ui, monkeypatch):
    freeze_day(monkeypatch, DAY)
    hd, eid = owner_ui.api.headers, f"doc:SUBI-M-{uuid.uuid4().hex[:8]}"
    assert _key(await client.post("/docs/import", headers=hd, json=_import(eid, "active", []))) == (
        "subscriptions.err_bill_refused_import")
    assert (await client.get(f"/docs/{eid}", headers=hd)).status_code == 404
    assert (await client.post("/docs/import", headers=hd, json=_import(eid, "draft", []))).status_code == 200
    assert t("btn._add_item", "en") in await _page(owner_ui, eid)
    batch = f"doc:SUBI-M-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=hd, json={"records": [_import(batch, "active", [])]})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    ref = batch.split(":")[1]
    assert any(f"{ref} can't be imported as active." in e for e in r.json()["errors"]), r.json()["errors"]
    fine = f"doc:SUBI-M-{uuid.uuid4().hex[:8]}"
    assert (await client.post("/docs/import", headers=hd, json=_import(fine, "active", [dict(LINE)]))).status_code == 200


async def test_an_unvoid_never_restores_an_agreement_its_bill_refuses_to_billing(client, owner_ui, monkeypatch):
    """A void agreement that was active stays void on Unvoid, which names New Subscription."""
    freeze_day(monkeypatch, DAY)
    hd, eid = owner_ui.api.headers, f"doc:SUBI-V-{uuid.uuid4().hex[:8]}"
    record = _import(eid, "void", UNTAXED)
    record["data"]["pre_void_status"] = "active"
    assert (await client.post("/docs/import", headers=hd, json=record)).status_code == 200
    assert _key(await client.post(f"/docs/{eid}/unvoid", headers=hd, json={})) == "subscriptions.err_bill_refused_ended"
    assert (await template(client, hd, eid))["status"] == "void"
    listing = await owner_ui.get("/subscriptions?direction=sales")
    assert t("page.new_subscription", "en") in listing.text


async def test_the_line_edit_takes_only_a_paused_agreement_and_only_its_lines(client, owner_ui, monkeypatch):
    """Not paused: refused naming Pause. Paused: a field other than the lines, discount and
    document taxes is refused naming the fields it takes."""
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    eid = await agreement(client, hd, start=DAY)

    async def patch(**fields):
        doc = await template(client, hd, eid)
        return await client.patch(f"/subscriptions/{eid}/lines", headers=hd, json={
            "expected_version": doc["version"], "fields_changed": {f: {"old": doc.get(f), "new": v} for f, v in fields.items()}})

    assert _key(await patch(line_items=[dict(LINE)])) == "subscriptions.err_lines_paused_only"
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    assert _key(await patch(line_items=[dict(LINE)], frequency="weekly")) == "subscriptions.err_lines_fields"
    assert (await patch(line_items=[{**LINE, "quantity": 2}])).status_code == 200
    assert (await template(client, hd, eid))["line_items"][0]["quantity"] == 2
    # The page's save takes the header discount with the lines.
    url = f"/subscriptions/{eid}/lines"
    doc = await template(client, hd, eid)
    saved = await owner_ui.post(url, json={"expected_version": doc["version"], "line_items": doc["line_items"],
                                           "discount": 5, "discount_type": "percentage"})
    assert saved.status_code == 200, saved.text
    doc = await template(client, hd, eid)
    assert (doc["discount"], doc["discount_type"]) == (5, "percentage")


async def test_a_paused_line_edit_keeps_line_ids_and_each_bill_has_its_own(client, owner_ui, session, monkeypatch):
    """A paused agreement's line edit keeps the id of a line it keeps and gives a new line its
    own; each bill is a record of its own, so its lines never share the agreement's ids or
    another period's."""
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    eid = await agreement(client, hd, start="2026-01-01")
    kept = (await template(client, hd, eid))["line_items"][0]["line_id"]
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    doc = await template(client, hd, eid)
    edited = await client.patch(f"/subscriptions/{eid}/lines", headers=hd, json={
        "expected_version": doc["version"], "fields_changed": {"line_items": {
            "old": doc["line_items"], "new": [{**LINE, "quantity": 2}, {**LINE, "description": "Setup"}]}}})
    assert edited.status_code == 200, edited.text
    ids = [li["line_id"] for li in (await template(client, hd, eid))["line_items"]]
    assert ids[0] == kept and ids[1] and ids[1] != kept
    assert (await client.post(f"/subscriptions/{eid}/resume", headers=hd, json={})).status_code == 200
    for _ in range(2):
        assert (await generate_due(client, hd, eid)).status_code == 200
    bills = await billed_docs(session, eid)
    bill_ids = [li["line_id"] for bill in bills for li in bill["line_items"]]
    assert len(bills) == 2 and all(bill_ids)
    assert len(set(bill_ids)) == len(bill_ids) and not set(bill_ids) & set(ids)


@pytest.mark.parametrize("field,value", [("line_items", v) for v in (None, 7, "x", {}, [None], ["x"], [[]], [{**LINE}, 5])]
                         + [("discount", v) for v in ("x", "NaN", None, [], {}, True)], ids=repr)
async def test_the_line_edit_refuses_lines_or_a_discount_it_cannot_read(client, owner_ui, monkeypatch, field, value):
    """Lines that are not a list of lines, or a discount that is not a number, are refused naming
    the field, and nothing is stored: the page still opens and Resume still runs."""
    freeze_day(monkeypatch, DAY)
    hd = owner_ui.api.headers
    eid = await agreement(client, hd, start=DAY)
    assert (await client.post(f"/subscriptions/{eid}/pause", headers=hd)).status_code == 200
    doc = await template(client, hd, eid)
    r = await client.patch(f"/subscriptions/{eid}/lines", headers=hd, json={
        "expected_version": doc["version"], "fields_changed": {field: {"old": doc.get(field), "new": value}}})
    assert r.status_code == 422 and _key(r) == "subscriptions.err_lines_unreadable", r.text
    assert r.json()["detail"]["params"] == {"field": field}
    assert (await template(client, hd, eid))["version"] == doc["version"]
    assert (await owner_ui.get(f"/subscriptions/{eid}")).status_code == 200
    assert (await client.post(f"/subscriptions/{eid}/resume", headers=hd)).status_code == 200


async def test_an_id_holding_a_nul_is_not_found(client, monkeypatch):
    """Postgres stores no NUL, so no agreement has one in its id: refused as not found, never a 500."""
    hd = await register(client)
    for r in (await client.patch("/subscriptions/doc:A%00B/lines", headers=hd, json={"fields_changed": {}}),
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
    assert toast["message"] == t("subscriptions.err_bill_refused_draft", "en", ref=ref,
                                 reason=t("documents.err_finalize_no_lines", "en")), toast
    assert (await owner_ui.api.get(f"/docs/{eid}")).json()["status"] == "draft"
    assert t("btn._add_item", "en") in await _page(owner_ui, eid)
