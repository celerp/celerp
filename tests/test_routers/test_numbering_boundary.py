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

from sqlalchemy import event, select, text

from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
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

    def count(conn, cursor, statement, *_):
        if "projections" in statement:
            reads.append(statement)
    event.listen(engine, "before_cursor_execute", count)
    try:
        ref = await claim_number(session, company, "quotation")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    await session.rollback()
    assert ref.endswith("-0041")
    assert len(reads) <= 3, f"{len(reads)} reads for 40 held numbers"
    # Each read is an indexed lookup of the numbers, never a scan of the company's records.
    from sqlalchemy.dialects import postgresql
    from celerp_docs.sequences import _number_match
    query = select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type.in_(("doc", "list")),
        _number_match([ref.lower()]))
    sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    await session.execute(text("ANALYZE projections"))
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(r[0] for r in await session.execute(text("EXPLAIN " + sql)))
    await session.rollback()
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

def _imported(kind: str, key: str, number: str | None) -> dict:
    data = {"list_type": "quotation"} if kind == "list" else {"doc_type": "quotation"}
    return {"entity_id": key, "event_type": f"{kind}.created", "idempotency_key": f"k-{uuid.uuid4().hex}",
            "source": "csv", "data": {**data, "status": "draft", **({"ref_id": number} if number else {})}}


async def _target(client, h, contacts, door: str) -> str | None:
    """The record a patch or renumber door gives a new number to."""
    if door in ("patch doc", "renumber doc"):
        return (await _quote_doc(client, h, contacts)).json()["id"]
    return await _list(client, h, contacts, "quotation") if door == "patch list" else None


async def _give(client, h, contacts, door: str, number: str, *, target: str | None = None,
                key: str | None = None) -> tuple[str, str | None]:
    """Give a record ``number`` through ``door`` (a patch or renumber door: to ``target``): (the
    record's id, the refusal's message key or None). An import is stored under ``key`` when given."""
    kind = "list" if "list" in door else "doc"
    body = {"fields_changed": {"ref_id": {"old": None, "new": number}}}
    if door in ("patch doc", "renumber doc"):
        eid = target
        r = await (client.patch(f"/docs/{eid}", headers=h, json=body) if door == "patch doc" else
                   client.post(f"/docs/{eid}/renumber", headers=h, json={"ref_id": number}))
    elif door == "patch list":
        eid = target
        r = await client.patch(f"/lists/{eid}", headers=h, json=body)
    elif door == "create doc":
        r = await _quote_doc(client, h, contacts, ref_id=number)
        eid = r.json().get("id")
    elif door == "create list":
        r = await client.post("/lists", headers=h, json={"list_type": "quotation", **_payload(contacts), "ref_id": number})
        eid = r.json().get("id")
    else:
        eid = key or f"{kind}:imp-{uuid.uuid4().hex[:8]}"
        record = _imported(kind, eid, number)
        if door.endswith("batch"):
            r = await client.post(f"/{kind}s/import/batch", headers=h, json={"records": [record]})
            assert r.status_code == 200, r.text
            errors = r.json()["errors"]
            return eid, (next(k for k in ("err_doc_number_taken", "err_list_number_taken") if k in errors[0])
                         if errors else None)
        r = await client.post(f"/{kind}s/import", headers=h, json=record)
    if r.status_code == 200:
        return eid, None
    return eid, r.json()["detail"]["message_key"].split(".")[-1]


_DOORS = ("create doc", "create list", "patch doc", "patch list", "renumber doc",
          "import doc", "import doc batch", "import list", "import list batch")


@pytest.mark.parametrize("door", _DOORS)
async def test_a_padded_number_is_saved_trimmed_and_held_as_typed_plain(client, session, door):
    """A number sent with spaces around it is the number the user meant: it is saved without
    them, so the next record given the plain number is refused, and the counter skips it."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    target = await _target(client, h, contacts, door)
    number = (await _rows(client, h))["quotation"]["preview"]
    eid, refused = await _give(client, h, contacts, door, f"  {number} \t", target=target)
    assert refused is None and await _ref(client, h, eid) == number
    r = await _quote_doc(client, h, contacts, ref_id=number)
    kind = "list" if eid.startswith("list:") else "doc"
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == f"documents.err_{kind}_number_taken", r.text
    assert (await _rows(client, h))["quotation"]["preview"] != number


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


@pytest.mark.parametrize("field", ["ref_id", "doc_number", "entity_id"])
async def test_a_number_stored_with_spaces_before_numbers_were_trimmed_is_still_held(client, session, field):
    """A record saved with a padded number by an earlier version holds the number it meant,
    whether the padding is on the number it shows, the number it keeps or its storage key."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    number = (await _rows(client, h))["quotation"]["preview"]
    eid = (await _quote_doc(client, h, contacts, ref_id=f"OLD-{uuid.uuid4().hex[:6]}")).json()["id"]
    if field == "entity_id":
        await session.execute(text("UPDATE projections SET entity_id = :new WHERE entity_id = :old"),
                              {"new": f"doc:{number} ", "old": eid})
    else:
        await session.execute(text(f"UPDATE projections SET state = jsonb_set(state::jsonb, '{{{field}}}', "
                                   f"to_jsonb(CAST(:padded AS text)))::json WHERE entity_id = :eid"),
                              {"padded": f" {number}  ", "eid": eid})
    await session.commit()
    r = await _quote_doc(client, h, contacts, ref_id=number)
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "documents.err_doc_number_taken", r.text
    assert (await _rows(client, h))["quotation"]["preview"] != number


# ── Census: every door into numbering goes through the boundary ────────────

_DRAWS = {"next_doc_ref", "next_draft_ref", "_next_free_list_ref", "claim_number"}
_NUMBERED = {"doc.created", "list.created", "doc.renumbered", "DOC_CREATED", "LIST_CREATED", "DOC_RENUMBERED"}
_BOUNDARY = {"claim_number", "require_number_free", "free_import_numbers"}
# A writer handed a number its caller claimed: (file, function) -> the caller that claims it.
_CLAIMED_BY_CALLER = {
    ("default_modules/celerp-docs/celerp_docs/consignment_buy.py", "buy_consignment"): "convert_doc",
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


def _census() -> list[tuple[str, str, str, str, set[str]]]:
    """(kind, file, function, what, boundary calls) for every counter draw, every event that gives
    a Document or List its number and every writer of a patch that may change a number."""
    rows = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO)
        if rel.parts[0] not in {"celerp", "default_modules", "ui"} or any(
                p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.parts):
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in _own_nodes(fn) if isinstance(n, ast.Call)]
            via = {_name(c.func) for c in calls} & _BOUNDARY
            for call in calls:
                what = _name(call.func)
                tags = [a.value if isinstance(a, ast.Constant) else _name(a)
                        for a in [*call.args, *(k.value for k in call.keywords)]]
                if what in _DRAWS:
                    rows.append(("DRAW", str(rel), fn.name, what, via))
                if what in {"emit_event", "_emit_list"} and set(tags) & _NUMBERED:
                    rows.append(("NUMBER", str(rel), fn.name, what, via))
                if (what == "get" and isinstance(call.func, ast.Attribute)
                        and "fields_changed" in ast.unparse(call.func.value) and tags[:1] == ["ref_id"]):
                    rows.append(("REF_EDIT", str(rel), fn.name, what, via))
                if what == "_refuse_protected_fields":  # a writer of a user's field patch, ref_id included
                    rows.append(("PATCH", str(rel), fn.name, what, via))
    return rows


async def test_every_counter_draw_goes_through_claim_number():
    rows = _census()
    assert any(r[0] == "DRAW" for r in rows), "the census found no counter draw"
    stray = [r[:4] for r in rows if r[0] == "DRAW" and r[3] != "claim_number"
             and not (r[2] == "claim_number" and r[3] == "next_doc_ref")]
    assert not stray, f"a counter is drawn outside claim_number, so the number may be taken: {stray}"


async def test_every_numbered_writer_goes_through_the_boundary():
    rows = [r for r in _census() if r[0] in {"NUMBER", "REF_EDIT", "PATCH"}]
    assert len(rows) >= 15 and {r[2] for r in rows if r[0] == "PATCH"} >= {"write_doc_patch", "write_list_patch"}, rows
    stray = [r[:4] for r in rows if not r[4] and (r[1], r[2]) not in _CLAIMED_BY_CALLER]
    assert not stray, f"a writer gives a number without claim_number or require_number_free: {stray}"
    callers = {r[2] for r in _census() if r[4]}
    assert set(_CLAIMED_BY_CALLER.values()) <= callers


def _discarded_numbers() -> list[tuple[str, str]]:
    """(file, function) of every call of require_number_free or free_import_numbers whose
    returned number is thrown away, so the writer would store the number as sent."""
    found = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO)
        if rel.parts[0] not in {"celerp", "default_modules", "ui"} or any(
                p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.parts):
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found += [(str(rel), fn.name) for n in _own_nodes(fn) if isinstance(n, ast.Expr)
                          and isinstance(n.value, ast.Await) and isinstance(n.value.value, ast.Call)
                          and _name(n.value.value.func) in _BOUNDARY - {"claim_number"}]
    return found


async def test_every_writer_stores_the_number_the_boundary_returns():
    assert sum(1 for r in _census() if r[4] & (_BOUNDARY - {"claim_number"})) >= 8
    assert not _discarded_numbers(), f"a writer checks a number but stores it as sent: {_discarded_numbers()}"


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
