# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document whose issue posted its total keeps its lines as issued. Its issue posted that
total, and a payment collects it, so once issued a save may change only a line's description
and account. Every other field of every stored line must come back unchanged: a field left
out of the save counts as removed, and a line left out as every field removed. Revert to
Draft is the way to change a line, which voids the entry and posts the new total on the next
issue. Generated over every posted type and every key its lines store.

Every writer that takes lines from outside refuses lines of another shape, or a line holding a
value of the wrong type, naming the field. Generated over every writer (a save of each document
type, each List type's create, save and page save, each import) and every field a line declares."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.list_behavior import DOC_SCAN_BEHAVIOR, LIST_TYPES
from celerp.services.money import document_line_amount
from celerp_docs.doc_constants import TOTAL_POSTED_DOC_TYPES
from celerp_docs.line_bodies import LineItem
from celerp_docs.routes import refuse_malformed_input
from test_invoice_unshipped_books import _ok
from test_receipt_accounting import _doc, _finalize
from test_reissued_invoice_cogs import _invoice, _line_ids, _ship, _state, _stones

pytestmark = pytest.mark.asyncio

_EDITABLE = {"description", "account_code"}
_ABSENT = object()


async def _issued_charges(client, auth, doc_type: str) -> str:
    doc = await _doc(client, auth, doc_type, [{"name": "A", "description": "A", "quantity": 1, "unit_price": 100},
                                              {"name": "B", "description": "B", "quantity": 2, "unit_price": 50}])
    await _finalize(client, auth, doc)
    return doc


async def _issued_invoice(client, auth, doc_type: str) -> str:
    """An issued invoice of two stones whose second line shipped and came back."""
    doc = await _invoice(client, auth, (await _stones(client, auth))[:2])
    second = (await _line_ids(client, auth, doc))[1]
    assert (await _ship(client, auth, doc, [second])).status_code == 200
    await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": [second]})
    return doc


_ISSUED = {"invoice": _issued_invoice, "bill": _issued_charges, "credit_note": _issued_charges,
           "debit_note": _issued_charges}


async def _posted(session, doc: str) -> dict:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.entity_type == "journal_entry", Projection.entity_id.like(f"je:auto:{doc}%")))).scalars().all()
    return {r.entity_id: r.state for r in rows}


async def _save(client, auth, st: dict, lines: list[dict]):
    return await client.patch(f"/docs/{st['id']}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": st["line_items"], "new": lines}},
        "expected_version": st["version"]})


def _other(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return value + "-x"
    return "x" if value is None else None


def _variants(line: dict, key: str):
    """The line with ``key`` left out, emptied and changed, as far as each differs."""
    value = line[key]
    for name, new in (("omit", _ABSENT), ("null", None), ("change", _other(value))):
        if new is _ABSENT or new != value:
            yield name, ({k: v for k, v in line.items() if k != key} if new is _ABSENT else {**line, key: new})


def _sums(st: dict) -> float:
    return float(sum(document_line_amount(li, st["currency"]) or 0 for li in st["line_items"]))


async def test_every_document_whose_issue_posts_its_total_is_covered():
    """The guard runs over its own list of posted types, so narrowing the constant cannot
    shrink it, and a type added to the constant needs an issued example here."""
    assert TOTAL_POSTED_DOC_TYPES <= set(_ISSUED)


@pytest.mark.parametrize("doc_type", sorted(_ISSUED))
async def test_an_issued_document_keeps_every_stored_field_of_every_line(client, session, auth, doc_type):
    doc = await _ISSUED[doc_type](client, auth, doc_type)
    st = {**await _state(client, auth, doc), "id": doc}
    assert st["status"] != "draft" and _sums(st) == float(st["total"]), st
    posted = await _posted(session, doc)
    assert posted, doc_type
    for i, line in enumerate(st["line_items"]):
        saves = [(f"drop line {i + 1}", "line", st["line_items"][:i] + st["line_items"][i + 1:])]
        saves += [(f"{name} {key} of line {i + 1}", key, st["line_items"][:i] + [new] + st["line_items"][i + 1:])
                  for key in sorted(set(line) - _EDITABLE) for name, new in _variants(line, key)]
        for label, key, lines in saves:
            r = await _save(client, auth, st, lines)
            assert r.status_code == 409, (doc_type, label, r.text)
            if label.startswith("drop"):  # a line left out is a structural edit: Revert to Draft removes it
                assert r.json()["detail"]["message_key"] == "docs.edit_locked", (doc_type, label, r.text)
            if label.startswith("omit"):
                detail = r.json()["detail"]
                assert (detail["message_key"], detail["params"]) == (
                    "documents.err_line_field_finalized", {"field": key, "line": i + 1}), (doc_type, label)
            after = await _state(client, auth, doc)
            assert (after["line_items"], after["total"]) == (st["line_items"], st["total"]), (doc_type, label)
    r = await _save(client, auth, st, [{**li, "description": f"{li.get('description')} (renamed)"}
                                       for li in st["line_items"]])
    assert r.status_code == 200, (doc_type, r.text)
    after = await _state(client, auth, doc)
    assert _sums(after) == float(after["total"]) == float(st["total"]), doc_type
    assert await _posted(session, doc) == posted, doc_type


async def test_revert_to_draft_is_the_way_to_remove_a_line(client, session, auth):
    doc = await _invoice(client, auth, (await _stones(client, auth))[:2])
    await _ok(client, auth, f"/docs/{doc}/revert-to-draft")
    st = {**await _state(client, auth, doc), "id": doc}
    r = await _save(client, auth, st, st["line_items"][:-1])
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{doc}/finalize")
    st = await _state(client, auth, doc)
    assert (len(st["line_items"]), st["total"]) == (1, 100.0)


async def test_a_void_document_names_unvoid_for_a_line_change(client, session, auth):
    """Revert to Draft refuses a void document, so a line change on one names Unvoid."""
    doc = await _invoice(client, auth, (await _stones(client, auth))[:2])
    await _ok(client, auth, f"/docs/{doc}/void", {"reason": "test"})
    st = {**await _state(client, auth, doc), "id": doc}
    for lines in (st["line_items"][:-1], [{**st["line_items"][0], "quantity": 2}, st["line_items"][1]]):
        r = await _save(client, auth, st, lines)
        assert (r.status_code, r.json()["detail"]["message_key"]) == (409, "docs.edit_void"), r.text


def _hostile(lines: list[dict]) -> dict:
    """Lines in every shape that is not a list of objects."""
    return {"object": dict(enumerate(lines)), "string": "lines", "number": 1, "null": None,
            "list of strings": ["a", "b"], "list with a null": [*lines, None], "list of lists": [lines]}


# A value of every JSON type, as an agent, an import or a peer may send one.
_JSON_VALUES = {"object": {"a": 1}, "list": ["x"], "integer": 1, "number": 1.5, "true": True, "text": "abc",
                "numeric text": "10", "a choice": "stock", "object of texts": {"a": "b"}}
# Every field a line declares; its taxes have their own guard (test_tax_value_shape).
_LINE_FIELDS = sorted(set(LineItem.model_fields) - {"taxes"})


def _line_model_takes(field: str, value) -> bool:
    try:
        LineItem.model_validate({field: value})
        return True
    except ValidationError:
        return False


def _refusal(values: dict) -> dict | None:
    try:
        refuse_malformed_input(values)
    except HTTPException as exc:
        return exc.detail
    return None


def _shown(line: dict) -> str:
    """What a refusal shows a line as: its SKU, name or description, the first one that is text."""
    return next((v for k in ("sku", "name", "description") if isinstance(v := line.get(k), str) and v), "--")


def _field_refusal(lines: list[dict], field: str) -> dict:
    """The refusal of the last of *lines* for a wrong *field*: the line by number and as shown."""
    return {"line": len(lines), "sku": _shown(lines[-1]), "field": field}


async def test_a_written_line_takes_each_value_exactly_as_the_line_model_does():
    """Over every field a line declares and a value of every JSON type: a write refuses exactly
    what the line model refuses, naming the line and the field, and every field refuses some type.
    The line is shown by its SKU, name or description whichever of them the wrong value is in."""
    named = {"sku": "S-1", "name": "N", "description": "D"}
    for field in _LINE_FIELDS:
        refused = [kind for kind, value in _JSON_VALUES.items() if not _line_model_takes(field, value)]
        assert refused, field
        for kind, value in _JSON_VALUES.items():
            for label in (None, *named):
                lines = [{"description": "A"}, {**({label: named[label]} if label else {}), field: value}]
                detail = _refusal({"line_items": lines})
                want = _field_refusal(lines, field) if kind in refused else None
                assert (detail["params"] if detail else None) == want, (field, kind, label, detail)
                assert detail is None or detail["message_key"] == "line.invalid_field", (field, kind)
        assert _refusal({"line_items": [{field: None, "an undeclared key": {"a": 1}}]}) is None, field


def _wrong_lines(lines: list[dict]) -> dict[str, tuple[object, dict]]:
    """Lines of every wrong shape, and for every field a line declares the lines with one value
    the line model refuses for it, each with the parameters the refusal must carry."""
    wrong = {shape: (bad, {"fields": "line_items"}) for shape, bad in _hostile(lines).items()}
    for field in _LINE_FIELDS:
        value = next(v for v in _JSON_VALUES.values() if not _line_model_takes(field, v))
        bad = [*lines[:-1], {**lines[-1], field: value}]
        wrong[f"{field} {value!r}"] = (bad, _field_refusal(bad, field))
    return wrong


def _refused_naming(r, want: dict) -> bool:
    """422 carrying *want*: the line check's refusal, or the request model's own where it declares
    the lines a list of objects."""
    detail = r.json()["detail"]
    if isinstance(detail, list):
        return r.status_code == 422 and want == {"fields": "line_items"} and any(
            e["loc"][:2] == ["body", "line_items"] for e in detail)
    key = "line.invalid_field" if "field" in want else "error.invalid_input_fields"
    return (r.status_code, detail["message_key"], detail["params"]) == (422, key, want)


_LINE = {"description": "S", "quantity": 1, "unit_price": 100.0}


async def _get(client, auth, path: str):
    r = await client.get(path, headers=auth["headers"])
    return (r.json().get("version"), r.json().get("line_items"), r.json().get("total")) if r.status_code == 200 \
        else r.status_code


async def _doc_saves(client, auth, doc: str):
    st = {**await _state(client, auth, doc), "id": doc}
    return st["line_items"], lambda lines: _save(client, auth, st, lines), lambda: _get(client, auth, f"/docs/{doc}")


async def _draft_saves(client, auth, doc_type: str):
    return await _doc_saves(client, auth, await _doc(client, auth, doc_type, [_LINE, {**_LINE, "description": "T"}]))


async def _issued_saves(client, auth, doc_type: str):
    return await _doc_saves(client, auth, await _ISSUED[doc_type](client, auth, doc_type))


async def _list(client, auth, list_type: str) -> dict:
    r = await client.post("/lists", headers=auth["headers"], json={"list_type": list_type, "line_items": [_LINE]})
    assert r.status_code == 200, r.text
    return (await client.get(f"/lists/{r.json()['id']}", headers=auth["headers"])).json()


async def _list_saves(client, auth, list_type: str):
    st = await _list(client, auth, list_type)
    return st["line_items"], lambda lines: client.patch(f"/lists/{st['id']}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"new": lines}}, "expected_version": st.get("version")}), \
        lambda: _get(client, auth, f"/lists/{st['id']}")


async def _list_page_saves(client, auth, list_type: str):
    st = await _list(client, auth, list_type)
    return st["line_items"], lambda lines: client.patch(f"/lists/{st['id']}/line-page", headers=auth["headers"], json={
        "line_items": lines, "offset": 0, "original_count": 1, "expected_version": st.get("version")}), \
        lambda: _get(client, auth, f"/lists/{st['id']}")


async def _list_creates(client, auth, list_type: str):
    async def count():
        return len((await client.get("/lists", headers=auth["headers"], params={"limit": 500})).json()["items"])
    return [_LINE], lambda lines: client.post("/lists", headers=auth["headers"], json={
        "list_type": list_type, "line_items": lines}), count


def _record(kind: str, data: dict) -> dict:
    return {"entity_id": f"{kind}:IMP-{uuid.uuid4().hex[:8]}", "event_type": f"{kind}.created", "source": "csv",
            "idempotency_key": uuid.uuid4().hex, "data": {"status": "draft", **data}}


async def _imports(client, auth, kind: str, batch: bool, data: dict):
    sent: list[str] = []

    async def send(lines):
        rec = _record(kind, {**data, "line_items": lines})
        sent.append(rec["entity_id"])
        if not batch:
            return await client.post(f"/{kind}s/import", headers=auth["headers"], json=rec)
        r = await client.post(f"/{kind}s/import/batch", headers=auth["headers"], json={"records": [rec]})
        assert r.status_code == 200 and r.json()["created"] == 0, r.text
        return r

    async def stored():  # every record a refused import sent is absent
        return [e for e in sent if (await client.get(f"/{kind}s/{e}", headers=auth["headers"])).status_code != 404]
    return [_LINE], send, stored


_WRITERS = {
    **{f"save issued {t}": (_issued_saves, t) for t in sorted(_ISSUED)},
    **{f"save draft {t}": (_draft_saves, t) for t in sorted(DOC_SCAN_BEHAVIOR)},
    **{f"{how} {t}": (writer, t) for t in LIST_TYPES
       for how, writer in (("create list", _list_creates), ("save list", _list_saves),
                           ("save list page", _list_page_saves))},
    **{f"import {kind}{' batch' * batch}": (lambda c, a, d, kind=kind, batch=batch: _imports(c, a, kind, batch, d),
                                            {"doc_type": "invoice"} if kind == "doc" else {"list_type": "quotation"})
       for kind in ("doc", "list") for batch in (False, True)},
}


@pytest.mark.parametrize("writer", sorted(_WRITERS))
async def test_every_line_writer_refuses_lines_of_a_wrong_shape_or_type_naming_the_line(client, auth, writer):
    """Whoever sends them (the agent, an import, a peer), lines of another shape or a line
    holding a value of the wrong type are refused before any check reads them, naming the
    line and the field, and nothing is stored."""
    make, arg = _WRITERS[writer]
    lines, send, stored = await make(client, auth, arg)
    before = await stored()
    for case, (bad, want) in _wrong_lines(lines).items():
        r = await send(bad)
        if writer.endswith("batch"):
            assert all(str(v) in str(r.json()["errors"]) for v in want.values()), (writer, case, r.text)
        else:
            assert _refused_naming(r, want), (writer, case, r.text)
        assert await stored() == before, (writer, case)
