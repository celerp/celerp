# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Document and List numbers are unique at one boundary: every counter draw skips a number a
Document or List already shows, every number a user or an import gives is refused while another
record shows it, and the census below fails when a new writer reaches numbering another way.
Numbering shows only counters something draws, and a company with the old shared List counter
keeps it for quotation, transfer and untyped Lists: the shared counter stores the List types it
numbers, and no other counter, whoever writes it, changes which counter a List draws."""
from __future__ import annotations

import ast
import pathlib
import time
import uuid

import pytest

import sqlalchemy as sa
from sqlalchemy import event, select, text

from celerp.events.engine import emit_event
from celerp.main import app
from celerp.models.projections import Projection, canonical_number, strip_key, strip_number
from celerp.services.company_lock import locked_company
from celerp_docs.sequences import NUMBER_FIELDS
from test_tax_acceptance_matrix import _demo_company
from fasthtml.common import to_xml
from ui.i18n import refusal_text, t

pytestmark = pytest.mark.asyncio

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _payload(contacts):
    c = next(c for c in contacts if c.get("contact_type") in ("customer", "both"))
    return {"contact_id": c["id"], "line_items": [{"name": "A", "quantity": 1, "unit_price": 100}]}


async def _list(client, h, contacts, list_type):
    r = await client.post("/lists", headers=h, json={"list_type": list_type, **_payload(contacts)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _ref(client, h, entity_id):
    kind = "lists" if entity_id.startswith("list:") else "docs"
    return (await client.get(f"/{kind}/{entity_id}", headers=h)).json()["ref_id"]


async def _quote_doc(client, h, contacts, **extra):
    r = await client.post("/docs", headers=h, json={"doc_type": "quotation", **_payload(contacts), **extra})
    return r


async def _legacy(session, company_id, nxt=7):
    """A 2.8.0 company's stored settings: every List drew the shared LST counter."""
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    seqs = dict(settings.get("sequences") or {})
    seqs["list"] = {"prefix": "LST", "pattern": "{PREFIX}-{YY}{MM}-{####}", "next": nxt}
    settings["sequences"] = seqs
    company.settings = settings
    await session.commit()


async def _rows(client, h):
    return {r["doc_type"]: r for r in (await client.get("/docs/sequences", headers=h)).json()}


# ── One number, one record ──────────────────────────────────────────────────

async def test_a_document_after_a_reset_skips_a_number_a_list_holds(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    held = await _ref(client, h, await _list(client, h, contacts, "quotation"))
    assert (await client.patch("/docs/sequences/quotation", headers=h, json={"next": 1})).status_code == 200
    r = await _quote_doc(client, h, contacts)
    assert r.status_code == 200, r.text
    assert await _ref(client, h, r.json()["id"]) != held


async def test_a_list_skips_the_number_a_renumbered_document_shows(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    doc = (await _quote_doc(client, h, contacts)).json()["id"]
    shown = (await _ref(client, h, doc))[:-4] + "0002"
    assert (await client.post(f"/docs/{doc}/renumber", headers=h, json={"ref_id": shown})).status_code == 200
    assert await _ref(client, h, await _list(client, h, contacts, "quotation")) != shown


async def test_a_document_after_a_reset_skips_the_number_a_renumbered_document_is_stored_under(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    doc = (await _quote_doc(client, h, contacts)).json()["id"]
    stored = doc.split(":", 1)[1]
    assert (await client.post(f"/docs/{doc}/renumber", headers=h, json={"ref_id": stored[:-4] + "0050"})).status_code == 200
    assert (await client.patch("/docs/sequences/quotation", headers=h, json={"next": 1})).status_code == 200
    r = await _quote_doc(client, h, contacts)
    assert r.status_code == 200, r.text
    assert await _ref(client, h, r.json()["id"]) != stored


async def test_the_draw_preview_names_the_invoice_number_the_draw_gives(client, session):
    from memo_draw_support import auth as books_auth, draw, lot, ok, state
    h = await books_auth(session)
    item = await lot(client, h, "PV-1", 1, price=10.0)
    memo = (await ok(await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [
        {"item_id": item, "sku": "PV-1", "description": "PV-1", "quantity": 1, "unit_price": 10.0,
         "sell_by": "piece"}]})))["id"]
    await ok(await client.post(f"/docs/{memo}/finalize", headers=h))
    lids = [li["line_id"] for li in (await state(client, h, memo))["line_items"]]
    await ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": lids}))
    preview = await ok(await draw(client, h, memo, line_ids=lids, dry_run=True))
    assert not preview["problems"], preview["problems"]
    done = await ok(await draw(client, h, memo, line_ids=lids, binding=preview["binding"]))
    assert await _ref(client, h, done["target_doc_id"]) == preview["target"]["ref_id"]


@pytest.mark.parametrize("kind", ["docs", "lists"])
async def test_a_number_too_long_to_store_is_refused_by_name(client, session, kind):
    """A record is stored under its number, so a number longer than the stored key can be is
    refused with the limit, never a failed write."""
    import secrets
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    number = secrets.token_hex(1500)
    body = {"doc_type": "quotation"} if kind == "docs" else {"list_type": "quotation"}
    r = await client.post(f"/{kind}", headers=h, json={**body, "ref_id": number, **_payload(contacts)})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "documents.err_number_too_long"
    assert refusal_text(detail) == t("documents.err_number_too_long", "en", max=100, length=len(number))


@pytest.mark.parametrize("door", ["create doc", "create list", "renumber doc", "patch doc", "patch list"])
async def test_a_number_with_a_nul_character_is_refused_by_name(client, session, door):
    """A record cannot be stored under a number holding a NUL character, so every door that takes
    a number refuses it as invalid, never a failed write, and keeps the number the record had."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    number = "QUO\x00-1"
    if door == "create doc":
        r = await _quote_doc(client, h, contacts, ref_id=number)
    elif door == "create list":
        r = await client.post("/lists", headers=h, json={"list_type": "quotation", "ref_id": number, **_payload(contacts)})
    else:
        entity_id = (await _quote_doc(client, h, contacts)).json()["id"] if door.endswith("doc") \
            else await _list(client, h, contacts, "quotation")
        before = await _ref(client, h, entity_id)
        r = await (client.patch(f"/docs/{entity_id}", headers=h,
                                json={"fields_changed": {"ref_id": {"old": None, "new": number}}})
                   if door == "patch doc" else _renumber(client, h, entity_id, number))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "documents.err_number_nul"
    assert refusal_text(detail) == t("documents.err_number_nul", "en")
    if not door.startswith("create"):
        assert await _ref(client, h, entity_id) == before


@pytest.mark.parametrize("kind", ["docs", "lists"])
async def test_a_number_at_the_limit_is_stored_and_one_past_it_is_refused(client, session, kind):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    body = {"doc_type": "quotation"} if kind == "docs" else {"list_type": "quotation"}
    r = await client.post(f"/{kind}", headers=h, json={**body, "ref_id": "Q" * 100, **_payload(contacts)})
    assert r.status_code == 200, r.text
    r = await client.post(f"/{kind}", headers=h, json={**body, "ref_id": "R" * 101, **_payload(contacts)})
    assert r.status_code == 422 and r.json()["detail"]["params"] == {"max": 100, "length": 101}, r.text


async def test_a_generated_number_past_the_limit_is_refused_and_the_counter_stays(client, session):
    """The counter's own numbers meet the limit too: at next 9 under a 98-character prefix,
    {PREFIX}-{#} gives a 100-character number 9, and number 10 (101 characters) is refused
    with the limit, leaving the counter where it was."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    prefix = "P" * 98
    r = await client.patch("/docs/sequences/quotation", headers=h,
                           json={"prefix": prefix, "pattern": "{PREFIX}-{#}", "next": 9})
    assert r.status_code == 200, r.text
    r = await _quote_doc(client, h, contacts)
    assert r.status_code == 200, r.text
    assert await _ref(client, h, r.json()["id"]) == f"{prefix}-9"
    r = await _quote_doc(client, h, contacts)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "documents.err_number_too_long"
    assert detail["params"] == {"max": 100, "length": 101}
    assert (await _rows(client, h))["quotation"]["next"] == 10


async def test_skipping_held_numbers_past_the_limit_is_refused_and_the_preview_says_so(client, session):
    """Numbers already held are skipped; when the skip reaches a number too long to store, the
    next document is refused with the limit, the counter does not move, and the Numbering row
    shows the refusal instead of a number nothing can be stored under."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    prefix = "P" * 97
    for n in (98, 99):
        assert (await _quote_doc(client, h, contacts, ref_id=f"{prefix}-{n}")).status_code == 200
    r = await client.patch("/docs/sequences/quotation", headers=h,
                           json={"prefix": prefix, "pattern": "{PREFIX}-{#}", "next": 98})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["preview"] is None, row
    assert row["refusal"]["message_key"] == "documents.err_number_too_long"
    assert row["refusal"]["params"] == {"max": 100, "length": 101}
    r = await _quote_doc(client, h, contacts)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["params"] == {"max": 100, "length": 101}
    assert (await _rows(client, h))["quotation"]["next"] == 98


async def test_the_numbering_row_shows_a_refused_preview_as_its_message():
    from ui.routes.settings_sales import _numbering_row
    from celerp.accounting_roles import catalog_refusal
    refusal = catalog_refusal("documents.err_number_too_long", max=100, length=101)
    html = to_xml(_numbering_row("quotation", {"prefix": "P", "pattern": "{PREFIX}-{#}", "next": 98,
                                               "preview": None, "refusal": refusal}))
    assert refusal_text(refusal) in html
    assert "numbering-preview" not in html


async def test_a_document_cannot_be_renumbered_to_a_number_a_list_holds(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    held = await _ref(client, h, await _list(client, h, contacts, "quotation"))
    doc = (await _quote_doc(client, h, contacts)).json()["id"]
    for r in (await client.post(f"/docs/{doc}/renumber", headers=h, json={"ref_id": held}),
              await client.post(f"/docs/{doc}/renumber", headers=h, json={"ref_id": held.lower()}),
              await client.patch(f"/docs/{doc}", headers=h,
                                 json={"fields_changed": {"ref_id": {"old": None, "new": held}}})):
        assert r.status_code == 409, r.text
        assert refusal_text(r.json()["detail"]) == t("documents.err_list_number_taken", "en", number=held)


async def _renumber(client, h, entity_id, number):
    """Renumber a Document through the renumber endpoint, or a draft List through its patch."""
    if entity_id.startswith("list:"):
        return await client.patch(f"/lists/{entity_id}", headers=h,
                                  json={"fields_changed": {"ref_id": {"old": None, "new": number}}})
    return await client.post(f"/docs/{entity_id}/renumber", headers=h, json={"ref_id": number})


async def _moved_away(client, h, entity_id):
    """Renumber a record away from the number it is stored under; returns that number."""
    stored = entity_id.split(":", 1)[1]
    r = await _renumber(client, h, entity_id, stored[:-4] + "0050")
    assert r.status_code == 200, r.text
    return stored


@pytest.mark.parametrize("holder_kind,taker_kind", [("doc", "doc"), ("list", "doc"), ("doc", "list")])
async def test_a_record_cannot_be_renumbered_to_the_number_another_record_is_stored_under(
        client, session, holder_kind, taker_kind):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]

    async def record(kind):
        return (await _quote_doc(client, h, contacts)).json()["id"] if kind == "doc" \
            else await _list(client, h, contacts, "quotation")

    holder = await record(holder_kind)
    stored = await _moved_away(client, h, holder)
    shown = await _ref(client, h, holder)
    key = "documents.err_doc_number_taken" if holder_kind == "doc" else "documents.err_list_number_taken"
    taker = await record(taker_kind)
    refused = [await _renumber(client, h, taker, stored)]
    if taker_kind == "doc":
        refused.append(await client.patch(f"/docs/{taker}", headers=h,
                                          json={"fields_changed": {"ref_id": {"old": None, "new": stored}}}))
    for r in refused:
        assert r.status_code == 409, r.text
        assert refusal_text(r.json()["detail"]) == t(key, "en", number=shown)


@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_a_record_can_be_renumbered_back_to_its_own_number_or_to_a_free_one(client, session, kind):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    entity_id = (await _quote_doc(client, h, contacts)).json()["id"] if kind == "doc" \
        else await _list(client, h, contacts, "quotation")
    stored = await _moved_away(client, h, entity_id)
    assert (await _renumber(client, h, entity_id, stored)).status_code == 200
    assert await _ref(client, h, entity_id) == stored
    assert (await _renumber(client, h, entity_id, stored[:-4] + "0077")).status_code == 200
    assert await _ref(client, h, entity_id) == stored[:-4] + "0077"


async def test_a_number_given_on_create_is_refused_while_the_other_kind_shows_it(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    held = await _ref(client, h, await _list(client, h, contacts, "quotation"))
    r = await _quote_doc(client, h, contacts, ref_id=held)
    assert r.status_code == 409, r.text
    assert refusal_text(r.json()["detail"]) == t("documents.err_list_number_taken", "en", number=held)
    doc_ref = await _ref(client, h, (await _quote_doc(client, h, contacts)).json()["id"])
    r = await client.post("/lists", headers=h, json={"list_type": "transfer", "ref_id": doc_ref})
    assert r.status_code == 409, r.text
    assert refusal_text(r.json()["detail"]) == t("documents.err_doc_number_taken", "en", number=doc_ref)


async def test_an_imported_list_is_refused_a_number_a_document_shows(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    doc_ref = await _ref(client, h, (await _quote_doc(client, h, contacts)).json()["id"])
    r = await client.post("/lists/import", headers=h, json={
        "entity_id": "list:imported-1", "event_type": "list.created", "idempotency_key": "imp-1", "source": "csv",
        "data": {"ref_id": doc_ref, "list_type": "quotation", "status": "draft"}})
    assert r.status_code == 409, r.text
    assert refusal_text(r.json()["detail"]) == t("documents.err_doc_number_taken", "en", number=doc_ref)


# ── The old shared List counter (released 2.8.0) ───────────────────────────

async def test_legacy_quotation_and_untyped_lists_stay_on_the_shared_counter(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    await _legacy(session, auth["company_id"])
    first = await _ref(client, h, await _list(client, h, contacts, "quotation"))
    doc = await _ref(client, h, (await _quote_doc(client, h, contacts)).json()["id"])
    second = await _ref(client, h, await _list(client, h, contacts, "quotation"))
    untyped = await _ref(client, h, await _list(client, h, contacts, None))
    transfer = await _ref(client, h, await _list(client, h, contacts, "transfer"))
    audit = await _ref(client, h, await _list(client, h, contacts, "audit"))
    assert doc.startswith("QUO-")
    assert [first[:4], second[:4], untyped[:4], transfer[:4]] == ["LST-"] * 4, (first, second, untyped, transfer)
    assert audit.startswith("AUD-")


async def test_legacy_numbering_rows_preview_what_each_record_takes(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    assert "list" not in await _rows(client, h), "a new company draws nothing from the shared counter"
    await _legacy(session, auth["company_id"])
    rows = await _rows(client, h)
    assert rows["transfer"]["preview"] == rows["list"]["preview"], "transfers draw the shared counter"
    assert await _ref(client, h, (await _quote_doc(client, h, contacts)).json()["id"]) == rows["quotation"]["preview"]
    assert await _ref(client, h, await _list(client, h, contacts, "quotation")) == rows["list"]["preview"]
    transfer = (await _rows(client, h))["transfer"]["preview"]
    assert await _ref(client, h, await _list(client, h, contacts, "transfer")) == transfer


async def test_a_list_document_on_a_new_company_moves_no_list_type(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={"doc_type": "list", **_payload(contacts)})
    assert r.status_code == 200, r.text
    assert (await _ref(client, h, r.json()["id"])).startswith("LST-")
    rows = await _rows(client, h)
    quotation = await _ref(client, h, await _list(client, h, contacts, "quotation"))
    untyped = await _ref(client, h, await _list(client, h, contacts, None))
    transfer = await _ref(client, h, await _list(client, h, contacts, "transfer"))
    assert [quotation[:4], untyped[:4], transfer[:4]] == ["QUO-", "QUO-", "TRF-"], (quotation, untyped, transfer)
    assert transfer == rows["transfer"]["preview"]


async def test_a_transfer_document_keeps_legacy_transfer_lists_on_the_shared_counter(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    await _legacy(session, auth["company_id"])
    r = await client.post("/docs", headers=h, json={"doc_type": "transfer", **_payload(contacts)})
    assert r.status_code == 200, r.text
    assert (await _ref(client, h, r.json()["id"])).startswith("TRF-")
    assert (await _ref(client, h, await _list(client, h, contacts, "transfer"))).startswith("LST-")
    # Saving the transfer row is what gives transfers their own counter.
    assert (await client.patch("/docs/sequences/transfer", headers=h, json={"prefix": "XFR"})).status_code == 200
    assert (await _ref(client, h, await _list(client, h, contacts, "transfer"))).startswith("XFR-")
    assert (await _ref(client, h, await _list(client, h, contacts, "quotation"))).startswith("LST-")


# ── Numbering refusals are keyed ────────────────────────────────────────────

@pytest.mark.parametrize("doc_type, body, key", [
    ("audit", {"pattern": "{PREFIX}-{YY}{MM}"}, "documents.err_numbering_no_sequence_token"),
    ("audit", {"pattern": "{PREFIX}-{FOO}-{####}"}, "documents.err_numbering_unknown_tokens"),
    ("audit", {"prefix": "A{X}"}, "documents.err_numbering_prefix_braces"),
    ("bogus", {"prefix": "B"}, "documents.err_numbering_unknown_type"),
    ("audit", {"prefix": "P" * 101}, "documents.err_number_too_long"),
    ("audit", {"next": 10 ** 101}, "documents.err_number_too_long"),
])
async def test_every_numbering_refusal_reaches_the_reader_in_their_language(client, session, doc_type, body, key):
    from ui.i18n import set_lang
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    before = await _rows(client, h)
    r = await client.patch(f"/docs/sequences/{doc_type}", headers=h, json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key
    set_lang("th")
    try:
        # The refusal reads as a sentence, so its first letter may be raised.
        assert refusal_text(detail).lower() == t(key, "th", **detail.get("params", {})).lower()
    finally:
        set_lang("en")
    assert await _rows(client, h) == before


# ── The preview is the number given, read in bounded work ──────────────────

async def _held_run(client, session, h, contacts, count):
    """``count`` quotations, then the quotation counter reset to 1: its first ``count`` numbers are held."""
    for _ in range(count):
        assert (await _quote_doc(client, h, contacts)).status_code == 200
    assert (await client.patch("/docs/sequences/quotation", headers=h, json={"next": 1})).status_code == 200


async def test_after_a_reset_the_preview_is_the_number_the_next_document_gets(client, session):
    from celerp.models.company import Company
    from celerp_docs.sequences import peek_draft_ref
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    await _held_run(client, session, h, contacts, 20)
    preview = (await _rows(client, h))["quotation"]["preview"]
    assert preview.endswith("-0021"), preview
    session.expire_all()
    assert await peek_draft_ref(session, await session.get(Company, auth["company_id"]), "quotation") == preview
    assert await _ref(client, h, (await _quote_doc(client, h, contacts)).json()["id"]) == preview


async def test_a_long_run_of_held_numbers_costs_a_few_reads_under_the_lock(client, session):
    from celerp.models.company import Company
    from celerp_docs.sequences import claim_number
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    await _held_run(client, session, h, contacts, 40)
    session.expire_all()
    company = await locked_company(session, auth["company_id"])
    reads = []
    engine = session.bind.sync_engine

    def count(conn, cursor, statement, parameters, *_):
        if "projections" in statement:
            reads.append((statement, parameters))
    event.listen(engine, "before_cursor_execute", count)
    try:
        ref = await claim_number(session, company, "quotation")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    await session.rollback()
    assert ref.endswith("-0041")
    assert len(reads) <= 3, f"{len(reads)} reads for 40 held numbers"
    # Each read, as claim_number sent it, looks the numbers up through their indexes on a company
    # with many records, never by scanning its records. The records are added and taken away
    # again in one transaction.
    await session.execute(text(
        "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
        "SELECT :company, 'doc:OLD-' || n, 'doc', json_build_object('ref_id', 'OLD-' || n, "
        "'doc_number', 'KEPT-' || n), 1, now() FROM generate_series(1, 5000) AS n"),
        {"company": auth["company_id"]})
    await session.execute(text("ANALYZE projections"))
    conn = await session.connection()
    plans = ["\n".join(r[0] for r in await conn.exec_driver_sql("EXPLAIN " + sql, params)) for sql, params in reads]
    await session.rollback()
    plan = "\n".join(plans)
    for index in ("ix_projections_canonical_number_shown", "ix_projections_canonical_number_kept",
                  "ix_projections_canonical_number_key"):
        assert index in plan, plan


# ── Overlap refusal ─────────────────────────────────────────────────────────

async def test_a_counter_cannot_take_the_format_of_a_list_counter_on_its_default(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    r = await client.patch("/docs/sequences/quotation", headers=h, json={"prefix": "SHIP"})
    assert r.status_code == 422, r.text
    assert (await _rows(client, h))["quotation"]["prefix"] == "QUO"


async def test_the_overlap_refusal_is_in_the_users_language_and_names_the_type(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    assert (await client.patch("/docs/sequences/transfer", headers=h, json={"prefix": "QX"})).status_code == 200
    r = await client.patch("/docs/sequences/audit", headers=h, json={"prefix": "QX"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "documents.err_numbering_overlap"
    from ui.i18n import doc_type_label, set_lang
    set_lang("th")
    try:
        text = refusal_text(detail)
        assert text == t("documents.err_numbering_overlap", "th", doc_type=doc_type_label("transfer"))
        assert doc_type_label("transfer") in text and "transfer" not in text
    finally:
        set_lang("en")


# ── A List's number follows its type through the UI path ──────────────────

async def test_a_draft_list_changed_to_another_type_takes_that_types_number(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    eid = await _list(client, h, contacts, "quotation")
    preview = (await _rows(client, h))["writeoff"]["preview"]
    r = await client.post(f"/lists/{eid}/change-type", headers=h, json={"list_type": "writeoff"})
    assert r.status_code == 200, r.text
    assert r.json()["ref_id"] == preview == await _ref(client, h, eid)
    assert (await _rows(client, h))["writeoff"]["preview"] != preview


# ── One number: padded on the way in, held however it was stored ───────────

def _imported(kind: str, key: str, number: str | None, field: str = "ref_id") -> dict:
    data = {"list_type": "quotation"} if kind == "list" else {"doc_type": "quotation"}
    return {"entity_id": key, "event_type": f"{kind}.created", "idempotency_key": f"k-{uuid.uuid4().hex}",
            "source": "csv", "data": {**data, "status": "draft", **({field: number} if number else {})}}


async def _target(client, h, contacts, door: str) -> str | None:
    """The record a patch or renumber door gives a new number to."""
    if door in ("patch doc", "renumber doc"):
        return (await _quote_doc(client, h, contacts)).json()["id"]
    return await _list(client, h, contacts, "quotation") if door == "patch list" else None


async def _give(client, h, contacts, door: str, number: str | None, *, target: str | None = None,
                key: str | None = None, field: str = "ref_id") -> tuple[str, str | None]:
    """Give a record ``number`` in ``field`` through ``door`` (a patch or renumber door: to
    ``target``): (the record's id, the refusal's message key or None). An import is stored under
    ``key`` when given."""
    kind = "list" if "list" in door else "doc"
    body = {"fields_changed": {field: {"old": None, "new": number}}}
    if door in ("patch doc", "renumber doc"):
        eid = target
        r = await (client.patch(f"/docs/{eid}", headers=h, json=body) if door == "patch doc" else
                   client.post(f"/docs/{eid}/renumber", headers=h, json={field: number}))
    elif door == "patch list":
        eid = target
        r = await client.patch(f"/lists/{eid}", headers=h, json=body)
    elif door == "create doc":
        r = await _quote_doc(client, h, contacts, **{field: number})
        eid = r.json().get("id")
    elif door == "create list":
        r = await client.post("/lists", headers=h,
                              json={"list_type": "quotation", **_payload(contacts), field: number})
        eid = r.json().get("id")
    else:
        eid = key or f"{kind}:imp-{uuid.uuid4().hex[:8]}"
        record = _imported(kind, eid, number, field)
        if door.endswith("batch"):
            r = await client.post(f"/{kind}s/import/batch", headers=h, json={"records": [record]})
            assert r.status_code == 200, r.text
            errors = r.json()["errors"]
            # A batch row gives its refusal as English text: the key whose message it begins with.
            return eid, (next((k for k in ("err_doc_number_taken", "err_list_number_taken")
                               if t(f"documents.{k}", "en", number="\0").split("\0")[0] in errors[0]),
                              errors[0]) if errors else None)
        r = await client.post(f"/{kind}s/import", headers=h, json=record)
    if r.status_code == 200:
        return eid, None
    detail = r.json()["detail"]
    # A body the route's schema refuses is refused before any number is read.
    return eid, detail["message_key"].split(".")[-1] if isinstance(detail, dict) else f"schema {r.status_code}"


# Every /docs and /lists route that writes a number, as the app's router lists it: (method, path)
# -> the door _give sends it through.
_ROUTE_DOORS = {
    ("post", "/docs"): "create doc", ("post", "/lists"): "create list",
    ("patch", "/docs/{entity_id}"): "patch doc", ("patch", "/lists/{entity_id}"): "patch list",
    ("post", "/docs/{entity_id}/renumber"): "renumber doc",
    ("post", "/docs/import"): "import doc", ("post", "/docs/import/batch"): "import doc batch",
    ("post", "/lists/import"): "import list", ("post", "/lists/import/batch"): "import list batch",
}
_DOORS = tuple(_ROUTE_DOORS.values())
# The body fields through which a route reaches a record's numbers: a patch reaches every number
# field, an import every number field and the key the record is stored under.
_KEY_FIELDS = ("key", "key without colon")
_REACHES = {"fields_changed": NUMBER_FIELDS, "data": (*NUMBER_FIELDS, *_KEY_FIELDS),
            "records": (*NUMBER_FIELDS, *_KEY_FIELDS), **{f: (f,) for f in NUMBER_FIELDS}}


def _number_routes() -> dict[tuple[str, str], tuple[str, ...]]:
    """(method, path) -> the numbers it reaches, for every /docs or /lists write route whose
    body reaches one, read from the app's OpenAPI schema (FastAPI's own route walk)."""
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    routes = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            fields = schemas[body["$ref"].split("/")[-1]] if "$ref" in body else body
            reached = [f for p in fields.get("properties", {}) for f in _REACHES.get(p, ())]
            if path.startswith(("/docs", "/lists")) and method in ("post", "put", "patch") and reached:
                routes[method, path] = tuple(dict.fromkeys(reached))
    return routes


_NUMBER_ROUTES = _number_routes()
# A record an earlier version saved under its key as sent, through the event the import wrote.
_EARLIER = "written before numbers were trimmed"

# One number in hostile spellings: name -> (counter prefix or None, the number as sent, its
# canonical twin). Each is saved as the number it means, held against its twin, and skipped by
# the counter.
HOSTILE = {
    "space": (None, " {n} ", "{n}"),
    "tab": (None, "\t{n}\t", "{n}"),
    "LF": (None, "\n{n}\n", "{n}"),
    "NBSP": (None, " {n} ", "{n}"),
    "U+2007": (None, " {n} ", "{n}"),
    "U+3000": (None, "　{n}　", "{n}"),
    "dotted capital I": ("İRS", "{n}", "{n}"),
    "final sigma": ("ΟΔΟΣ", "{n}", "{n}"),
}


async def test_every_route_that_writes_a_number_is_a_door_the_hostile_set_goes_through():
    assert set(_NUMBER_ROUTES) == set(_ROUTE_DOORS), set(_NUMBER_ROUTES) ^ set(_ROUTE_DOORS)


@pytest.mark.parametrize("sent, twin", [v[1:] for v in HOSTILE.values()], ids=list(HOSTILE))
async def test_a_hostile_spelling_and_its_twin_are_one_number(session, sent, twin):
    """Both spellings of a number are one number: stored stripped (strip_number), the database
    reads them in one case."""
    n = "Qİ-ΟΣ-1"
    one = canonical_number(sa.literal(strip_number(sent.format(n=n)), sa.Text))
    other = canonical_number(sa.literal(twin.format(n=n), sa.Text))
    same, kept = (await session.execute(select(one == other, one))).one()
    assert same and kept == strip_number(kept) and strip_number(sent.format(n=n)) == twin.format(n=n)


async def _seed_earlier(session, company_id, kind: str, field: str, sent: str) -> str:
    """A record an earlier version saved, as the upgrade leaves it: the import's event under the
    key as sent (a key is never rewritten) with its numbers stripped (r1s2t3u4v5w6, whose
    rewrite of padded numbers test_r1s2t3u4v5w6_projections_canonical_number_lookup covers)."""
    key = (sent if field == "key without colon" else f"{kind}:{sent}" if field == "key"
           else f"{kind}:old-{uuid.uuid4().hex[:6]}")
    data = {**_imported(kind, key, None)["data"], **({field: strip_number(sent)} if field in NUMBER_FIELDS else {})}
    await emit_event(session, company_id=company_id, entity_id=key, entity_type=kind,
                     event_type=f"{kind}.created", data=data, actor_id=None, location_id=None,
                     source="csv", idempotency_key=f"k-{uuid.uuid4().hex}")
    await session.commit()
    return key


# A storage key is never rewritten, so one an earlier version saved padded with a space past
# ASCII is matched only once that space is gone (canonical_number): those cases are left out.
@pytest.mark.parametrize("door, field, hostile", [
    (d, f, h) for (m, p), fields in _NUMBER_ROUTES.items()
    for d in [_ROUTE_DOORS.get((m, p), f"{m} {p}")] for f in fields for h in HOSTILE
] + [(_EARLIER, f, h) for f in (*NUMBER_FIELDS, *_KEY_FIELDS) for h in HOSTILE
     if f in NUMBER_FIELDS or HOSTILE[h][1].isascii()])
async def test_a_number_in_a_hostile_spelling_is_one_number_through_every_door(client, session, door, field, hostile):
    """A number sent through any door in any field, spelled with spaces of any kind around it or
    with letters whose lower case differs between Python and the database, is saved as the number
    it means, the next record given its twin is refused, and the counter skips it. A record an
    earlier version saved with its spaces is held the same."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    prefix, sent, twin = HOSTILE[hostile]
    if prefix:
        assert (await client.patch("/docs/sequences/quotation", headers=h, json={"prefix": prefix})).status_code == 200
    target = await _target(client, h, contacts, door)
    n = (await _rows(client, h))["quotation"]["preview"]
    kind = "list" if "list" in door else "doc"
    sent = sent.format(n=n)
    if door == _EARLIER:
        eid = await _seed_earlier(session, auth["company_id"], kind, field, sent)
    else:
        key = sent if field == "key without colon" else f"{kind}:{sent}" if field == "key" else None
        eid, refused = await _give(client, h, contacts, door, None if key else sent, target=target, key=key,
                                   field="ref_id" if key else field)
        assert refused is None, refused
        if key:  # stored under the key with its number stripped
            assert (await client.get(f"/{kind}s/{strip_key(key)}", headers=h)).status_code == 200
        if not key:
            state = (await client.get(f"/{kind}s/{eid}", headers=h)).json()
            assert state[field] == strip_number(sent), state[field]
    r = await _quote_doc(client, h, contacts, ref_id=twin.format(n=n))
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == f"documents.err_{kind}_number_taken", r.text
    shown = r.json()["detail"]["params"]["number"]
    assert shown and shown == strip_number(shown), shown
    assert (await _rows(client, h))["quotation"]["preview"] != n


@pytest.mark.parametrize("door", _DOORS)
async def test_a_number_that_is_not_text_is_refused_and_a_blank_one_is_stored_empty(client, session, door):
    """A number that is not text is refused through every door. A number of nothing but spaces
    is stored as empty by an import or a patch, held by no record, and refused by name where a
    record is created or renumbered to it."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    kind = "list" if "list" in door else "doc"
    _, refused = await _give(client, h, contacts, door, 12345, target=await _target(client, h, contacts, door))
    assert refused in ("err_number_required", "schema 422") or refused.endswith(": Enter a document number."), refused
    for _ in range(2):
        eid, refused = await _give(client, h, contacts, door, "\u3000\t ", target=await _target(client, h, contacts, door))
        if door.startswith(("create", "renumber")):
            assert refused == "err_number_required", refused
        else:
            assert refused is None, refused
            assert (await client.get(f"/{kind}s/{eid}", headers=h)).json()["ref_id"] == "", door


@pytest.mark.parametrize("door", [d for d in _DOORS if d.startswith("import")])
async def test_an_import_stored_under_a_key_another_record_holds_is_refused(client, session, door):
    """The number inside an import's storage key counts as the numbers it shows do: a key equal,
    in another case, to the key another record is stored under, or to the number another record
    shows, is refused with the message every other door gives."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    kind = "list" if "list" in door else "doc"
    stored = (await _quote_doc(client, h, contacts)).json()["id"].split(":", 1)[1]
    shown_by = (await _quote_doc(client, h, contacts)).json()["id"]
    shown = f"SHOWN-{uuid.uuid4().hex[:6]}"
    assert (await client.post(f"/docs/{shown_by}/renumber", headers=h, json={"ref_id": shown})).status_code == 200
    for key in (f"{kind}:{stored.lower()}", f"{kind}:{shown.lower()} "):
        eid, refused = await _give(client, h, contacts, door, f"IMP-{uuid.uuid4().hex[:6]}", key=key)
        assert refused == "err_doc_number_taken", (key, refused)
        assert (await client.get(f"/{kind}s/{eid}", headers=h)).status_code == 404


@pytest.mark.parametrize("door", [d for d in _DOORS if d.startswith("import")])
async def test_an_import_under_a_key_whose_number_is_free_is_saved(client, session, door):
    """A storage key holds the number after its first colon, or the whole key when it has none:
    an import under any key whose number no other record holds is saved."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    kind = "list" if "list" in door else "doc"
    for key in (f"{kind}-imp-{uuid.uuid4().hex[:6]}", f"{kind}:", f"other:{uuid.uuid4().hex[:6]}"):
        eid, refused = await _give(client, h, contacts, door, f"IMP-{uuid.uuid4().hex[:6]}", key=key)
        assert refused is None, (key, refused)
        assert (await client.get(f"/{kind}s/{eid}", headers=h)).status_code == 200, key


# ── Census: every door into numbering goes through the boundary ────────────

_DRAWS = {"next_doc_ref", "next_draft_ref", "_next_free_list_ref", "claim_number"}
_NUMBERED = {"doc.created", "list.created", "doc.renumbered", "DOC_CREATED", "LIST_CREATED", "DOC_RENUMBERED"}
_BOUNDARY = {"claim_number", "require_number_free", "free_number", "free_import_numbers", "free_patched_numbers"}
_FIELD_ROOTS = ("default_modules", "celerp/importers")
# A writer handed a number its caller claimed: (file, function) -> the caller that claims it.
_CLAIMED_BY_CALLER = {
    ("default_modules/celerp-docs/celerp_docs/consignment_buy.py", "buy_consignment"): "convert_doc",
}
# A writer that builds a record and hands it to a writer that checks its numbers:
# (file, function) -> (file, function) of that writer.
_HANDED = {
    ("default_modules/celerp-docs/celerp_docs/migration_sink.py", "_doc_record"): (
        "default_modules/celerp-docs/celerp_docs/import_service.py", "create_imported_doc"),
}
# A write of a number field that gives no Document or List a number, by reason.
_NOT_A_RECORD_NUMBER = {
    "an item event or journal entry naming the document it came from": {
        ("celerp-docs/celerp_docs/routes.py", "record_historical_delivery"),
        ("celerp-docs/celerp_docs/routes.py", "_set_lot_status"),
        ("celerp-docs/celerp_docs/routes.py", "_return_to_source_memo"),
        ("celerp-docs/celerp_docs/routes.py", "_fulfill_lines_impl"),
        ("celerp-docs/celerp_docs/routes.py", "_reverse_whole_lines"),
        ("celerp-docs/celerp_docs/routes.py", "write_off_stock"),
        ("celerp-inventory/celerp_inventory/routes.py", "undo_merge"),
        ("celerp-inventory/celerp_inventory/services.py", "restate_item_cost"),
        ("celerp-inventory/celerp_inventory/services.py", "cost_correction_notice"),
        ("celerp-inventory/celerp_inventory/services.py", "_sale_of_lot"),
        ("celerp-inventory/celerp_inventory/projections.py", "_apply_item_event"),
    },
    "a projection storing the number its event carries": {
        ("celerp-docs/celerp_docs/doc_projections.py", "apply_documents_event"),
        ("celerp-docs/celerp_docs/received.py", "apply_received_event"),
    },
    "a row, page, email or platform payload built to show or send a record": {
        ("celerp-admin/celerp_admin/routes.py", "_check_missing_jes"),
        ("celerp-admin/celerp_admin/routes.py", "_check_inverted_doc_dates"),
        ("celerp-admin/celerp_admin/routes.py", "_check_fractional_piece_quantities"),
        ("celerp-docs/celerp_docs/doc_money.py", "memo_draws"),
        ("celerp-docs/celerp_docs/doc_money.py", "memo_fixed_amounts"),
        ("celerp-docs/celerp_docs/doc_service.py", "_invoice_for_push"),
        ("celerp-docs/celerp_docs/routes.py", "_attach_memo_takes"),
        ("celerp-docs/celerp_docs/routes.py", "doc_email"),
        ("celerp-docs/celerp_docs/routes.py", "_list_columns"),
        ("celerp-docs/celerp_docs/routes.py", "_draw_bill"),
        ("celerp-docs/celerp_docs/routes.py", "_draw_preview"),
        ("celerp-docs/celerp_docs/routes.py", "send_list"),
        ("celerp-fixed-assets/celerp_fixed_assets/ui_physical.py", "_files_card"),
        ("celerp-manufacturing/celerp_manufacturing/routes.py", "_compute_to_make"),
        ("celerp-manufacturing/celerp_manufacturing/routes.py", "item_manufacturing_hub"),
        ("celerp-subscriptions/celerp_subscriptions/billing.py", "_result"),
        ("celerp-subscriptions/celerp_subscriptions/billing.py", "_listed_row"),
        ("celerp-subscriptions/celerp_subscriptions/billing.py", "period_list"),
        ("celerp-subscriptions/celerp_subscriptions/summary.py", "_rows"),
    },
    "a number a connected platform gave its record, kept as given: such a platform may give two records one number": {
        ("celerp-docs/celerp_docs/doc_service.py", "upsert_order_from_shopify"),
        ("celerp-docs/celerp_docs/doc_service.py", "upsert_invoice_from_quickbooks"),
        ("celerp-docs/celerp_docs/doc_service.py", "upsert_invoice_from_xero"),
        ("celerp-docs/celerp_docs/doc_service.py", "upsert_order_from_woocommerce"),
    },
    "a bill taking back the number of the order it was, which no other record holds (number_holder)": {
        ("celerp-docs/celerp_docs/routes.py", "revert_doc_to_draft"),
    },
    "a record keeping the number it already has": {
        ("celerp-docs/celerp_docs/routes.py", "finalize_document"),
        ("celerp-docs/celerp_docs/routes.py", "change_list_type"),
    },
}


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def _own_nodes(fn):
    todo = list(ast.iter_child_nodes(fn))
    while todo:
        node = todo.pop()
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
            todo.extend(ast.iter_child_nodes(node))


def _app_functions(roots=("celerp", "default_modules", "ui")):
    """(file, function node) for every function of the app under ``roots``, tests left out."""
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO)
        if not any(str(rel).startswith(f"{r}/") for r in roots) or any(
                p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.parts):
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield str(rel), fn


def _field_writes(fn) -> list[tuple[str, int, ast.expr]]:
    """(field, line, value) for every number field (NUMBER_FIELDS) ``fn`` writes: as a dict
    key, a subscript store or a keyword argument."""
    found = []
    for n in _own_nodes(fn):
        if isinstance(n, ast.Dict):
            found += [(k.value, n.lineno, v) for k, v in zip(n.keys, n.values)
                      if isinstance(k, ast.Constant) and k.value in NUMBER_FIELDS]
        elif isinstance(n, ast.Assign):
            found += [(t_.slice.value, n.lineno, n.value) for t_ in n.targets if isinstance(t_, ast.Subscript)
                      and isinstance(t_.slice, ast.Constant) and t_.slice.value in NUMBER_FIELDS]
        elif isinstance(n, ast.Call):
            found += [(k.arg, n.lineno, k.value) for k in n.keywords if k.arg in NUMBER_FIELDS]
    return found


def _checked(fn) -> set[str]:
    """The names and subscripts in ``fn`` that hold a number the boundary returned, followed
    through plain assignments."""
    assigns = [n for n in _own_nodes(fn) if isinstance(n, ast.Assign)]
    held: set[str] = set()
    while True:
        grown = held | {ast.unparse(t_) for n in assigns if _from_boundary(n.value, held) for t_ in n.targets}
        if grown == held:
            return held
        held = grown


def _from_boundary(value: ast.expr, held: set[str]) -> bool:
    return any((isinstance(n, ast.Call) and _name(n.func) in _BOUNDARY)
               or (isinstance(n, (ast.Name, ast.Subscript)) and ast.unparse(n) in held) for n in ast.walk(value))


def _disposition(rel: str, fn) -> str | None:
    """Why ``fn`` may write a number field it did not take from the boundary; None when nothing says."""
    if (rel, fn.name) in _CLAIMED_BY_CALLER:
        return f"claimed by its caller {_CLAIMED_BY_CALLER[rel, fn.name]}"
    if (rel, fn.name) in _HANDED:
        return f"handed to {_HANDED[rel, fn.name][1]}"
    short = rel.removeprefix("default_modules/")
    return next((why for why, where in _NOT_A_RECORD_NUMBER.items() if (short, fn.name) in where), None)


def _field_census() -> list[tuple[str, str, str, int, str | None]]:
    """(field, file, function, line, disposition) for every write of a number field under
    default_modules and celerp/importers: "boundary" when the value written is one the boundary
    returned, otherwise the stated reason, or None."""
    rows = []
    for rel, fn in _app_functions(_FIELD_ROOTS):
        held = _checked(fn)
        for field, line, value in _field_writes(fn):
            how = "boundary" if _from_boundary(value, held) else _disposition(rel, fn)
            rows.append((field, rel, fn.name, line, how))
    return rows


def _census() -> list[tuple[str, str, str, str, set[str]]]:
    """(kind, file, function, what, boundary calls) for every counter draw, every event that gives
    a Document or List its number and every writer of a user's field patch."""
    rows = []
    for rel, fn in _app_functions():
        calls = [n for n in _own_nodes(fn) if isinstance(n, ast.Call)]
        via = {_name(c.func) for c in calls} & _BOUNDARY
        for call in calls:
            what = _name(call.func)
            tags = [a.value if isinstance(a, ast.Constant) else _name(a)
                    for a in [*call.args, *(k.value for k in call.keywords)]]
            if what in _DRAWS:
                rows.append(("DRAW", rel, fn.name, what, via))
            if what in {"emit_event", "_emit_list"} and set(tags) & _NUMBERED:
                rows.append(("NUMBER", rel, fn.name, what, via))
            if what == "_refuse_protected_fields":  # a writer of a user's field patch, any number field included
                rows.append(("PATCH", rel, fn.name, what, via))
    return rows


async def test_every_counter_draw_goes_through_claim_number():
    rows = _census()
    assert any(r[0] == "DRAW" for r in rows), "the census found no counter draw"
    stray = [r[:4] for r in rows if r[0] == "DRAW" and r[3] != "claim_number"
             and not (r[2] == "claim_number" and r[3] == "next_doc_ref")]
    assert not stray, f"a counter is drawn outside claim_number, so the number may be taken: {stray}"


async def test_every_numbered_writer_goes_through_the_boundary():
    rows = [r for r in _census() if r[0] in {"NUMBER", "PATCH"}]
    assert len(rows) >= 15 and {r[2] for r in rows if r[0] == "PATCH"} >= {"write_doc_patch", "write_list_patch"}, rows
    stray = [r[:4] for r in rows if not r[4] and (r[1], r[2]) not in _CLAIMED_BY_CALLER]
    assert not stray, f"a writer gives a number without claim_number or require_number_free: {stray}"
    unpatched = [r[:4] for r in rows if r[0] == "PATCH" and "free_patched_numbers" not in r[4]]
    assert not unpatched, f"a patch writer leaves a number field unchecked: {unpatched}"
    callers = {r[2] for r in _census() if r[4]}
    assert set(_CLAIMED_BY_CALLER.values()) <= callers


async def test_every_write_of_a_number_field_takes_the_number_the_boundary_returns():
    rows = _field_census()
    assert {r[0] for r in rows} == set(NUMBER_FIELDS) and sum(r[4] == "boundary" for r in rows) >= 20, rows
    stray = [r[:4] for r in rows if r[4] is None]
    assert not stray, f"a number field is written without the boundary or a stated reason: {stray}"
    named = {(r[1], r[2]) for r in rows if r[4] not in (None, "boundary")}
    mapped = {*_CLAIMED_BY_CALLER, *_HANDED, *(("default_modules/" + f, fn) for where in
                                                _NOT_A_RECORD_NUMBER.values() for f, fn in where)}
    assert mapped <= named, f"a census reason names a writer that writes no number field: {mapped - named}"
    checkers = {(rel, fn.name) for rel, fn in _app_functions(_FIELD_ROOTS)
                if {_name(c.func) for c in _own_nodes(fn) if isinstance(c, ast.Call)} & _BOUNDARY}
    unchecked = {k: w for k, w in _HANDED.items() if w not in checkers}
    assert not unchecked, f"a record is handed to a writer that does not check its numbers: {unchecked}"


def _checks_the_key(call: ast.Call) -> bool:
    """Whether ``call`` is free_number on the number inside a storage key (key_number)."""
    return (_name(call.func) == "free_number" and len(call.args) > 2
            and "key_number" in ast.unparse(call.args[2]))


def _discarded_numbers() -> list[tuple[str, str]]:
    """(file, function) of every boundary call whose returned number is thrown away, so the
    writer would store the number as sent. The number inside an import's storage key is only
    checked: the import record strips it (strip_key). free_patched_numbers gives the numbers
    back in the patch it is handed."""
    return [(rel, fn.name) for rel, fn in _app_functions() for n in _own_nodes(fn)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await) and isinstance(n.value.value, ast.Call)
            and _name(n.value.value.func) in _BOUNDARY - {"claim_number", "free_patched_numbers"}
            and not _checks_the_key(n.value.value)]


async def test_every_writer_stores_the_number_the_boundary_returns():
    assert sum(1 for r in _census() if r[4] & (_BOUNDARY - {"claim_number"})) >= 8
    assert not _discarded_numbers(), f"a writer checks a number but stores it as sent: {_discarded_numbers()}"


# A refusal is fair only where the user holds the number that resolves it. Every caller of the
# numbering check, by who gives the number: "user" (typed or imported: refused with the choose-
# another message) or "system" (derived by the app: never refused, the writer takes the path that
# keeps the record's meaning and only asks number_holder who holds the number).
_REFUSES = {"require_number_free", "free_number", "free_import_numbers", "free_patched_numbers"}
_DOCS = "default_modules/celerp-docs/celerp_docs"
_REFUSAL_WAY_OUT = {
    (f"{_DOCS}/routes.py", "create_doc_record"): ("user", "the number given on create"),
    (f"{_DOCS}/routes.py", "create_list_record"): ("user", "the number given on create"),
    (f"{_DOCS}/routes.py", "write_doc_patch"): ("user", "the number a patch sets"),
    (f"{_DOCS}/routes.py", "write_list_patch"): ("user", "the number a patch sets"),
    (f"{_DOCS}/routes.py", "renumber_doc"): ("user", "the number a renumber asks for"),
    (f"{_DOCS}/routes.py", "import_list"): ("user", "the number and key in the imported file"),
    (f"{_DOCS}/routes.py", "batch_import_lists"): ("user", "the number and key in the imported file"),
    (f"{_DOCS}/import_service.py", "create_imported_doc"): ("user", "the number and key in the imported file"),
    (f"{_DOCS}/routes.py", "revert_doc_to_draft"): (
        "system", "the order number a bill was converted from: held elsewhere, the bill stays a bill"),
}


def _number_checks() -> dict[tuple[str, str], set[str]]:
    """(file, function) -> the numbering checks it calls, for every caller outside the checks."""
    calls = {}
    for rel, fn in _app_functions():
        if fn.name in _REFUSES | {"number_holder"}:
            continue
        names = {_name(n.func) for n in _own_nodes(fn) if isinstance(n, ast.Call)} & (_REFUSES | {"number_holder"})
        if names:
            calls[rel, fn.name] = names
    return calls


async def test_every_caller_of_the_numbering_check_says_who_holds_the_way_out():
    calls = _number_checks()
    assert calls.keys() == _REFUSAL_WAY_OUT.keys(), (
        f"unclassified: {calls.keys() - _REFUSAL_WAY_OUT.keys()}, gone: {_REFUSAL_WAY_OUT.keys() - calls.keys()}")
    refused = {k: calls[k] & _REFUSES for k, (who, _) in _REFUSAL_WAY_OUT.items() if who == "system"}
    assert not any(refused.values()), f"a number the app derived is refused: {refused}"


async def _imported_bill(client, h, contacts) -> tuple[str, str]:
    """An imported bill naming an imported purchase order, a separate record holding the order's
    number: (the bill's key, its number)."""
    po, number, bill = f"PO-{uuid.uuid4().hex[:6]}", f"BILL-{uuid.uuid4().hex[:6]}", f"doc:{uuid.uuid4()}"
    vendor = next(c for c in contacts if c.get("contact_type") in ("vendor", "both"))
    base = {"contact_id": vendor["id"], "line_items": [{"name": "Svc", "quantity": 1, "unit_price": 100}],
            "currency": "USD", "issue_date": "2026-11-01", "import_treatment": "opening_balances",
            "subtotal": 100, "total": 100}
    for key, data in ((f"doc:{uuid.uuid4()}", {"doc_type": "purchase_order", "ref_id": po, "status": "converted"}),
                      (bill, {"doc_type": "bill", "ref_id": number, "status": "awaiting_payment",
                              "source_po_ref": po, "amount_outstanding": 100})):
        r = await client.post("/docs/import", headers=h, json={
            "entity_id": key, "event_type": "doc.created", "idempotency_key": f"k-{uuid.uuid4().hex}",
            "source": "csv", "data": {**base, **data}})
        assert r.status_code == 200, r.text
    return bill, number


@pytest.mark.parametrize("door", (*_DOORS, "revert"))
async def test_every_door_that_writes_a_number_locks_the_company_before_any_record(client, session, door):
    """Numbering locks the company (lock_company, FOR NO KEY UPDATE), so a door that locked a
    record first could deadlock with one that numbers: each takes that lock before any record
    FOR UPDATE, as lock_projections and emit_event do. A share lock (hold_company) does not count."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    target = (await _imported_bill(client, h, contacts))[0] if door == "revert" else await _target(client, h, contacts, door)
    seen = []
    engine = session.bind.sync_engine

    def record(conn, cursor, statement, parameters, *_):
        if "FROM companies" in statement and "FOR NO KEY UPDATE" in statement:
            seen.append("companies")
        elif "FROM projections" in statement and " FOR " in statement and "SHARE" not in statement:
            seen.append("projections")
    event.listen(engine, "before_cursor_execute", record)
    try:
        if door == "revert":
            r = await client.post(f"/docs/{target}/revert-to-draft", headers=h, json={"reason": "wrong supplier"})
            assert r.status_code == 200, r.text
        else:
            assert (await _give(client, h, contacts, door, f"LK-{uuid.uuid4().hex[:6]}", target=target))[1] is None
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert "companies" in seen and seen.index("companies") <= (seen + ["projections"]).index("projections"), seen


def census_report() -> str:
    """The census by number field as text: one line per write with its disposition."""
    return "\n".join(f"{f}\t{rel}:{line}\t{fn}\t{how}" for f, rel, fn, line, how in
                     sorted(_field_census(), key=lambda r: (r[0], r[1], r[3])))


# ── Census: the numbering map has one writer and the legacy rule one reader ──

_SEQUENCES = "default_modules/celerp-docs/celerp_docs/sequences.py"


def _map_census() -> list[tuple[str, str, str, str]]:
    """(file, function, what, how) for every use of the numbering map's key ("sequences") in the
    app, every use of the shared counter's stored List types ("shares") in sequences.py, every
    raise in sequences.py and every assignment to a company's settings there."""
    rows = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = str(path.relative_to(_REPO))
        if rel.split("/")[0] not in {"celerp", "default_modules", "ui"} or "/tests/" in rel or "/test_" in rel:
            continue
        tree = ast.parse(path.read_text())
        owner = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in _own_nodes(fn):
                    owner[node] = fn.name
        parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            fn = owner.get(node, "<module>")
            if isinstance(node, ast.Constant) and (node.value == "sequences" or (
                    node.value == "shares" and rel == _SEQUENCES)):
                up = parent.get(node)
                how = ("write" if isinstance(up, ast.Subscript) and isinstance(up.ctx, ast.Store)
                       else "key" if isinstance(up, ast.Dict) else "read")
                rows.append((rel, fn, node.value, how))
            if rel != _SEQUENCES:
                continue
            if isinstance(node, ast.Raise) and node.exc is not None:
                rows.append((rel, fn, "raise", "keyed" if _keyed_raise(node.exc, parent.get(node))
                             else ast.unparse(node.exc)[:60]))
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t_, ast.Attribute) and t_.attr == "settings" for t_ in targets):
                    rows.append((rel, fn, "settings", "write"))
    return rows


def _keyed_raise(exc: ast.expr, handler: ast.AST | None) -> bool:
    """A keyed refusal is a Refused, or an HTTPException whose detail is a catalog refusal or
    the detail of the Refused its ``except Refused as <name>`` (``handler``) caught."""
    caught = (handler.name if isinstance(handler, ast.ExceptHandler)
              and _name(handler.type) == "Refused" else None)
    return isinstance(exc, ast.Call) and (_name(exc.func) == "Refused" or (
        _name(exc.func) == "HTTPException"
        and any(k.arg == "detail" and (
            (isinstance(k.value, ast.Call) and _name(k.value.func) == "catalog_refusal")
            or (caught and ast.unparse(k.value) == f"{caught}.detail")) for k in exc.keywords)))


async def test_the_numbering_map_has_one_writer():
    rows = _map_census()
    assert len([r for r in rows if r[2] == "sequences"]) >= 8, rows
    outside = [r for r in rows if r[2] == "sequences" and r[0] != _SEQUENCES
               and r[:2] != ("celerp/services/company_settings.py", "<module>")]
    assert not outside, f"the numbering map is touched outside sequences.py: {outside}"
    writers = {r[1] for r in rows if r[3] == "write" and r[2] in {"sequences", "settings", "shares"}}
    assert writers == {"_store"}, f"the numbering map is written outside _store: {writers}"


async def test_the_legacy_list_rule_reads_only_the_stored_list_types():
    rows = [r for r in _map_census() if r[2] == "shares"]
    assert {r[1] for r in rows if r[3] == "read"} == {"_shares"}, rows
    assert {r[1] for r in rows if r[3] == "key"} <= {"_store", "update_sequence"}, rows


async def test_every_numbering_refusal_is_keyed():
    rows = [r for r in _map_census() if r[2] == "raise"]
    assert len(rows) >= 5, rows
    unkeyed = [r for r in rows if r[3] != "keyed"]
    assert not unkeyed, f"a numbering refusal is raised without a message key: {unkeyed}"


@pytest.mark.parametrize("source, keyed", [
    ('raise Refused("documents.err_number_too_long", max=1, length=2)', True),
    ('raise HTTPException(409, detail=catalog_refusal("documents.err_list_number_taken", number="A"))', True),
    ("try:\n    pass\nexcept Refused as exc:\n    raise HTTPException(409, detail=exc.detail)", True),
    ("try:\n    pass\nexcept ValueError as exc:\n    raise HTTPException(409, detail=exc.detail)", False),
    ("try:\n    pass\nexcept Refused as exc:\n    raise HTTPException(409, detail=str(exc))", False),
    ('raise HTTPException(409, detail="Number taken")', False),
    ('raise ValueError("Number taken")', False),
])
async def test_the_refusal_census_tells_a_keyed_raise_from_a_bare_one(source, keyed):
    tree = ast.parse(source)
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    raise_ = next(n for n in ast.walk(tree) if isinstance(n, ast.Raise))
    assert _keyed_raise(raise_.exc, parent.get(raise_)) is keyed


async def test_an_imported_bill_whose_order_is_another_record_reverts_to_a_draft_bill(client, session):
    """An imported bill names its imported purchase order, a separate record holding the order's
    number, so the bill was never that order: it goes back to a draft bill under its own number,
    with no refusal the user could not resolve."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    bill, number = await _imported_bill(client, h, contacts)
    r = await client.post(f"/docs/{bill}/revert-to-draft", headers=h, json={"reason": "wrong supplier"})
    assert r.status_code == 200, r.text
    after = (await client.get(f"/docs/{bill}", headers=h)).json()
    assert (after["doc_type"], after["ref_id"], after["status"]) == ("bill", number, "draft")
