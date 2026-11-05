# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bulk line action's confirm comes from the action itself.

Sent with ``dry_run``, a stock line action answers what it would do (``acted``: the line ids
it acts on; ``skipped``: the lines it leaves and why) without changing anything, from the
same rule the action then runs. Whatever the mix of lines chosen (no item, a service, nothing
held, already shipped, out on another record), the preview equals the real answer, and the
real answer changes exactly the lines it names in ``acted``.

Set as shipped leaves a line that has already shipped, or whose goods are on another record,
and names it, so the lines that can ship still ship.

The records are the gemstones demo company's: demo stones and a "GIA Certificate"."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from test_item_less_line_actions import GIA, SVC, _act, _gems, _ids, _line, _list, _ok, _state, _status

pytestmark = pytest.mark.asyncio

# The lines of every record here, in order; the GIA Certificate has no item.
SKUS = ("DEMO-SAP-001", "DEMO-EMR-001", "DEMO-MIN-001", "GIA", SVC)
STONES = SKUS[:3]


def _lines(g) -> list[dict]:
    return [GIA if s == "GIA" else _line(g, s, 200.0 if s == SVC else 1000.0) for s in SKUS]


async def _record(client, g, kind: str, states: dict[str, str]) -> tuple[str, dict[str, str]]:
    """A finalized invoice or a List of SKUS, each stone in its state: "held" by this record,
    "shipped" by it, or "elsewhere" (sold on another invoice). Returns it and its line ids by sku."""
    lines = _lines(g)
    owner = await (_list(client, g, lines) if kind == "list" else _doc_of(client, g, lines))
    lids = dict(zip(SKUS, await _ids(client, g, owner)))
    for sku, st in states.items():
        if st == "held":
            await _ok(await _act(client, g, owner, "reserve-lines", [lids[sku]], new_status="reserved"))
        elif st == "shipped":
            await _ok(await _act(client, g, owner, "fulfill-lines", [lids[sku]]))
        elif st == "elsewhere":
            other = await _doc_of(client, g, [_line(g, sku)])
            await _ok(await _act(client, g, other, "fulfill-lines", await _ids(client, g, other)))
    return owner, lids


async def _doc_of(client, g, lines: list[dict]) -> str:
    d = (await _ok(await client.post("/docs", headers=g.h, json={"doc_type": "invoice", "line_items": lines})))["id"]
    await _ok(await client.post(f"/docs/{d}/finalize", headers=g.h))
    return d


# case -> (record, action path, extra body, stone states, chosen skus,
#          skus it acts on or None when the choice is refused, skipped kind by sku)
_CASES = {
    "ship-mixed": ("invoice", "fulfill-lines", {}, {"DEMO-EMR-001": "shipped", "DEMO-MIN-001": "elsewhere"}, SKUS,
                   ["DEMO-SAP-001", SVC], {"DEMO-EMR-001": "shipped", "DEMO-MIN-001": "unavailable", "GIA": "no_item"}),
    "ship-none": ("invoice", "fulfill-lines", {}, {"DEMO-EMR-001": "shipped", "DEMO-MIN-001": "elsewhere"},
                  ("DEMO-EMR-001", "DEMO-MIN-001", "GIA"), None, {}),
    "reserve-mixed": ("invoice", "reserve-lines", {"new_status": "reserved"}, {"DEMO-EMR-001": "shipped"},
                      ("DEMO-SAP-001", "DEMO-EMR-001", "GIA", SVC), ["DEMO-SAP-001", "DEMO-EMR-001"],
                      {"GIA": "no_item", SVC: "service"}),
    "reserve-none": ("invoice", "reserve-lines", {"new_status": "reserved"}, {}, ("GIA", SVC), None, {}),
    "reserve-elsewhere": ("invoice", "reserve-lines", {"new_status": "reserved"}, {"DEMO-MIN-001": "elsewhere"},
                          ("DEMO-SAP-001", "DEMO-MIN-001"), None, {}),
    "release-mixed": ("invoice", "reserve-lines", {"new_status": "available"}, {"DEMO-SAP-001": "held"},
                      ("DEMO-SAP-001", "GIA", SVC), ["DEMO-SAP-001"], {"GIA": "no_item", SVC: "service"}),
    "set-available-mixed": ("invoice", "set-available", {}, {"DEMO-SAP-001": "held", "DEMO-EMR-001": "shipped"}, SKUS,
                            ["DEMO-SAP-001", "DEMO-EMR-001"],
                            {"DEMO-MIN-001": "idle", "GIA": "no_item", SVC: "service"}),
    "set-available-none": ("invoice", "set-available", {}, {}, ("DEMO-MIN-001", "GIA"), None, {}),
    "revert-mixed": ("invoice", "revert-lines", {}, {"DEMO-EMR-001": "shipped"}, ("DEMO-EMR-001", "GIA", SVC),
                     ["DEMO-EMR-001"], {"GIA": "no_item", SVC: "service"}),
    "revert-none": ("invoice", "revert-lines", {}, {}, ("GIA", SVC), None, {}),
    "list-reserve-mixed": ("list", "reserve-lines", {"new_status": "reserved"}, {}, ("DEMO-SAP-001", "GIA", SVC),
                           ["DEMO-SAP-001"], {"GIA": "no_item", SVC: "service"}),
    "list-release-mixed": ("list", "reserve-lines", {"new_status": "available"}, {"DEMO-SAP-001": "held"},
                           ("DEMO-SAP-001", "GIA", SVC), ["DEMO-SAP-001"], {"GIA": "no_item", SVC: "service"}),
    "list-set-available-mixed": ("list", "set-available", {}, {"DEMO-SAP-001": "held"},
                                 ("DEMO-SAP-001", "DEMO-MIN-001", "GIA", SVC), ["DEMO-SAP-001"],
                                 {"DEMO-MIN-001": "idle", "GIA": "no_item", SVC: "service"}),
    "list-set-available-none": ("list", "set-available", {}, {}, ("DEMO-MIN-001", "GIA"), None, {}),
}


async def _ledger_rows(session, g) -> int:
    return (await session.execute(
        select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == g.company_id))).scalar_one()


async def _snapshot(client, session, g, owner) -> tuple:
    state = await _state(client, g, owner)
    return (await _ledger_rows(session, g), state["version"], state["line_items"],
            [await _status(client, g, s) for s in STONES])


@pytest.mark.parametrize("case", list(_CASES))
async def test_the_preview_of_a_bulk_line_action_is_what_the_action_then_does(client, session, case):
    kind, path, extra, states, chosen, acts, kinds = _CASES[case]
    g = await _gems(client)
    owner, lids = await _record(client, g, kind, states)
    send = [lids[s] for s in chosen]
    key = uuid.uuid4().hex
    base = "lists" if kind == "list" else "docs"
    before = await _snapshot(client, session, g, owner)
    preview = await client.post(f"/{base}/{owner}/{path}", headers=g.h,
                                json={"line_ids": send, "idempotency_key": key, "dry_run": True, **extra})
    assert await _snapshot(client, session, g, owner) == before, "the preview changed something"
    real = await client.post(f"/{base}/{owner}/{path}", headers=g.h,
                             json={"line_ids": send, "idempotency_key": key, **extra})
    assert (preview.status_code, preview.json()) == (real.status_code, real.json())
    after = dict(zip(STONES, [await _status(client, g, s) for s in STONES]))
    was = dict(zip(STONES, before[3]))
    if acts is None:
        assert real.status_code == 422, real.text
        assert after == was
        return
    answer = await _ok(real)
    assert answer["acted"] == [lids[s] for s in acts]
    assert {s["line_id"]: s["kind"] for s in answer["skipped"]} == {lids[s]: k for s, k in kinds.items()}
    # Every stone it names in ``acted`` changed, and no other stone did.
    assert {s for s in STONES if after[s] != was[s]} == {s for s in acts if s in STONES}


async def test_set_as_reserved_names_the_shipped_lines_it_takes_back(client, session):
    g = await _gems(client)
    owner, lids = await _record(client, g, "invoice", {"DEMO-EMR-001": "shipped"})
    answer = await _ok(await _act(client, g, owner, "reserve-lines", [lids["DEMO-SAP-001"], lids["DEMO-EMR-001"]],
                                  new_status="reserved", dry_run=True))
    assert answer["taken_back"] == [lids["DEMO-EMR-001"]]


async def test_set_as_shipped_ships_the_lines_that_can_ship_and_names_the_rest(client, session):
    """An already sold line, an available line and the GIA Certificate: the available line
    ships, and the other two are named with what to do next. Red statement: the whole request
    was refused ("Not available: DEMO-EMR-001 has status Sold") and the available line stayed."""
    g = await _gems(client)
    owner, lids = await _record(client, g, "invoice", {"DEMO-EMR-001": "shipped"})
    answer = await _ok(await _act(client, g, owner, "fulfill-lines",
                                  [lids["DEMO-EMR-001"], lids["DEMO-SAP-001"], lids["GIA"]]))
    assert await _status(client, g, "DEMO-SAP-001") == "sold"
    assert [(n["message_key"], n["params"]) for n in answer["skipped_notice"]] == [
        ("documents.lines_skipped_one", {"lines": '4 "GIA Certificate"'}),
        ("documents.shipped_lines_skipped_one", {"lines": '2 "DEMO-EMR-001"'}),
    ]


async def test_a_line_shipped_from_part_of_a_weight_lot_does_not_ship_again(client, session):
    """A line of 1 ct off a weight-sold demo lot ships once; sent again under a new key it is
    named as already shipped and no more of the lot is carved off and sold. Red statement:
    each call carved and sold another carat (38.6 -> 36.6 after two ships of one line)."""
    g = await _gems(client)
    lot = next(i for i in g.items.values() if i.get("sell_by") not in (None, "piece")
               and i.get("status") == "available" and float(i.get("quantity") or 0) > 2)
    line = {"item_id": lot["id"], "sku": lot["sku"], "description": lot["name"], "quantity": 1,
            "unit_price": 10.0, "sell_by": lot["sell_by"]}
    owner = await _doc_of(client, g, [line, GIA])
    lid, gia = await _ids(client, g, owner)
    await _ok(await _act(client, g, owner, "fulfill-lines", [lid]))
    left = (await _ok(await client.get(f"/items/{lot['id']}", headers=g.h)))["quantity"]
    again = await _act(client, g, owner, "fulfill-lines", [lid])
    assert again.status_code == 422, again.text
    assert again.json()["detail"]["message_key"] == "documents.shipped_lines_skipped_one"
    mixed = await _act(client, g, owner, "fulfill-lines", [lid, gia])
    assert mixed.status_code == 422, mixed.text
    assert (await _ok(await client.get(f"/items/{lot['id']}", headers=g.h)))["quantity"] == left
