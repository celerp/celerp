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

import json
import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from test_item_less_line_actions import GIA, SVC, _act, _gems, _ids, _line, _list, _ok, _state, _status, assert_described
import test_item_less_line_actions

# The page's client, shared with the item-less line tests.
ui_client = test_item_less_line_actions.ui_client

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
    assert [s["line_id"] for s in answer["taken_back"]] == [lids["DEMO-EMR-001"]]


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


async def test_a_line_invoiced_from_a_memo_is_left_as_already_with_the_customer(client, session):
    """Set as shipped on a finalized invoice drawn from a memo leaves the drawn line as one
    whose goods are already with the customer. Red statement: it was named as already
    shipped with the advice to set it as available first, which would take the customer's
    goods back into stock."""
    from memo_draw_support import draw
    g = await _gems(client)
    memo = await test_item_less_line_actions._doc(client, g, [_line(g, "DEMO-SAP-001")], doc_type="memo")
    mids = await _ids(client, g, memo)
    await _ok(await _act(client, g, memo, "fulfill-lines", mids))
    preview = await _ok(await draw(client, g.h, memo, line_ids=mids, dry_run=True))
    inv = (await _ok(await draw(client, g.h, memo, line_ids=mids, binding=preview["binding"])))["target_doc_id"]
    await _ok(await client.post(f"/docs/{inv}/finalize", headers=g.h))
    [drawn] = await _ids(client, g, inv)
    r = await _act(client, g, inv, "fulfill-lines", [drawn])
    assert r.status_code == 422, r.text
    assert (r.json()["detail"]["message_key"], r.json()["detail"]["params"]) == (
        "documents.drawn_lines_skipped_one", {"lines": '1 "DEMO-SAP-001"'})
    assert await _status(client, g, "DEMO-SAP-001") == "sold"


async def test_the_preview_of_back_to_memo_is_what_it_then_does(client, session):
    from test_item_less_line_actions import _drawn_invoice_with_own_certificate
    g = await _gems(client)
    _memo, invoice, drawn, own = await _drawn_invoice_with_own_certificate(client, g)
    for chosen in ([own], [drawn, own]):
        key = uuid.uuid4().hex
        before = await _state(client, g, invoice)
        preview = await client.post(f"/docs/{invoice}/back-to-memo", headers=g.h,
                                    json={"line_ids": chosen, "idempotency_key": key, "dry_run": True})
        assert await _state(client, g, invoice) == before, "the preview changed something"
        real = await client.post(f"/docs/{invoice}/back-to-memo", headers=g.h,
                                 json={"line_ids": chosen, "idempotency_key": key})
        assert (preview.status_code, preview.json()) == (real.status_code, real.json())
    answer = await _ok(real)
    assert answer["acted"] == [drawn] and [s["line_id"] for s in answer["skipped"]] == [own]


async def test_a_line_whose_goods_went_out_elsewhere_says_where_they_went(client, session):
    """Shipping an available stone with one another invoice shipped: the available one ships,
    and the notice names the other line, the invoice its goods went out on and the next step.
    Red statement: the notice only said the line was sold, held or out on another record."""
    g = await _gems(client)
    owner, lids = await _record(client, g, "invoice", {"DEMO-MIN-001": "elsewhere"})
    answer = await _ok(await _act(client, g, owner, "fulfill-lines", [lids["DEMO-SAP-001"], lids["DEMO-MIN-001"]]))
    assert answer["acted"] == [lids["DEMO-SAP-001"]]
    went = g.items["DEMO-MIN-001"]
    other = (await _ok(await client.get(f"/items/{went['id']}", headers=g.h)))["status_doc_number"]
    notice, reason = answer["skipped_notice"]
    assert (notice["message_key"], notice["params"]) == ("documents.unavailable_lines_skipped_one",
                                                         {"lines": '3 "DEMO-MIN-001"'})
    assert reason["message_key"] == "lines.shipped_elsewhere" and reason["params"]["docs"] == other
    assert reason["message"].endswith(f"Revert fulfillment first: set the goods as available on {other}.")


# case -> (ui.api_client call, the action's confirm key). The page asks its confirm from the
# action's preview: the count of ``acted`` and the lines in ``skipped``, never a rule of its own.
_PAGE = {"ship-mixed": ("fulfill_lines", "documents.confirm_set_shipped"),
         "reserve-mixed": ("reserve_lines", "documents.confirm_set_reserved"),
         "set-available-mixed": ("set_lines_available", "documents.confirm_set_available"),
         "list-reserve-mixed": ("reserve_lines", "documents.confirm_set_reserved"),
         "list-set-available-mixed": ("set_lines_available", "documents.confirm_set_available")}


def _count(key: str, n: int, **params) -> str:
    from ui.i18n import t
    return t(f"{key}_{'one' if n == 1 else 'many'}", n=n, **params)


async def _page_post(ui_client, call: str, answer: dict, url: str, data: dict):
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token
    sent = AsyncMock(return_value=answer)
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch(f"ui.api_client.{call}", new=sent):
        r = await ui_client.post(url, data=data)
    return r, sent


@pytest.mark.parametrize("case", list(_PAGE))
async def test_the_page_confirm_is_the_actions_own_preview(client, session, ui_client, case):
    """Red statement: the page counted the ticked rows itself (liLineConfirm) and asked "Set 2
    lines as shipped?" for a sold line, an available line and the GIA Certificate, then the
    server shipped one or refused the lot."""
    from celerp.services.line_measures import line_name
    from ui.i18n import t
    call, confirm = _PAGE[case]
    kind, path, extra, states, chosen, acts, kinds = _CASES[case]
    g = await _gems(client)
    owner, lids = await _record(client, g, kind, states)
    send = [lids[s] for s in chosen]
    base = "lists" if kind == "list" else "docs"
    preview = await _ok(await client.post(f"/{base}/{owner}/{path}", headers=g.h, json={
        "line_ids": send, "idempotency_key": "k-page", "dry_run": True, **extra}))
    data = {"line_id": send, "idempotency_key": "k-page", **extra}
    r, sent = await _page_post(ui_client, call, preview, f"/{base}/{owner}/{path}", {**data, "dry_run": "1"})
    assert r.status_code == 200, r.text
    assert sent.await_args.kwargs["dry_run"] is True
    said = _count(confirm, len(acts))
    for k in dict.fromkeys(s["kind"] for s in sorted(preview["skipped"], key=lambda s: s["line"])):
        of_kind = sorted((s for s in preview["skipped"] if s["kind"] == k), key=lambda s: s["line"])
        said += " " + _count(f"documents.will_skip_{k}", len(of_kind),
                             lines=", ".join(line_name(s["line"], s["name"]) for s in of_kind))
    if taken := preview.get("taken_back"):
        said += " " + _count("documents.will_take_back", len(taken),
                             lines=", ".join(line_name(s["line"], s["name"]) for s in taken))
    assert r.json() == {"confirm": said, "binding": preview["binding"]}
    if case == "ship-mixed":
        assert said == ('Set 2 lines as shipped? Line 2 "DEMO-EMR-001" has already shipped, so it will be left '
                        'as it is. Line 3 "DEMO-MIN-001" has goods that aren\'t free to ship, so it will be left '
                        'as it is. Line 4 "GIA Certificate" isn\'t an inventory item, so it will be left as it is.')

    # The action itself: its notice counts the lines it acted on.
    real = await _ok(await client.post(f"/{base}/{owner}/{path}", headers=g.h, json={
        "line_ids": send, "idempotency_key": "k-page", **extra}))
    r, sent = await _page_post(ui_client, call, real, f"/{base}/{owner}/{path}", data)
    assert r.status_code == 204, r.text
    assert sent.await_args.kwargs.get("dry_run", False) is False
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]
    assert toast.startswith(_count(_DONE[path, extra.get("new_status")], len(acts)) + " ")


_DONE = {("fulfill-lines", None): "documents.lines_shipped", ("reserve-lines", "reserved"): "documents.lines_reserved",
         ("set-available", None): "documents.lines_available"}


async def test_a_refused_preview_is_shown_and_nothing_is_asked(client, session, ui_client):
    """A choice none of whose lines ships answers the preview with the action's refusal."""
    import ui.api_client as api
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token
    g = await _gems(client)
    owner, lids = await _record(client, g, "invoice", {"DEMO-EMR-001": "shipped"})
    r = await _act(client, g, owner, "fulfill-lines", [lids["DEMO-EMR-001"], lids["GIA"]], dry_run=True)
    assert r.status_code == 422, r.text
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.fulfill_lines", new=AsyncMock(side_effect=api._api_error(422, r.json(), r.text))):
        page = await ui_client.post(f"/docs/{owner}/fulfill-lines", data={
            "line_id": [lids["DEMO-EMR-001"], lids["GIA"]], "idempotency_key": "k", "dry_run": "1"})
    toast = json.loads(page.headers["HX-Trigger"])["celerpToast"]
    assert toast["type"] == "error" and "set it as available first" in toast["message"]


async def test_a_notice_naming_goods_another_invoice_had_leads_with_the_count(ui_client):
    """Red statement: the notice opened with whose goods moved and never said how many lines shipped."""
    answer = {"acted": ["a"], "taken_from": [{"sku": "DEMO-SAP-001", "doc_number": "INV-0001"}]}
    r, _ = await _page_post(ui_client, "fulfill_lines", answer, "/docs/doc:1/fulfill-lines",
                            {"line_id": ["a"], "idempotency_key": "k"})
    assert r.status_code == 204, r.text
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]
    assert toast.startswith(_count("documents.lines_shipped", 1) + " ")
    assert "INV-0001" in toast


async def test_mark_as_scanned_counts_the_lines_the_list_changed(ui_client):
    """The notice counts the rows the list says it changed, then names the row it left. Red
    statement: Mark as scanned on two rows, one already scanned, said "2 lines marked as
    scanned."."""
    from unittest.mock import AsyncMock, patch

    from celerp_docs.routes import _skip_messages
    from test_helpers import make_test_token
    from ui.i18n import refusal_text
    skipped = [{"line": 1, "name": "DEMO-SAP-001", "line_id": "a", "kind": "scanned"}]
    answer = {"changed": 1, "skipped": skipped, "skipped_notice": _skip_messages("set-scanned", skipped)}
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.set_scanned", new=AsyncMock(return_value=answer)), \
            patch("ui.api_client.get_list_page", new=AsyncMock(return_value={
                "list": {"status": "finalized", "list_type": "audit"}, "items": [], "total": 0, "version": 1})):
        r = await ui_client.post("/lists/list:1/set-scanned", data={"line_id": ["a", "b"], "scanned": "1"})
    assert r.status_code == 200, r.text
    assert json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"] == (
        f'{_count("documents.lines_scanned", 1)} {refusal_text(answer["skipped_notice"])}')


# ── Bound to its preview ──────────────────────────────────────────────────────
# The action confirms only the preview shown: the preview answers with a ``binding`` (each part
# of OUTCOME_SHOWN), and an action sent with that binding refuses (409, changing nothing) when it
# would now do anything else.

async def _sell_elsewhere(client, g, owner, lids, sku):
    other = await _doc_of(client, g, [_line(g, sku)])
    await _ok(await _act(client, g, other, "fulfill-lines", await _ids(client, g, other)))


async def _take_back_elsewhere(client, g, owner, lids, sku):
    item = g.items[sku]["id"]
    other = (await _ok(await client.get(f"/items/{item}", headers=g.h)))["status_doc_id"]
    await _ok(await _act(client, g, other, "set-available", await _ids(client, g, other)))


async def _hold_here(client, g, owner, lids, sku):
    await _ok(await _act(client, g, owner, "reserve-lines", [lids[sku]], new_status="reserved"))


async def _release_here(client, g, owner, lids, sku):
    await _ok(await _act(client, g, owner, "reserve-lines", [lids[sku]], new_status="available"))


async def _ship_here(client, g, owner, lids, sku):
    await _ok(await _act(client, g, owner, "fulfill-lines", [lids[sku]]))


async def _take_back_here(client, g, owner, lids, sku):
    await _ok(await _act(client, g, owner, "set-available", [lids[sku]]))


async def _ship_here_once_taken_back_elsewhere(client, g, owner, lids, sku):
    await _take_back_elsewhere(client, g, owner, lids, sku)
    await _ship_here(client, g, owner, lids, sku)


async def _set_aside_elsewhere(client, g, owner, lids, sku):
    await _doc_of(client, g, [_line(g, sku)])


async def _choose_too(client, g, owner, lids, sku):
    """Not a change in stock: the confirm is sent for ``sku``'s line as well."""
    return (sku,)


_SAP, _EMR, _MIN = STONES
_ACTS, _LEAVES = frozenset({"acted", "skipped"}), frozenset({"skipped"})
# (action, change made by someone else between the preview and the confirm) -> how the bound
# action answers: (record, path, extra body, stone states, chosen skus, change, its sku, and
# the parts of the binding (OUTCOME_SHOWN) the change alters, so the action is refused as changed
# since the preview, or "refused": the action's own refusal), or why the change cannot make that
# action do anything its preview did not say. A change whose method returns skus sends the
# confirm for those lines as well.
_RACES = {
    ("ship", "grows"): ("invoice", "fulfill-lines", {}, {_MIN: "elsewhere"}, (_SAP, _MIN, "GIA"),
                        _take_back_elsewhere, _MIN, _ACTS | {"taken_from"}),
    ("ship", "shrinks"): ("invoice", "fulfill-lines", {}, {}, (_SAP, _MIN, "GIA"), _sell_elsewhere, _MIN, _ACTS),
    ("ship", "takes back"): "Set as shipped never takes goods back.",
    ("reserve", "grows"): "A line that cannot be reserved refuses the whole choice, so no preview leaves one.",
    ("reserve", "shrinks"): ("invoice", "reserve-lines", {"new_status": "reserved"}, {}, (_SAP, _EMR, "GIA"),
                             _sell_elsewhere, _EMR, "refused"),
    ("reserve", "takes back"): ("invoice", "reserve-lines", {"new_status": "reserved"}, {}, (_SAP, _EMR, "GIA"),
                                _ship_here, _EMR, frozenset({"taken_back"})),
    ("release", "grows"): "A line holding nothing refuses the whole choice, so no preview leaves one.",
    ("release", "shrinks"): ("invoice", "reserve-lines", {"new_status": "available"}, {_SAP: "held", _EMR: "held"},
                             (_SAP, _EMR), _release_here, _EMR, "refused"),
    ("release", "takes back"): "Releasing a hold never takes goods back.",
    ("set-available", "grows"): ("invoice", "set-available", {}, {_SAP: "held"}, (_SAP, _MIN, "GIA"),
                                 _hold_here, _MIN, _ACTS),
    ("set-available", "shrinks"): ("invoice", "set-available", {}, {_SAP: "held", _MIN: "held"}, (_SAP, _MIN, "GIA"),
                                   _release_here, _MIN, _ACTS),
    ("set-available", "takes back"): ("invoice", "set-available", {}, {_SAP: "held", _MIN: "held"},
                                      (_SAP, _MIN, "GIA"), _ship_here, _MIN, frozenset({"taken_back"})),
    ("revert", "grows"): "A line with nothing out refuses the whole choice, so no preview leaves one.",
    ("revert", "shrinks"): ("invoice", "revert-lines", {}, {_SAP: "shipped", _EMR: "shipped"}, (_SAP, _EMR, "GIA"),
                            _take_back_here, _EMR, "refused"),
    ("revert", "takes back"): "Each line it acts on is taken back, so a line taken back is a line it acts on (grows).",
    ("list reserve", "grows"): "A line that cannot be reserved refuses the whole choice, so no preview leaves one.",
    ("list reserve", "shrinks"): ("list", "reserve-lines", {"new_status": "reserved"}, {}, (_SAP, _EMR, "GIA"),
                                  _sell_elsewhere, _EMR, "refused"),
    ("list reserve", "takes back"): "A List never ships, so it has no goods to take back.",
    ("list release", "grows"): "A line holding nothing refuses the whole choice, so no preview leaves one.",
    ("list release", "shrinks"): ("list", "reserve-lines", {"new_status": "available"}, {_SAP: "held", _EMR: "held"},
                                  (_SAP, _EMR), _release_here, _EMR, "refused"),
    ("list release", "takes back"): "A List never ships, so it has no goods to take back.",
    ("list set-available", "grows"): ("list", "set-available", {}, {_SAP: "held"}, (_SAP, _MIN, "GIA"),
                                      _hold_here, _MIN, _ACTS),
    ("list set-available", "shrinks"): ("list", "set-available", {}, {_SAP: "held", _MIN: "held"},
                                        (_SAP, _MIN, "GIA"), _release_here, _MIN, _ACTS),
    ("list set-available", "takes back"): "A List never ships, so it has no goods to take back.",
    # Parts of the binding no change above alters alone; only Set as shipped's answer can.
    ("ship", "leaves for another reason"): ("invoice", "fulfill-lines", {}, {_MIN: "elsewhere"}, (_SAP, _MIN, "GIA"),
                                            _ship_here_once_taken_back_elsewhere, _MIN, _LEAVES),
    ("ship", "takes from another invoice"): ("invoice", "fulfill-lines", {}, {}, (_SAP, "GIA"),
                                             _set_aside_elsewhere, _SAP, frozenset({"taken_from"})),
    ("ship", "is confirmed for more lines"): ("invoice", "fulfill-lines", {}, {}, (_SAP, "GIA"),
                                              _choose_too, _MIN, frozenset({"acted"})),
}


async def test_every_stock_line_action_meets_every_change_between_preview_and_action():
    """Each line action with a preview, and each way the state can change before its confirm,
    has a case above or the reason it cannot happen."""
    actions = {"ship": "fulfill-lines", "reserve": "reserve-lines", "release": "reserve-lines",
               "set-available": "set-available", "revert": "revert-lines", "list reserve": "reserve-lines",
               "list release": "reserve-lines", "list set-available": "set-available"}
    matrix = {(a, c) for a in actions for c in ("grows", "shrinks", "takes back")}
    assert set(_RACES) >= matrix
    assert all(not isinstance(_RACES[k], str) for k in set(_RACES) - matrix)
    previewed = {(base, r.path.rsplit("/", 1)[-1]) for base, r in _previewed_line_actions()}
    covered = {("lists" if a.startswith("list") else "docs", p) for a, p in actions.items()}
    assert previewed == covered | {("docs", "back-to-memo")}


async def test_every_part_of_the_binding_has_a_change_that_alters_it_alone():
    """Each part of what a preview binds has a race above in which it alone changes, so no part
    can drop out of the binding unnoticed; a part added without one fails here."""
    from celerp_docs.routes import OUTCOME_SHOWN
    raced = [v[-1] for v in _RACES.values() if not isinstance(v, str) and v[-1] != "refused"]
    assert set().union(*raced) <= set(OUTCOME_SHOWN)
    assert [p for p in OUTCOME_SHOWN if {p} not in raced] == []


async def test_the_question_words_why_it_leaves_each_kind_of_line():
    """Every kind of line an action with a question can leave has its own words there, so a
    line left for another reason changes the question too."""
    from celerp_docs.doc_constants import LINE_SKIP_REASONS
    from ui.i18n import _load
    en = _load("en")
    kinds = {k for action in {_RACE_ACTIONS[p] for p in _CONFIRMS} for k in LINE_SKIP_REASONS[action]}
    assert [f"{k}_{n}" for k in sorted(kinds) for n in ("one", "many") if f"documents.will_skip_{k}_{n}" not in en] == []


def _previewed_line_actions():
    """Every docs and lists route a stock line action runs on: its body names lines by line id
    and has a preview (``dry_run``). The memo draw binds its own preview (test_preview_binding)."""
    import typing

    from celerp_docs.routes import binds_its_outcome, lists_router, router
    for base, rt in (("docs", router), ("lists", lists_router)):
        for r in rt.routes:
            for hint in typing.get_type_hints(getattr(r, "endpoint", None) or (lambda: None)).values():
                if isinstance(hint, type) and binds_its_outcome(hint):
                    yield base, r


# The question the page asks before each action with a race above (documents.py _line_action_proxy).
_RACE_ACTIONS = {"fulfill-lines": "fulfill", "set-available": "set-available", "reserve-lines": "reserve"}
_CONFIRMS = {"fulfill-lines": "documents.confirm_set_shipped", "set-available": "documents.confirm_set_available",
             "reserve-lines": "documents.confirm_set_{new_status}"}


@pytest.mark.parametrize("race", [k for k, v in _RACES.items() if not isinstance(v, str)],
                         ids=lambda k: " ".join(k))
async def test_an_action_does_only_what_its_preview_said(client, session, race):
    """Someone changes the stock between the preview and the confirm. The confirmed action,
    bound to its preview, changes nothing and says the lines changed (or gives its own refusal);
    a fresh preview then binds the action to what it now does. Red statement: Set as shipped
    confirmed "Set 1 line as shipped?" and shipped 2 when a line was taken back elsewhere
    meanwhile, and Set as reserved reversed a sale its confirm never mentioned."""
    from celerp_docs.routes import OUTCOME_SHOWN
    from ui.routes.documents import _preview_confirm
    kind, path, extra, states, chosen, change, sku, answer = _RACES[race]
    g = await _gems(client)
    owner, lids = await _record(client, g, kind, states)
    base = "lists" if kind == "list" else "docs"
    url, key = f"/{base}/{owner}/{path}", uuid.uuid4().hex
    body = {"line_ids": [lids[s] for s in chosen], "idempotency_key": key, **extra}
    preview = await _ok(await client.post(url, headers=g.h, json={**body, "dry_run": True}))
    assert preview.get("binding"), preview
    body["line_ids"] += [lids[s] for s in await change(client, g, owner, lids, sku) or ()]
    before = await _snapshot(client, session, g, owner)
    bound = await client.post(url, headers=g.h, json={**body, "binding": preview["binding"]})
    assert await _snapshot(client, session, g, owner) == before, "the refused action changed something"
    if answer != "refused":
        assert bound.status_code == 409, bound.text
        assert bound.json()["detail"]["message_key"] == "lines.preview_differs"
        fresh = await client.post(url, headers=g.h, json={**body, "dry_run": True})
        assert fresh.status_code == 200, fresh.text
        assert {p for p, show in OUTCOME_SHOWN.items() if show(fresh.json()) != show(preview)} == answer
        confirm = _CONFIRMS[path].format(**extra)
        assert _preview_confirm(confirm, fresh.json()) != _preview_confirm(confirm, preview), \
            "the fresh question repeats the one answered"
        real = await client.post(url, headers=g.h, json={**body, "binding": fresh.json()["binding"]})
        assert (real.status_code, real.json()) == (200, fresh.json())
        assert_described(("POST", f"/{base}/{{entity_id}}/{path}"), real.json())
    else:
        assert 400 <= bound.status_code < 500 and bound.status_code != 409, bound.text


async def test_back_to_memo_bound_to_its_preview_refuses_when_its_line_went_back_meanwhile(client, session):
    from test_item_less_line_actions import _drawn_invoice_with_own_certificate
    g = await _gems(client)
    _memo, invoice, drawn, own = await _drawn_invoice_with_own_certificate(client, g)
    url, body = f"/docs/{invoice}/back-to-memo", {"line_ids": [drawn, own], "idempotency_key": uuid.uuid4().hex}
    preview = await _ok(await client.post(url, headers=g.h, json={**body, "dry_run": True}))
    await _ok(await client.post(url, headers=g.h, json={"line_ids": [drawn], "idempotency_key": uuid.uuid4().hex}))
    before = await _state(client, g, invoice)
    bound = await client.post(url, headers=g.h, json={**body, "binding": preview["binding"]})
    assert 400 <= bound.status_code < 500, bound.text
    assert await _state(client, g, invoice) == before


async def test_an_action_sent_without_a_binding_still_names_the_sale_it_reverses(client, session):
    """An API caller that sent no binding saw no confirm: the answer still names every line
    whose goods came back into stock and whose sale was reversed, in its notice too."""
    g = await _gems(client)
    owner, lids = await _record(client, g, "invoice", {_EMR: "shipped"})
    answer = await _ok(await _act(client, g, owner, "reserve-lines", [lids[_SAP], lids[_EMR]],
                                  new_status="reserved"))
    assert [(t["line"], t["line_id"]) for t in answer["taken_back"]] == [(2, lids[_EMR])]
    assert [(n["message_key"], n["params"]) for n in answer["taken_back_notice"]] == [
        ("documents.taken_back_one", {"lines": f'2 "{_EMR}"'})]


async def test_the_page_sends_the_action_bound_and_asks_again_when_the_lines_changed(ui_client):
    """The page's action carries the binding of the confirm the user answered. Refused because
    the lines changed meanwhile, it changes nothing and the page answers the fresh question,
    saying so, with the fresh binding; answered, the action runs bound to that."""
    import ui.api_client as api
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token
    from ui.i18n import t
    differs = {"detail": {"message_key": "lines.preview_differs", "message": "changed", "params": {}}}
    fresh = {"acted": ["a"], "skipped": [{"line": 2, "name": "DEMO-EMR-001", "line_id": "b", "kind": "unavailable"}],
             "binding": "fresh"}
    sent = AsyncMock(side_effect=[api._api_error(409, differs, "changed"), fresh])
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.fulfill_lines", new=sent):
        r = await ui_client.post("/docs/doc:1/fulfill-lines",
                                 data={"line_id": ["a", "b"], "idempotency_key": "k", "binding": "stale"})
    assert r.status_code == 200, r.text
    (first, second) = sent.await_args_list
    assert first.kwargs["binding"] == "stale" and not first.kwargs.get("dry_run")
    assert second.kwargs["dry_run"] is True and second.kwargs["binding"] is None
    assert r.json() == {"binding": "fresh", "confirm": (
        f'{t("documents.lines_changed_since_confirm")} {_count("documents.confirm_set_shipped", 1)} '
        f'{_count("documents.will_skip_unavailable", 1, lines=chr(50) + " " + chr(34) + "DEMO-EMR-001" + chr(34))}')}
