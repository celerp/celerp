# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A copy is its parties and its lines (record_copy.copy_of), from the demo company. Each line keeps
only what a user enters on a line, read from the line save steps' own bodies (LineItem and
WriteoffLineBody), so a count, an adjustment, a write-off or a draw stays with the source line."""
from __future__ import annotations

from sqlalchemy import select

from celerp.models.projections import Projection
from celerp_docs.line_bodies import LineItem, WriteoffLineBody
from celerp_docs.record_copy import copy_of
from test_tax_acceptance_matrix import _demo_company

# Read from the save steps, never listed here: the keys a user can send on a line, less its identity.
ENTERED = (set(LineItem.model_fields) | set(WriteoffLineBody.model_fields)) - {"line_id", "entity_id"}


async def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


async def _qty(client, h, iid) -> float:
    return float((await _ok(await client.get(f"/items/{iid}", headers=h))).get("quantity") or 0)


async def _adjusted_audit(client, h):
    """A demo lot moved to a second location, counted one short there and adjusted."""
    loc = (await _ok(await client.post("/companies/me/locations", headers=h, json={"name": "Shop", "type": "warehouse"})))["id"]
    items = (await _ok(await client.get("/items", headers=h, params={"limit": 50})))["items"]
    lot = next(x for x in items if float(x.get("quantity") or 0) >= 2 and (x.get("status") or "available") == "available")
    await _ok(await client.post(f"/items/{lot['id']}/transfer", headers=h, json={"to_location_id": loc}))
    aid = (await _ok(await client.post("/lists/audit", headers=h, json={"location_id": loc})))["id"]
    await _ok(await client.post(f"/lists/{aid}/finalize", headers=h))
    before = await _qty(client, h, lot["id"])
    await _ok(await client.patch(f"/lists/{aid}/line/{lot['id']}", headers=h, json={"counted_qty": before - 1}))
    await _ok(await client.post(f"/lists/{aid}/adjust", headers=h))
    return aid, lot["id"], before


async def test_a_duplicate_of_an_adjusted_audit_moves_nothing_and_the_source_still_undoes(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    aid, lot, before = await _adjusted_audit(client, h)
    counted = await _qty(client, h, lot)
    assert counted == before - 1
    dup = (await _ok(await client.post(f"/lists/{aid}/duplicate", headers=h)))["id"]
    line = next(li for li in (await _ok(await client.get(f"/lists/{dup}", headers=h)))["line_items"] if li.get("item_id") == lot)
    assert set(line) - {"line_id"} <= ENTERED, sorted(set(line) - ENTERED)
    await _ok(await client.post(f"/lists/{dup}/finalize", headers=h))
    r = await client.post(f"/lists/{dup}/adjust", headers=h)
    assert r.status_code in (200, 409), r.text
    await client.post(f"/lists/{dup}/undo-adjust", headers=h)
    assert await _qty(client, h, lot) == counted
    await _ok(await client.post(f"/lists/{aid}/undo-adjust", headers=h))
    assert await _qty(client, h, lot) == before


async def test_a_duplicate_keeps_what_the_user_entered_on_its_lines_and_its_parties(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    item = (await _ok(await client.get("/items", headers=h, params={"limit": 5})))["items"][0]
    entered = {"item_id": item["id"], "sku": item.get("sku"), "description": "Engraved", "quantity": 3.0, "unit_price": 42.5}
    lid = (await _ok(await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "notes": "Source only", "line_items": [entered]})))["id"]
    src = await _ok(await client.get(f"/lists/{lid}", headers=h))
    dup = await _ok(await client.get(f"/lists/{(await _ok(await client.post(f'/lists/{lid}/duplicate', headers=h)))['id']}", headers=h))
    kept = ("item_id", "sku", "description", "quantity", "unit_price")
    assert src["line_items"][0]["description"] == "Engraved"
    assert {k: dup["line_items"][0].get(k) for k in kept} == {k: src["line_items"][0].get(k) for k in kept}
    assert dup["contact_id"] == cust and dup.get("contact_name") == src.get("contact_name")
    assert dup["status"] == "draft" and not dup.get("notes")


async def test_every_line_key_the_lifecycle_writes_stays_with_the_source(client, session):
    """Count, adjust, write off, send and convert on demo records; then every line of every document
    and List the company holds, copied, keeps only keys a user enters on a line."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    await _adjusted_audit(client, h)
    items = (await _ok(await client.get("/items", headers=h, params={"limit": 50})))["items"]
    lot = next(x for x in items if float(x.get("quantity") or 0) >= 2 and (x.get("status") or "available") == "available")
    wid = (await _ok(await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot["id"]]})))["id"]
    wl = (await _ok(await client.get(f"/lists/{wid}", headers=h)))["line_items"][0]
    entered = {"qty_out": 1.0, "account": "6950", "comment": "Spoiled"}
    await _ok(await client.post(f"/lists/{wid}/writeoff-line", headers=h, json={"line_id": wl["line_id"], **entered}))
    await _ok(await client.post(f"/lists/{wid}/finalize", headers=h))
    await _ok(await client.post(f"/lists/{wid}/write-off", headers=h))
    wdup = (await _ok(await client.post(f"/lists/{wid}/duplicate", headers=h)))["id"]
    (copied_line,) = (await _ok(await client.get(f"/lists/{wdup}", headers=h)))["line_items"]
    assert {k: copied_line.get(k) for k in entered} == entered
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    qid = (await _ok(await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "line_items": [{"item_id": lot["id"], "quantity": 1, "unit_price": 9}]})))["id"]
    await _ok(await client.post(f"/lists/{qid}/finalize", headers=h))
    await _ok(await client.post(f"/lists/{qid}/send", headers=h, json={"sent_via": "manual"}))
    inv = (await _ok(await client.post(f"/lists/{qid}/convert", headers=h, json={"target_type": "invoice"})))["target_doc_id"]
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type.in_(("doc", "list"))))).scalars().all()
    held = {k for r in rows for li in r.state.get("line_items") or [] if isinstance(li, dict) for k in li}
    assert {"counted_qty", "adjusted", "prior_qty", "disposed_qty"} <= held, sorted(held)
    copied = {k for r in rows for li in copy_of(r.state, {})["line_items"] for k in li}
    assert copied <= ENTERED | {"line_id"}, sorted(copied - ENTERED)
    # And through the doors: every List duplicated, and the invoice its quotation converted to.
    made = [(await _ok(await client.get(f"/docs/{inv}", headers=h)))["line_items"]]
    for r in rows:
        if r.entity_type == "list":
            dup = (await _ok(await client.post(f"/lists/{r.entity_id}/duplicate", headers=h)))["id"]
            made.append((await _ok(await client.get(f"/lists/{dup}", headers=h)))["line_items"])
    stored = {k for lines in made for li in lines for k in li}
    assert stored <= ENTERED | {"line_id"}, sorted(stored - ENTERED)


def test_a_line_naming_its_item_as_entity_id_is_copied_under_item_id():
    """The line editor sends an item as entity_id and the save step keeps it as item_id, so a copy of
    a line that holds only entity_id keeps that item under item_id, and the key itself is never kept."""
    assert LineItem(entity_id="item:1").model_dump(exclude_unset=False)["entity_id"] is None
    (line,) = copy_of({"line_items": [{"entity_id": "item:1", "quantity": 2}]}, {})["line_items"]
    assert line == {"item_id": "item:1", "quantity": 2}
