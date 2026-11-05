# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document whose issue posted its total keeps its lines as issued. Its issue posted that
total, and a payment collects it, so once issued a save may change only a line's description
and account. Every other field of every stored line must come back unchanged: a field left
out of the save counts as removed, and a line left out as every field removed. Revert to
Draft is the way to change a line, which voids the entry and posts the new total on the next
issue. Generated over every posted type and every key its lines store."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.money import document_line_amount
from celerp_docs.doc_constants import TOTAL_POSTED_DOC_TYPES
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


@pytest.mark.parametrize("doc_type", sorted(_ISSUED))
async def test_lines_that_are_not_a_list_of_objects_are_refused_naming_the_field(client, auth, doc_type):
    """Whoever sends them (the agent, an import), lines in any other shape are refused before
    any check reads them, naming the field, and nothing changes."""
    doc = await _ISSUED[doc_type](client, auth, doc_type)
    st = {**await _state(client, auth, doc), "id": doc}
    for shape, lines in _hostile(st["line_items"]).items():
        r = await _save(client, auth, st, lines)
        assert r.status_code == 422, (doc_type, shape, r.text)
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == ("error.invalid_input_fields", {"fields": "line_items"})
        after = await _state(client, auth, doc)
        assert (after["line_items"], after["total"], after["version"]) == (
            st["line_items"], st["total"], st["version"]), (doc_type, shape)
