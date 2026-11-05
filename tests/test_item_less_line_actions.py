# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Bulk line actions skip lines that have no stock to act on, and say which.

A line with no inventory item (a certificate, a free-text charge) has no status, so Ship,
Reserve, Set as available and Take back act on the stock lines chosen with it and name it
in a notice. A service line is counted fulfilled on Ship and skipped by the other stock
actions, in its own words. A choice of only such lines is refused with the same words.
Memo Invoice selected, Back to memo and Set as scanned skip and name such lines too, each with
its own reason (it stays on the memo; it did not come from a memo; there is nothing to scan),
read from the one table of skip reasons (LINE_SKIP_REASONS).

The documents are the gemstones demo company's: five demo stones and a "GIA Certificate"."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from connector_test_support import connector_session  # noqa: F401
from memo_draw_support import back_to_memo, draw, drawn_lines
from test_cost_restatement import _doc_cogs

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ui_client():
    from httpx import ASGITransport, AsyncClient

    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c

STONES = ("DEMO-SAP-001", "DEMO-EMR-001", "DEMO-MIN-001", "DEMO-PRL-001", "DEMO-JWL-001")
GIA = {"description": "GIA Certificate", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}
ENGRAVING = {"description": "Engraving", "quantity": 1, "unit_price": 40.0, "sell_by": "piece"}
SVC = "SVC-SETTING"


async def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


async def _gems(client) -> SimpleNamespace:
    """A gemstones company with its demo stock, and a setting service it sells."""
    email = f"gems-{uuid.uuid4().hex[:8]}@test.example"
    r = await _ok(await client.post("/auth/register", json={
        "company_name": "Gem House", "email": email, "name": "Owner", "password": "pwvalid1"}))
    h = {"Authorization": f"Bearer {r['access_token']}"}
    await _ok(await client.post("/companies/me/business-type", headers=h, json={"vertical": "gemstones"}))
    await _ok(await client.post("/companies/me/demo/reseed", headers=h))
    company_id = uuid.UUID((await _ok(await client.get("/companies/me", headers=h)))["id"])
    items = {i["sku"]: i for i in (await _ok(await client.get("/items", headers=h, params={"limit": 500})))["items"]}
    svc = await _ok(await client.post("/items", headers=h, json={
        "sku": SVC, "name": "Stone setting", "quantity": 0, "sell_by": "piece", "inventory_type": "service", "status": "available"}))
    items[SVC] = {"id": svc["id"], "sku": SVC, "name": "Stone setting", "cost_total": 0}
    return SimpleNamespace(h=h, company_id=company_id, items=items, email=email)


def _line(g, sku: str, price: float = 1000.0) -> dict:
    it = g.items[sku]
    return {"item_id": it["id"], "sku": sku, "description": it["name"], "quantity": 1,
            "unit_price": price, "sell_by": "piece"}


async def _doc(client, g, lines: list[dict], *, doc_type: str = "invoice") -> str:
    d = (await _ok(await client.post("/docs", headers=g.h, json={"doc_type": doc_type, "line_items": lines})))["id"]
    await _ok(await client.post(f"/docs/{d}/finalize", headers=g.h))
    return d


async def _list(client, g, lines: list[dict]) -> str:
    q = (await _ok(await client.post("/lists", headers=g.h, json={"list_type": "quotation"})))["id"]
    v = (await _ok(await client.get(f"/lists/{q}", headers=g.h)))["version"]
    await _ok(await client.patch(f"/lists/{q}", headers=g.h, json={
        "fields_changed": {"line_items": {"new": lines}}, "expected_version": v}))
    return q


async def _state(client, g, entity_id: str) -> dict:
    base = "lists" if entity_id.startswith("list:") else "docs"
    return await _ok(await client.get(f"/{base}/{entity_id}", headers=g.h))


async def _ids(client, g, entity_id: str) -> list[str]:
    return [li["line_id"] for li in (await _state(client, g, entity_id))["line_items"]]


async def _status(client, g, sku: str) -> str:
    return (await _ok(await client.get(f"/items/{g.items[sku]['id']}", headers=g.h)))["status"]


async def _act(client, g, entity_id: str, path: str, line_ids: list[str], **extra):
    base = "lists" if entity_id.startswith("list:") else "docs"
    return await client.post(f"/{base}/{entity_id}/{path}", headers=g.h, json={
        "line_ids": line_ids, "idempotency_key": uuid.uuid4().hex, **extra})


def _detail(r) -> dict:
    detail = r.json().get("detail")
    return detail if isinstance(detail, dict) else {}


def _notice(answer: dict) -> list[tuple[str, dict]]:
    return [(n["message_key"], n["params"]) for n in answer.get("skipped_notice") or []]


async def test_select_all_ship_on_mixed_invoice_ships_stock_and_reports_item_less_line(client, session):
    g = await _gems(client)
    d = await _doc(client, g, [*(_line(g, s) for s in STONES), GIA])
    lids = await _ids(client, g, d)
    answer = await _ok(await _act(client, g, d, "fulfill-lines", lids))
    assert [await _status(client, g, s) for s in STONES] == ["sold"] * 5
    assert answer["skipped"] == [{"line": 6, "name": "GIA Certificate", "kind": "no_item", "line_id": lids[5]}]
    assert _notice(answer) == [("documents.lines_skipped_one", {"lines": '6 "GIA Certificate"'})]
    assert await _doc_cogs(session, {"company_id": g.company_id}, d) == sum(g.items[s]["cost_total"] for s in STONES)
    assert (await _state(client, g, d))["fulfillment_status"] == "fulfilled"


# ── Every stock action skips and names what it does not act on ───────────────

async def _reserve(client, g, owner, lids):
    await _ok(await _act(client, g, owner, "reserve-lines", lids, new_status="reserved"))


async def _ship(client, g, owner, lids):
    await _ok(await _act(client, g, owner, "fulfill-lines", lids))


# (owner kind, setup on the stone line, action path, extra body, stone status after, service skipped)
_SKIP_CASES = {
    "fulfil": ("invoice", None, "fulfill-lines", {}, "sold", False),
    "reserve": ("invoice", None, "reserve-lines", {"new_status": "reserved"}, "reserved", True),
    "release": ("invoice", _reserve, "reserve-lines", {"new_status": "available"}, "available", True),
    "set-available": ("invoice", _reserve, "set-available", {}, "available", True),
    "revert": ("invoice", _ship, "revert-lines", {}, "available", True),
    "list-reserve": ("list", None, "reserve-lines", {"new_status": "reserved"}, "reserved", True),
    "list-release": ("list", _reserve, "reserve-lines", {"new_status": "available"}, "available", True),
    "list-set-available": ("list", _reserve, "set-available", {}, "available", True),
}


@pytest.mark.parametrize("case", list(_SKIP_CASES))
async def test_stock_line_actions_skip_item_less_lines(client, session, case):
    kind, setup, path, extra, after, service_skipped = _SKIP_CASES[case]
    g = await _gems(client)
    lines = [_line(g, "DEMO-SAP-001"), GIA, _line(g, SVC, 200.0)]
    owner = await (_list(client, g, lines) if kind == "list" else _doc(client, g, lines))
    lids = await _ids(client, g, owner)
    if setup:
        await setup(client, g, owner, lids[:1])
    answer = await _ok(await _act(client, g, owner, path, lids, **extra))
    assert await _status(client, g, "DEMO-SAP-001") == after
    expected = [{"line": 2, "name": "GIA Certificate", "kind": "no_item", "line_id": lids[1]}]
    notice = [("documents.lines_skipped_one", {"lines": '2 "GIA Certificate"'})]
    if service_skipped:
        expected.append({"line": 3, "name": SVC, "kind": "service", "line_id": lids[2]})
        notice.append(("documents.service_lines_skipped_one", {"lines": f'3 "{SVC}"'}))
    assert answer["skipped"] == expected
    assert _notice(answer) == notice


# ── A choice of only such lines is refused, in the same words ────────────────

# (owner kind, action path, extra body); the memo draw is a 409 with the other draw problems.
_REFUSE_CASES = {
    "fulfil": ("invoice", "fulfill-lines", {}),
    "reserve": ("invoice", "reserve-lines", {"new_status": "reserved"}),
    "release": ("invoice", "reserve-lines", {"new_status": "available"}),
    "list-reserve": ("list", "reserve-lines", {"new_status": "reserved"}),
    "list-release": ("list", "reserve-lines", {"new_status": "available"}),
    "set-available": ("invoice", "set-available", {}),
    "list-set-available": ("list", "set-available", {}),
    "revert": ("invoice", "revert-lines", {}),
    "memo-draw": ("memo", "invoice-lines", {}),
}


@pytest.mark.parametrize("many", [False, True], ids=["one", "many"])
@pytest.mark.parametrize("case", list(_REFUSE_CASES))
async def test_stock_line_actions_refuse_when_only_item_less_lines(client, session, case, many):
    kind, path, extra = _REFUSE_CASES[case]
    g = await _gems(client)
    lines = [*(_line(g, s) for s in STONES), GIA, ENGRAVING]
    if kind == "list":
        owner = await _list(client, g, lines)
    else:
        owner = await _doc(client, g, lines, doc_type="memo" if kind == "memo" else "invoice")
    lids = await _ids(client, g, owner)
    if kind == "memo":
        await _ship(client, g, owner, lids[:5])
    before = await _state(client, g, owner)
    chosen = lids[5:] if many else lids[5:6]
    r = await (draw(client, g.h, owner, line_ids=chosen) if kind == "memo"
               else _act(client, g, owner, path, chosen, **extra))
    assert r.status_code == (409 if kind == "memo" else 422), r.text
    detail = _detail(r)
    # Invoice selected says the line stays on the memo; the status actions, that it has no status.
    key = "lines.memo_no_stock" if kind == "memo" else "lines.no_stock"
    if many:
        assert (detail["message_key"], detail["params"]) == (
            f"{key}_many", {"lines": '6 "GIA Certificate", 7 "Engraving"'})
    else:
        assert (detail["message_key"], detail["params"]) == (f"{key}_one", {"lines": '6 "GIA Certificate"'})
    after = await _state(client, g, owner)
    assert after["version"] == before["version"] and after["line_items"] == before["line_items"]


# ── Set as available names a stocked line it leaves as it is ─────────────────

@pytest.mark.parametrize("kind", ["invoice", "list"])
async def test_set_available_names_a_line_with_nothing_held_or_out(client, session, kind):
    g = await _gems(client)
    lines = [_line(g, "DEMO-SAP-001"), _line(g, "DEMO-EMR-001"), GIA]
    owner = await (_list(client, g, lines) if kind == "list" else _doc(client, g, lines))
    lids = await _ids(client, g, owner)
    await _reserve(client, g, owner, lids[:1])
    answer = await _ok(await _act(client, g, owner, "set-available", lids))
    assert [await _status(client, g, s) for s in ("DEMO-SAP-001", "DEMO-EMR-001")] == ["available"] * 2
    assert _notice(answer) == [("documents.lines_skipped_one", {"lines": '3 "GIA Certificate"'}),
                               ("documents.idle_lines_skipped_one", {"lines": '2 "DEMO-EMR-001"'})]
    before = await _state(client, g, owner)
    r = await _act(client, g, owner, "set-available", lids[1:2])
    assert r.status_code == 422, r.text
    assert (_detail(r)["message_key"], _detail(r)["params"]) == ("documents.idle_lines_skipped_one", {"lines": '2 "DEMO-EMR-001"'})
    # A stone with nothing held and the certificate: an inventory line was chosen, so the
    # refusal says there was nothing to change rather than to choose the inventory lines.
    r = await _act(client, g, owner, "set-available", lids[1:])
    assert r.status_code == 422, r.text
    detail = _detail(r)
    assert [(m["message_key"], m["params"]) for m in [detail, *detail["also"]]] == [
        ("documents.lines_skipped_one", {"lines": '3 "GIA Certificate"'}),
        ("documents.idle_lines_skipped_one", {"lines": '2 "DEMO-EMR-001"'})]
    assert "Choose the inventory lines" not in " ".join(m["message"] for m in [detail, *detail["also"]])
    after = await _state(client, g, owner)
    assert after["version"] == before["version"] and after["line_items"] == before["line_items"]


async def test_service_lines_get_their_own_wording(client, session, ui_client):
    import ui.api_client as api
    from test_helpers import make_test_token
    from ui.i18n import refusal_text
    g = await _gems(client)
    d = await _doc(client, g, [_line(g, "DEMO-SAP-001"), _line(g, SVC, 200.0), GIA])
    stone, svc, gia = await _ids(client, g, d)
    answer = await _ok(await _act(client, g, d, "reserve-lines", [stone, svc], new_status="reserved"))
    assert _notice(answer) == [("documents.service_lines_skipped_one", {"lines": f'2 "{SVC}"'})]
    await _ok(await _act(client, g, d, "reserve-lines", [stone], new_status="available"))

    r = await _act(client, g, d, "reserve-lines", [svc], new_status="reserved")
    assert r.status_code == 422, r.text
    assert (_detail(r)["message_key"], _detail(r)["params"]) == ("lines.service_no_stock_one", {"lines": f'2 "{SVC}"'})

    r = await _act(client, g, d, "reserve-lines", [svc, gia], new_status="reserved")
    assert r.status_code == 422, r.text
    detail = _detail(r)
    assert detail["message_key"] == "lines.no_stock_one" and detail["params"] == {"lines": '3 "GIA Certificate"'}
    assert [(a["message_key"], a["params"]) for a in detail["also"]] == [
        ("lines.service_no_stock_one", {"lines": f'2 "{SVC}"'})]

    # The page shows both sentences in its toast.
    refused = AsyncMock(side_effect=api._api_error(422, r.json(), r.text))
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.reserve_lines", new=refused):
        page = await ui_client.post(f"/docs/{d}/reserve-lines", data={
            "line_id": [svc, gia], "new_status": "reserved", "idempotency_key": "k-svc"})
    toast = json.loads(page.headers["HX-Trigger"])["celerpToast"]["message"]
    assert refusal_text(detail) in toast and refusal_text(detail["also"]) in toast


# ── Callers outside the page ─────────────────────────────────────────────────

async def test_fulfill_impl_skips_item_less_lines_for_every_caller(client, session):
    """Store order sync and peer replay call the ship primitive directly: it ships the stock,
    counts the service line fulfilled and names the line with no item."""
    from celerp_docs.routes import _fulfill_lines_impl
    g = await _gems(client)
    d = await _doc(client, g, [_line(g, "DEMO-SAP-001"), _line(g, SVC, 200.0), GIA])
    from celerp.models.company import User
    user_id = (await session.execute(select(User.id).where(User.email == g.email))).scalar_one()
    result = await _fulfill_lines_impl(d, [0, 1, 2], str(g.company_id), SimpleNamespace(id=user_id), session)
    assert await _status(client, g, "DEMO-SAP-001") == "sold"
    assert result["skipped"] == [{"line": 3, "name": "GIA Certificate", "kind": "no_item",
                                  "line_id": (await _ids(client, g, d))[2]}]
    state = await _state(client, g, d)
    assert g.items[SVC]["id"] in {fi.get("item_id") for fi in state["fulfilled_items"]}
    assert state["fulfillment_status"] == "fulfilled"


async def test_completed_woo_order_with_a_service_line_still_reaches_fulfilled(connector_session, client, auth):
    import celerp.connectors.upsert as connector
    from sqlalchemy import update

    from celerp.models.accounting import UserCompany
    from test_invoice_unshipped_books import _lot
    session, cid = connector_session, str(auth["company_id"])
    await session.execute(update(UserCompany).where(UserCompany.company_id == auth["company_id"]).values(role="owner"))
    sku, svc_sku = f"WCS-{uuid.uuid4().hex[:6]}", f"WCV-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 1, 40.0)
    svc = await _ok(await client.post("/items", headers=auth["headers"], json={
        "sku": svc_sku, "name": "Gift wrap", "quantity": 0, "sell_by": "piece", "inventory_type": "service", "status": "available"}))
    order = {"id": 961, "number": "961", "status": "completed", "currency": "USD", "total": "20.00", "total_tax": "0",
             "line_items": [
                 {"product_id": 962, "variation_id": 0, "sku": sku, "name": "Stone", "quantity": 1,
                  "total": "15.00", "total_tax": "0"},
                 {"product_id": 963, "variation_id": 0, "sku": svc_sku, "name": "Gift wrap", "quantity": 1,
                  "total": "5.00", "total_tax": "0", "virtual": True}],
             "shipping_lines": [], "fee_lines": []}
    assert await connector.upsert_order_from_woocommerce(cid, order) == "created"
    session.expire_all()
    [doc] = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "doc"))).scalars().all()
    assert doc.state["fulfillment_status"] == "fulfilled"
    assert {fi.get("item_id") for fi in doc.state["fulfilled_items"]} == {lot, svc["id"]}


async def test_completed_woo_order_that_cannot_ship_every_line_is_refused_whole(connector_session, client, auth):
    """A completed order never syncs with a line left unshipped: two order lines of one
    single-unit lot are refused before anything ships, so the shipping step never skips one."""
    import celerp.connectors.upsert as connector
    from sqlalchemy import update

    from celerp.models.accounting import UserCompany
    from test_invoice_unshipped_books import _lot
    session, cid = connector_session, str(auth["company_id"])
    await session.execute(update(UserCompany).where(UserCompany.company_id == auth["company_id"]).values(role="owner"))
    sku = f"WCS-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 1, 40.0)
    line = {"product_id": 972, "variation_id": 0, "sku": sku, "name": "Stone", "quantity": 1,
            "total": "15.00", "total_tax": "0"}
    order = {"id": 971, "number": "971", "status": "completed", "currency": "USD", "total": "30.00", "total_tax": "0",
             "line_items": [line, {**line, "id": 2}], "shipping_lines": [], "fee_lines": []}
    with pytest.raises(ValueError, match="requires 2, but only 1 is sellable"):
        await connector.upsert_order_from_woocommerce(cid, order)
    session.expire_all()
    assert (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})).state["status"] != "sold"


async def test_the_lines_an_action_leaves_are_listed_in_line_order(client, session):
    """Red statement: ``skipped`` listed the lines by why they were left, so the item-less line 2
    came before the shipped line 1."""
    g = await _gems(client)
    d = await _doc(client, g, [_line(g, "DEMO-SAP-001"), GIA, _line(g, "DEMO-EMR-001")])
    stone, gia, emerald = await _ids(client, g, d)
    await _ship(client, g, d, [stone])
    answer = await _ok(await _act(client, g, d, "fulfill-lines", [emerald, gia, stone]))
    assert [(s["line"], s["kind"]) for s in answer["skipped"]] == [(1, "shipped"), (2, "no_item")]


async def test_by_item_take_back_is_unchanged(client, session):
    """Take back naming items, not lines, takes back that item's lots and skips nothing,
    with an item-less charge on the memo."""
    g = await _gems(client)
    memo = await _doc(client, g, [_line(g, "DEMO-SAP-001"), GIA], doc_type="memo")
    stone, _gia = await _ids(client, g, memo)
    await _ship(client, g, memo, [stone])
    assert await _status(client, g, "DEMO-SAP-001") == "memo_out"
    r = await client.post(f"/docs/{memo}/revert-lines", headers=g.h,
                          json={"line_entity_ids": [g.items["DEMO-SAP-001"]["id"]]})
    answer = await _ok(r)
    assert await _status(client, g, "DEMO-SAP-001") == "available"
    assert answer.get("skipped", []) == []


async def test_skipped_lines_replay_with_the_same_key(client, session):
    g = await _gems(client)
    d = await _doc(client, g, [_line(g, "DEMO-SAP-001"), GIA])
    stone, gia = await _ids(client, g, d)
    body = {"line_ids": [stone, gia], "idempotency_key": "same-key"}
    first = await _ok(await client.post(f"/docs/{d}/fulfill-lines", headers=g.h, json=body))
    again = await _ok(await client.post(f"/docs/{d}/fulfill-lines", headers=g.h, json=body))
    assert first == again and _notice(again) == [("documents.lines_skipped_one", {"lines": '2 "GIA Certificate"'})]
    session.expire_all()
    [recorded] = [e for e in (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == g.company_id, LedgerEntry.event_type == "line_action.recorded"))).scalars()
        if e.data.get("owner_id") == d]
    assert recorded.data["line_ids"] == [stone]


async def test_by_item_selector_skips_service_item(client, session):
    g = await _gems(client)
    d = await _doc(client, g, [_line(g, "DEMO-SAP-001"), _line(g, SVC, 200.0)])
    answer = await _ok(await client.post(f"/docs/{d}/reserve-lines", headers=g.h, json={
        "line_entity_ids": [g.items["DEMO-SAP-001"]["id"], g.items[SVC]["id"]], "new_status": "reserved"}))
    assert await _status(client, g, "DEMO-SAP-001") == "reserved"
    assert answer["skipped"] == [{"line": 2, "name": SVC, "kind": "service", "line_id": (await _ids(client, g, d))[1]}]


# ── Return goods names lines by id ───────────────────────────────────────────

async def test_return_goods_names_lines_by_id_only(client, session, ui_client):
    from test_helpers import make_test_token
    g = await _gems(client)
    r = await client.post("/docs/doc:any/return-items", headers=g.h,
                          json={"lines": [{"line_index": 0, "quantity_returned": 1}]})
    assert r.status_code == 422, r.text
    sent = AsyncMock(return_value={})
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.return_goods", new=sent):
        await ui_client.post("/docs/doc:any/return-goods", data={
            "qty_0": "1", "line_id_0": "11111111-1111-4111-8111-111111111111", "qty_1": "2", "line_id_1": ""})
    [lines] = [a[2]["lines"] for a in [sent.await_args.args]]
    assert [ln["line_id"] for ln in lines] == ["11111111-1111-4111-8111-111111111111", ""]
    assert all("line_index" not in ln for ln in lines)


# ── Memo Invoice selected and Back to memo ───────────────────────────────────

async def test_invoice_selected_skips_item_less_memo_line_and_names_it(client, session, ui_client):
    from test_helpers import make_test_token
    from ui.components.preview_confirm import BINDING
    from ui.i18n import refusal_text, t
    g = await _gems(client)
    memo = await _doc(client, g, [_line(g, "DEMO-SAP-001"), _line(g, "DEMO-EMR-001"), GIA], doc_type="memo")
    lids = await _ids(client, g, memo)
    await _ship(client, g, memo, lids[:2])
    preview = await _ok(await draw(client, g.h, memo, line_ids=lids, dry_run=True))
    assert len(preview["lines"]) == 2
    # The draw opens the new invoice, so the notice says the line is the memo's and stays there.
    notice = [("documents.memo_lines_skipped_one", {"lines": '3 "GIA Certificate"'})]
    assert _notice(preview) == notice
    done = await _ok(await draw(client, g.h, memo, line_ids=lids, binding=preview["binding"]))
    assert _notice(done) == notice
    memo_ref = (await _state(client, g, memo))["ref_id"]
    assert (done["invoiced"], done["memo_ref"]) == (2, memo_ref)
    invoice = await _state(client, g, done["target_doc_id"])
    assert sorted(li["sku"] for li in drawn_lines(invoice, memo)) == ["DEMO-EMR-001", "DEMO-SAP-001"]
    assert [li.get("description") for li in (await _state(client, g, memo))["line_items"]][2] == "GIA Certificate"

    sent = AsyncMock(return_value=done)
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.invoice_lines", new=sent):
        page = await ui_client.post(f"/docs/{memo}/invoice-lines", data={
            "pick_0": lids[0], "pick_1": lids[1], "pick_2": lids[2], BINDING: "b", "idempotency_key": "k-draw"})
    assert page.headers.get("HX-Redirect")
    toast = json.loads(page.headers["HX-Trigger"])["celerpToast"]
    said = t("documents.lines_invoiced_many", n=2, memo=memo_ref)
    assert toast["message"] == f'{said} {refusal_text(done["skipped_notice"])}' and toast.get("persist") is True


async def _drawn_invoice_with_own_certificate(client, g) -> tuple[str, str, str, str]:
    """A draft invoice holding a stone drawn from a memo and its own "GIA Certificate"."""
    memo = await _doc(client, g, [_line(g, "DEMO-SAP-001")], doc_type="memo")
    lids = await _ids(client, g, memo)
    await _ship(client, g, memo, lids)
    preview = await _ok(await draw(client, g.h, memo, line_ids=lids, dry_run=True))
    invoice = (await _ok(await draw(client, g.h, memo, line_ids=lids, binding=preview["binding"])))["target_doc_id"]
    st = await _state(client, g, invoice)
    await _ok(await client.patch(f"/docs/{invoice}", headers=g.h, json={
        "expected_version": st["version"], "fields_changed": {"line_items": {"new": [*st["line_items"], GIA]}}}))
    drawn, own = await _ids(client, g, invoice)
    return memo, invoice, drawn, own


async def test_back_to_memo_leaves_every_line_of_the_invoices_own_and_says_why(client, session):
    """Back to memo leaves a line that did not come from a memo, with or without an item, and
    gives that reason, never a status action's."""
    g = await _gems(client)
    memo, invoice, drawn, own = await _drawn_invoice_with_own_certificate(client, g)
    st = await _state(client, g, invoice)
    await _ok(await client.patch(f"/docs/{invoice}", headers=g.h, json={
        "expected_version": st["version"],
        "fields_changed": {"line_items": {"new": [*st["line_items"], _line(g, "DEMO-EMR-001")]}}}))
    stone = (await _ids(client, g, invoice))[2]
    answer = await _ok(await back_to_memo(client, g.h, invoice, [drawn, own, stone]))
    # Numbered as the invoice stands once the drawn stone has gone back.
    assert _notice(answer) == [("documents.own_lines_skipped_many",
                                {"lines": '1 "GIA Certificate", 2 "DEMO-EMR-001"'})]
    assert answer["skipped_notice"][0]["message"] == (
        'Lines 1 "GIA Certificate", 2 "DEMO-EMR-001" did not come from a memo, so they stay on this invoice.')
    left = (await _state(client, g, invoice))["line_items"]
    assert [li.get("description") or li.get("sku") for li in left] == ["GIA Certificate", g.items["DEMO-EMR-001"]["name"]]
    assert await _status(client, g, "DEMO-SAP-001") == "memo_out"


async def test_back_to_memo_refuses_when_only_own_item_less_lines(client, session):
    g = await _gems(client)
    memo, invoice, drawn, own = await _drawn_invoice_with_own_certificate(client, g)
    before = await _state(client, g, invoice)
    r = await back_to_memo(client, g.h, invoice, [own])
    assert r.status_code == 409, r.text
    detail = _detail(r)
    n = [li["line_id"] for li in before["line_items"]].index(own) + 1
    assert (detail["message_key"], detail["params"]) == ("docs.back_to_memo_not_drawn_one", {"lines": f'{n} "GIA Certificate"'})
    assert detail["message"].startswith(f'Line {n} "GIA Certificate" did not come from a memo')
    assert (await _state(client, g, invoice))["line_items"] == before["line_items"]


# ── Class guard: each bulk line action counts and names what it leaves, in its own words ─

async def _audit_with_certificate(client, g) -> str:
    """A finalized audit of the demo stock location, with a "GIA Certificate" row added while
    it was a draft."""
    loc = next(i["location_id"] for i in g.items.values() if i.get("location_id"))
    a = (await _ok(await client.post("/lists/audit", headers=g.h, json={"location_id": loc})))["id"]
    st = await _state(client, g, a)
    await _ok(await client.patch(f"/lists/{a}", headers=g.h, json={
        "fields_changed": {"line_items": {"new": [*st["line_items"], GIA]}}, "expected_version": st["version"]}))
    await _ok(await client.post(f"/lists/{a}/finalize", headers=g.h))
    return a


def _line_selection_routes() -> set[tuple[str, str]]:
    """Every document and list route whose body names lines by ``line_ids``: the bulk line actions."""
    from typing import get_type_hints

    from fastapi.routing import APIRoute
    from pydantic import BaseModel

    from celerp_docs.routes import lists_router, router
    found = set()
    for prefix, rt in (("/docs", router), ("/lists", lists_router)):
        for r in rt.routes:
            if not isinstance(r, APIRoute):
                continue
            for hint in get_type_hints(r.endpoint).values():
                if isinstance(hint, type) and issubclass(hint, BaseModel) and "line_ids" in hint.model_fields:
                    found |= {(m, prefix + r.path) for m in r.methods}
    return found


async def _scan(client, g, audit, lids, scanned):
    return await client.post(f"/lists/{audit}/set-scanned", headers=g.h, json={"line_ids": lids, "scanned": scanned})


async def _draw(client, g, memo, lids):
    """Invoice selected, confirmed as the page confirms it: after its preview."""
    preview = await draw(client, g.h, memo, line_ids=lids, dry_run=True)
    if preview.status_code != 200 or preview.json().get("problems"):
        return await draw(client, g.h, memo, line_ids=lids)
    return await draw(client, g.h, memo, line_ids=lids, binding=preview.json()["binding"])


async def _owner_for(client, g, kind: str) -> tuple[str, str]:
    """A record of ``kind`` holding demo stones and a "GIA Certificate", with the first stone
    in the state the action acts on; returns it and that stone's line id."""
    lines = [*(_line(g, s) for s in STONES[:2]), GIA]
    if kind == "audit":
        owner = await _audit_with_certificate(client, g)
        return owner, (await _ids(client, g, owner))[0]
    if kind == "drawn":
        _memo, owner, drawn_id, _own = await _drawn_invoice_with_own_certificate(client, g)
        return owner, drawn_id
    owner = await (_list(client, g, lines) if kind.startswith("list") else
                   _doc(client, g, lines, doc_type="memo" if kind == "memo" else "invoice"))
    lids = await _ids(client, g, owner)
    if kind in ("memo", "invoice-shipped"):
        await _ship(client, g, owner, lids[:1])
    elif kind in ("invoice-held", "list-held"):
        await _reserve(client, g, owner, lids[:1])
    return owner, lids[0]


# route -> (record it acts on, action, request, UI route and API call it goes through or None,
# key of the UI notice's count); the request gets (client, g, owner, line ids).
_ALL_SKIPPED = {
    ("POST", "/docs/{entity_id}/fulfill-lines"): ("invoice", "fulfill", lambda c, g, o, l: _act(
        c, g, o, "fulfill-lines", l), ("fulfill-lines", "fulfill_lines"), "documents.lines_shipped"),
    ("POST", "/docs/{entity_id}/reserve-lines"): ("invoice", "reserve", lambda c, g, o, l: _act(
        c, g, o, "reserve-lines", l, new_status="reserved"), ("reserve-lines", "reserve_lines"), "documents.lines_reserved"),
    ("POST", "/docs/{entity_id}/set-available"): ("invoice-held", "set-available", lambda c, g, o, l: _act(
        c, g, o, "set-available", l), ("set-available", "set_lines_available"), "documents.lines_available"),
    ("POST", "/docs/{entity_id}/revert-lines"): ("invoice-shipped", "revert", lambda c, g, o, l: _act(
        c, g, o, "revert-lines", l), None, None),
    ("POST", "/lists/{entity_id}/reserve-lines"): ("list", "reserve", lambda c, g, o, l: _act(
        c, g, o, "reserve-lines", l, new_status="reserved"), ("reserve-lines", "reserve_lines"), "documents.lines_reserved"),
    ("POST", "/lists/{entity_id}/set-available"): ("list-held", "set-available", lambda c, g, o, l: _act(
        c, g, o, "set-available", l), ("set-available", "set_lines_available"), "documents.lines_available"),
    ("POST", "/docs/{entity_id}/invoice-lines"): ("memo", "invoice", _draw, ("invoice-lines", "invoice_lines"),
                                                  "documents.lines_invoiced"),
    ("POST", "/docs/{entity_id}/back-to-memo"): ("drawn", "back_to_memo", lambda c, g, o, l: back_to_memo(
        c, g.h, o, l), ("back-to-memo", "back_to_memo"), "documents.lines_back_to_memo"),
    ("POST", "/lists/{entity_id}/set-scanned"): ("audit", "set-scanned", lambda c, g, o, l: _scan(
        c, g, o, l, True), ("set-scanned", "set_scanned"), "documents.lines_scanned"),
}
_ROUTES = [r[1] for r in _ALL_SKIPPED]


def _case(route: str) -> tuple:
    return next(v for k, v in _ALL_SKIPPED.items() if k[1] == route)


def _route_id(route: str) -> str:
    return route.split("/", 2)[1] + route.rsplit("/", 1)[1]


async def test_every_bulk_line_action_is_covered_by_the_guard_and_has_its_own_reasons(client):
    from celerp_docs.doc_constants import LINE_SKIP_REASONS
    assert _line_selection_routes() == set(_ALL_SKIPPED)
    assert {v[1] for v in _ALL_SKIPPED.values()} <= set(LINE_SKIP_REASONS)


@pytest.mark.parametrize("path", ["/docs/{entity_id}/fulfill-lines", "/docs/{entity_id}/reserve-lines",
                                  "/docs/{entity_id}/set-available", "/docs/{entity_id}/revert-lines",
                                  "/lists/{entity_id}/reserve-lines", "/lists/{entity_id}/set-available"])
async def test_the_api_reference_describes_the_lines_a_stock_action_leaves(client, path):
    """Red statement: the reference said nothing of ``skipped``, and set-available called
    itself "all or nothing" though it leaves lines with nothing to change."""
    from celerp.main import app
    app.openapi_schema = None
    said = app.openapi()["paths"][path]["post"].get("description", "")
    assert "``skipped``" in said and "``skipped_notice``" in said and "(422)" in said, said
    assert "all or nothing" not in said


def assert_described(route: tuple[str, str], answer: dict) -> None:
    """Every key of ``answer``, the answer to the bulk line action ``route``, is described in
    the API reference of that route as ``key``."""
    from celerp.main import app
    said = app.openapi()["paths"][route[1]][route[0].lower()].get("description", "")
    assert not (missing := [k for k in answer if f"``{k}``" not in said]), (route, missing)


async def test_the_api_reference_describes_every_field_a_bulk_line_action_takes(client):
    """Red statement: ``dry_run`` and ``binding`` of Set as reserved, Set as available and
    Revert had no description in the reference."""
    from celerp.main import app
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    for method, path in _line_selection_routes():
        ref = spec["paths"][path][method.lower()]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        props = schemas[ref.rsplit("/", 1)[-1]]["properties"]
        assert not (missing := [f for f, p in props.items() if not p.get("description")]), (path, missing)


async def test_the_page_never_decides_which_lines_a_stock_action_acts_on():
    """The page script carries no skip table for the stock actions: only Print labels, which
    runs in the page, names its skipped rows itself. No stock action on the toolbar asks a
    fixed question of its own; each asks its preview (test_line_action_preview). Red
    statement: the page counted the ticked rows by kinds of its own and asked about lines
    the server then refused."""
    from fasthtml.common import to_xml

    from ui.routes.documents import _li_bulk_toolbar, _line_skip_strings
    assert set(_line_skip_strings()) == {"labels_skipped"}
    html = to_xml(_li_bulk_toolbar("doc:1", False, show_fulfill=True, show_reserve=True))
    assert "hx-confirm" not in html
    assert 'onclick="liBulkFulfillConfirmed(event)"' in html


async def _page_notice(ui_client, route: str, owner: str, lids: list[str], answer: dict) -> str:
    """The notice the page shows for ``answer`` to the action ``route`` names, sent for ``lids``."""
    from test_helpers import make_test_token
    from ui.components.preview_confirm import BINDING
    path, call = _case(route)[3]
    data = {"line_id": lids, "idempotency_key": "k-guard", "new_status": "reserved", "scanned": "1"}
    if call == "invoice_lines":
        data = {**{f"pick_{i}": lid for i, lid in enumerate(lids)}, BINDING: "b", "idempotency_key": "k-guard"}
    page = {"list": {"status": "finalized"}, "items": [], "total": 0, "version": 1}
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch(f"ui.api_client.{call}", new=AsyncMock(return_value=answer)), \
            patch("ui.api_client.get_list_page", new=AsyncMock(return_value=page)):
        r = await ui_client.post(f"/{'lists' if owner.startswith('list:') else 'docs'}/{owner}/{path}", data=data)
    assert r.status_code in (200, 204), r.text
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]


@pytest.mark.parametrize("route", _ROUTES, ids=_route_id)
async def test_a_mixed_choice_is_counted_then_named_in_the_actions_own_words(client, session, ui_client, route):
    from celerp_docs.doc_constants import LINE_SKIP_REASONS
    from ui.i18n import refusal_text, t
    kind, action, send, ui, done = _case(route)
    g = await _gems(client)
    owner, acting = await _owner_for(client, g, kind)
    before = {li["line_id"]: li for li in (await _state(client, g, owner))["line_items"]
              if not (li.get("item_id") or li.get("entity_id"))}
    item_less = list(before)
    answer = await _ok(await send(client, g, owner, [acting, *item_less]))
    assert_described(next(k for k in _ALL_SKIPPED if k[1] == route), answer)
    # The lines it named as left are stored as they were, and it counts only the one it changed.
    after = {li["line_id"]: li for li in (await _state(client, g, owner))["line_items"]}
    assert {lid: after.get(lid) for lid in item_less} == before
    assert answer.get("changed", 1) == 1
    reasons = LINE_SKIP_REASONS[action]
    assert {s["kind"] for s in answer["skipped"]} <= set(reasons)
    assert [k for k, _ in _notice(answer)] == [f"{reasons[s['kind']][0]}_one" for s in answer["skipped"]]
    if ui:
        said = t(f"{done}_one", n=1, memo=answer.get("memo_ref") or "")
        assert await _page_notice(ui_client, route, owner, [acting, *item_less], answer) == (
            f"{said} {refusal_text(answer['skipped_notice'])}")


@pytest.mark.parametrize("route", _ROUTES, ids=_route_id)
async def test_a_choice_of_only_item_less_lines_changes_nothing_and_is_refused(client, session, route):
    from celerp_docs.doc_constants import LINE_SKIP_REASONS
    kind, action, send, _ui, _done = _case(route)
    g = await _gems(client)
    owner, _acting = await _owner_for(client, g, kind)
    before = await _state(client, g, owner)
    item_less = [li["line_id"] for li in before["line_items"] if not (li.get("item_id") or li.get("entity_id"))]
    assert item_less
    r = await send(client, g, owner, item_less)
    assert r.status_code in (409, 422), r.text
    # The kind an item-less line is to this action: a line of the invoice's own to Back to memo.
    skip_kind = {"back_to_memo": "own", "invoice": "memo_no_item"}.get(action, "no_item")
    assert _detail(r)["message_key"] == f"{LINE_SKIP_REASONS[action][skip_kind][1]}_one"
    after = await _state(client, g, owner)
    assert after["version"] == before["version"] and after["line_items"] == before["line_items"]


async def test_mark_and_clear_scanned_name_the_lines_already_so_and_a_no_op_says_why(client, session):
    """A chosen line already marked as scanned (or, clearing, not marked) is left as it is and
    named, like a line with nothing held to Set as available; a choice of only such lines is
    refused with that notice and changes nothing. Red statement: Mark as scanned on all three
    lines, line 1 already scanned, said "1 line marked as scanned." and named only the GIA
    Certificate; on line 1 alone it said "0 lines marked as scanned." with no reason."""
    g = await _gems(client)
    audit = await _audit_with_certificate(client, g)
    lines = (await _state(client, g, audit))["line_items"]
    lids = [li["line_id"] for li in lines]
    gia = next(i for i, li in enumerate(lines) if not li.get("item_id"))
    first, second, third = [i for i in range(len(lines)) if i != gia][:3]
    assert not any(li.get("audited_at") for li in lines)
    await _ok(await _scan(client, g, audit, [lids[first]], True))

    answer = await _ok(await _scan(client, g, audit, [lids[first], lids[second], lids[gia]], True))
    assert answer["changed"] == 1
    assert [(s["line"], s["kind"]) for s in answer["skipped"]] == sorted(
        [(first + 1, "scanned"), (gia + 1, "no_item")])
    assert [k for k, _ in _notice(answer)] == ["documents.scan_lines_skipped_one", "documents.scanned_lines_skipped_one"]

    for chosen, scanned, key in (([first], True, "documents.scanned_lines_skipped_one"),
                                 ([third], False, "documents.unscanned_lines_skipped_one"),
                                 ([first, gia], True, "documents.scan_lines_skipped_one")):
        before = await _state(client, g, audit)
        r = await _scan(client, g, audit, [lids[i] for i in chosen], scanned)
        assert r.status_code == 422, r.text
        refused = [_detail(r)["message_key"], *(m["message_key"] for m in _detail(r).get("also") or [])]
        assert key in refused, refused
        assert (await _state(client, g, audit))["version"] == before["version"]


# ── Class guard: every line table's checkbox names its line the same way ─────

def _boxes(html: str, scope: str = "") -> list:
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").select(f"{scope} .li-select".strip())


async def test_every_line_renderer_gives_its_checkbox_the_line_id_and_label(client, session):
    """The draft rows and the empty row a draft adds, the finalized rows, the audit's rows and
    the audit tbody a scan swaps in: each checkbox carries the line id an action sends and the
    label a notice names it by, and nothing the page could judge a line by (its status, a
    kind of skip); every stock action asks its preview; the swapped audit row has a cell under
    every heading."""
    from bs4 import BeautifulSoup
    from test_helpers import ui_signed_in
    g = await _gems(client)
    lines = [*(_line(g, s) for s in STONES[:2]), GIA, _line(g, SVC, 200.0)]
    draft = (await _ok(await client.post("/docs", headers=g.h, json={"doc_type": "invoice", "line_items": lines})))["id"]
    final = await _doc(client, g, lines)
    audit = await _audit_with_certificate(client, g)
    async with ui_signed_in(g.h["Authorization"].split()[1]) as ui:
        pages = {k: (await ui.get(f"/{p}/{e}")).text
                 for k, p, e in (("draft", "docs", draft), ("final", "docs", final), ("audit", "lists", audit))}
        swap = await ui.post(f"/lists/{audit}/set-scanned", data={
            "scanned": "1", "line_id": (await _ids(client, g, audit))[0], "offset": "0", "limit": "100"})
        assert swap.status_code == 200, swap.text
        pages["audit swap"] = swap.text
    for name, html in pages.items():
        state = await _state(client, g, {"draft": draft, "final": final}.get(name, audit))
        want = {(li["line_id"], line_label(li)) for li in state["line_items"]}
        got = {(b.get("data-line-id"), b.get("data-label")) for b in _boxes(html, "tbody")
               if b.get("data-line-id")}
        assert got == want, name
        assert not any(b.has_attr("data-skip") or b.has_attr("data-item-status") for b in _boxes(html)), name
        assert "liLineConfirm" not in html and "skip_kinds" not in html, name
        assert all(b.has_attr("data-line-id") and b.has_attr("data-label") for b in _boxes(html)), name
    heads = BeautifulSoup(pages["audit"], "html.parser").select("table:has(#line-body) thead tr")[0].find_all("th")
    assert heads
    for tr in BeautifulSoup(pages["audit swap"], "html.parser").select("tr"):
        assert len(tr.find_all("td")) == len(heads)


def line_label(li: dict) -> str:
    from celerp.services.line_measures import line_label as _label
    return _label(li)


async def test_a_finalized_edit_that_changes_a_line_id_is_refused(client, session):
    """Holds and shipments are keyed by line id, so a finalized document's line ids are as fixed
    as its quantities: a PATCH that rewrites one is refused and leaves the document as it was."""
    g = await _gems(client)
    d = await _doc(client, g, [_line(g, "DEMO-SAP-001"), GIA])
    before = await _state(client, g, d)
    moved = [dict(li) for li in before["line_items"]]
    moved[1]["line_id"] = str(uuid.uuid4())
    r = await client.patch(f"/docs/{d}", headers=g.h, json={
        "fields_changed": {"line_items": {"new": moved}}, "expected_version": before["version"]})
    assert r.status_code == 409, r.text
    assert (_detail(r)["message_key"], _detail(r)["params"]) == (
        "documents.err_line_field_finalized", {"field": "line_id", "line": 2})
    assert (await _state(client, g, d))["line_items"] == before["line_items"]
