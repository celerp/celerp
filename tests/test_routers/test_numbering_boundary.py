# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Document and List numbers are unique at one boundary: every counter draw skips a number a
Document or List already shows, every number a user or an import gives is refused while another
record shows it, and the census below fails when a new writer reaches numbering another way.
Numbering shows only counters something draws, and a company with the old shared List counter
keeps it for quotation, transfer and untyped Lists."""
from __future__ import annotations

import ast
import pathlib

import pytest

from celerp.services.company_lock import locked_company
from test_tax_acceptance_matrix import _demo_company
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
        "entity_id": "list:imported-1", "event_type": "list.created", "idempotency_key": "imp-1",
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
    quotation, shared, transfer = rows["quotation"]["preview"], rows["list"]["preview"], rows["transfer"]["preview"]
    assert await _ref(client, h, (await _quote_doc(client, h, contacts)).json()["id"]) == quotation
    assert await _ref(client, h, await _list(client, h, contacts, "quotation")) == shared
    assert transfer == (await _rows(client, h))["list"]["preview"]
    assert await _ref(client, h, await _list(client, h, contacts, "transfer")) == transfer


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
        assert text == t("documents.err_numbering_overlap", "th", kind=doc_type_label("transfer"))
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


# ── Census: every door into numbering goes through the boundary ────────────

_DRAWS = {"next_doc_ref", "next_draft_ref", "_next_free_list_ref", "claim_number"}
_NUMBERED = {"doc.created", "list.created", "doc.renumbered", "DOC_CREATED", "LIST_CREATED", "DOC_RENUMBERED"}
_BOUNDARY = {"claim_number", "require_number_free"}
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
    a Document or List its number and every patch of a document's number (fix-1107/census-N.sh)."""
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
    return rows


async def test_every_counter_draw_goes_through_claim_number():
    rows = _census()
    assert any(r[0] == "DRAW" for r in rows), "the census found no counter draw"
    stray = [r[:4] for r in rows if r[0] == "DRAW" and r[3] != "claim_number"
             and not (r[2] == "claim_number" and r[3] == "next_doc_ref")]
    assert not stray, f"a counter is drawn outside claim_number, so the number may be taken: {stray}"


async def test_every_numbered_writer_goes_through_the_boundary():
    rows = [r for r in _census() if r[0] in {"NUMBER", "REF_EDIT"}]
    assert len(rows) >= 15, rows
    stray = [r[:4] for r in rows if not r[4] and (r[1], r[2]) not in _CLAIMED_BY_CALLER]
    assert not stray, f"a writer gives a number without claim_number or require_number_free: {stray}"
    callers = {r[2] for r in _census() if r[4]}
    assert set(_CLAIMED_BY_CALLER.values()) <= callers
