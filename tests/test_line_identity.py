# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every document and List line carries a stable line id.

The id is assigned where lines are written, survives every later save (including a
save from a client that does not know about ids, and the paginated List splice), is
carried by a conversion, and is fresh on a duplicate. A malformed or repeated id is
refused, so two lines can never claim one identity."""
from __future__ import annotations

import uuid

import pytest

from line_actions_support import doc, h, line, line_ids, lot, quotation, set_list_lines, state  # noqa: F401


def _well_formed(ids) -> bool:
    def ok(i):
        try:
            return isinstance(i, str) and uuid.UUID(i) is not None
        except ValueError:
            return False
    return all(ok(i) for i in ids) and len(set(ids)) == len(ids)


@pytest.mark.asyncio
async def test_doc_create_gives_every_line_an_id(client, h):
    a = await lot(client, h, "LID-A", 10)
    d = await doc(client, h, [line(a, 4, sku="LID-A"), line(a, 3, sku="LID-A"), line(None, 1, sku="Fee")],
                  finalize=False)
    ids = await line_ids(client, h, d)
    assert len(ids) == 3 and _well_formed(ids), ids


@pytest.mark.asyncio
async def test_client_supplied_id_is_kept(client, h):
    mine = str(uuid.uuid4())
    d = await doc(client, h, [line(None, 1, sku="Fee", line_id=mine)], finalize=False)
    assert await line_ids(client, h, d) == [mine]


@pytest.mark.asyncio
async def test_patch_without_ids_keeps_existing_ids(client, h):
    """A client that does not send ids (an older form, an integration) must not re-mint
    the identity of lines it merely re-saved."""
    a = await lot(client, h, "LID-P", 10)
    d = await doc(client, h, [line(a, 4, sku="LID-P"), line(None, 1, sku="Fee")], finalize=False)
    before = await line_ids(client, h, d)
    assert _well_formed(before)
    stripped = [{k: v for k, v in li.items() if k != "line_id"} for li in (await state(client, h, d))["line_items"]]
    stripped[0]["quantity"] = 5
    r = await client.patch(f"/docs/{d}", headers=h, json={"fields_changed": {"line_items": {"new": stripped}}})
    assert r.status_code == 200, r.text
    assert await line_ids(client, h, d) == before


@pytest.mark.asyncio
async def test_inserted_line_gets_new_id_and_others_keep_theirs(client, h):
    d = await doc(client, h, [line(None, 1, sku="One"), line(None, 1, sku="Two")], finalize=False)
    before = await line_ids(client, h, d)
    lines = (await state(client, h, d))["line_items"]
    lines.insert(1, line(None, 1, sku="Inserted"))
    r = await client.patch(f"/docs/{d}", headers=h, json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text
    after = await line_ids(client, h, d)
    assert after[0] == before[0] and after[2] == before[1]
    assert after[1] not in before and _well_formed(after)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-uuid", "1234", 7])
async def test_malformed_line_id_refused(client, h, bad):
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "total": 1,
                                                     "line_items": [line(None, 1, sku="Fee", line_id=bad)]})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_duplicate_line_id_refused_and_nothing_written(client, h):
    d = await doc(client, h, [line(None, 1, sku="One"), line(None, 1, sku="Two")], finalize=False)
    lines = (await state(client, h, d))["line_items"]
    lines[1]["line_id"] = lines[0]["line_id"]
    r = await client.patch(f"/docs/{d}", headers=h, json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "line.duplicate_line_id"
    ids = await line_ids(client, h, d)
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_list_line_page_splice_keeps_ids(client, h):
    a, b, c = [await lot(client, h, f"LID-S{i}", 5) for i in range(3)]
    q = await quotation(client, h, [line(x, 1, sku=f"S{i}") for i, x in enumerate((a, b, c))])
    before = await line_ids(client, h, q)
    assert _well_formed(before)
    st = await state(client, h, q)
    # The editor saves the middle row as a one-row page, without ids.
    page = [{k: v for k, v in st["line_items"][1].items() if k != "line_id"} | {"quantity": 2}]
    r = await client.patch(f"/lists/{q}/line-page", headers=h, json={
        "line_items": page, "offset": 1, "original_count": 1, "expected_version": st["version"]})
    assert r.status_code == 200, r.text
    assert await line_ids(client, h, q) == before


@pytest.mark.asyncio
async def test_list_conversion_carries_line_ids(client, h):
    a = await lot(client, h, "LID-C", 5)
    q = await quotation(client, h, [line(a, 2, sku="LID-C"), line(None, 1, sku="Fee")])
    ids = await line_ids(client, h, q)
    assert _well_formed(ids)
    r = await client.post(f"/lists/{q}/finalize", headers=h, json={})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{q}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    assert await line_ids(client, h, r.json()["target_doc_id"]) == ids


@pytest.mark.asyncio
async def test_list_duplicate_gets_fresh_ids(client, h):
    q = await quotation(client, h, [line(None, 1, sku="One"), line(None, 1, sku="Two")])
    ids = await line_ids(client, h, q)
    r = await client.post(f"/lists/{q}/duplicate", headers=h)
    assert r.status_code == 200, r.text
    copy = await line_ids(client, h, r.json()["id"])
    assert _well_formed(copy) and not set(copy) & set(ids)


def test_normalize_carries_unique_item_match_across_a_move():
    """Two linked lines swapped by an id-less client keep their own ids: the match is by
    item when the position no longer agrees."""
    from celerp.services.document_lines import normalize_line_ids
    stored = [{"line_id": str(uuid.uuid4()), "item_id": "item:a"},
              {"line_id": str(uuid.uuid4()), "item_id": "item:b"}]
    incoming = [{"item_id": "item:b"}, {"item_id": "item:a"}]
    normalize_line_ids(incoming, stored)
    assert [li["line_id"] for li in incoming] == [stored[1]["line_id"], stored[0]["line_id"]]


def test_normalize_never_gives_one_stored_id_to_two_lines():
    from celerp.services.document_lines import normalize_line_ids
    stored = [{"line_id": str(uuid.uuid4()), "item_id": "item:a"}]
    incoming = [{"item_id": "item:a"}, {"item_id": "item:a"}]
    normalize_line_ids(incoming, stored)
    assert incoming[0]["line_id"] == stored[0]["line_id"]
    assert incoming[1]["line_id"] != stored[0]["line_id"] and _well_formed([incoming[1]["line_id"]])
