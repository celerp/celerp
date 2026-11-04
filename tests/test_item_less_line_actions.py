# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Bulk line actions skip lines that have no stock to act on, and say which.

A line with no inventory item (a certificate, a free-text charge) has no status, so Ship,
Reserve, Set as available and Take back act on the stock lines chosen with it and name it
in a notice. A service line is counted fulfilled on Ship and skipped by the other stock
actions, in its own words. A choice of only such lines is refused with the same words.
Memo Invoice selected and Back to memo skip and name a line with no item the same way.

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
    answer = await _ok(await _act(client, g, d, "fulfill-lines", await _ids(client, g, d)))
    assert [await _status(client, g, s) for s in STONES] == ["sold"] * 5
    assert answer["skipped"] == [{"line": 6, "name": "GIA Certificate", "kind": "no_item"}]
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
    expected = [{"line": 2, "name": "GIA Certificate", "kind": "no_item"}]
    notice = [("documents.lines_skipped_one", {"lines": '2 "GIA Certificate"'})]
    if service_skipped:
        expected.append({"line": 3, "name": SVC, "kind": "service"})
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
    if many:
        assert (detail["message_key"], detail["params"]) == (
            "lines.no_stock_many", {"lines": '6 "GIA Certificate", 7 "Engraving"'})
    else:
        assert (detail["message_key"], detail["params"]) == ("lines.no_stock_one", {"lines": '6 "GIA Certificate"'})
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
    assert result["skipped"] == [{"line": 3, "name": "GIA Certificate", "kind": "no_item"}]
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
    assert answer["skipped"] == [{"line": 2, "name": SVC, "kind": "service"}]


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
    from ui.i18n import refusal_text
    g = await _gems(client)
    memo = await _doc(client, g, [_line(g, "DEMO-SAP-001"), _line(g, "DEMO-EMR-001"), GIA], doc_type="memo")
    lids = await _ids(client, g, memo)
    await _ship(client, g, memo, lids[:2])
    preview = await _ok(await draw(client, g.h, memo, line_ids=lids, dry_run=True))
    assert len(preview["lines"]) == 2
    notice = [("documents.lines_skipped_one", {"lines": '3 "GIA Certificate"'})]
    assert _notice(preview) == notice
    done = await _ok(await draw(client, g.h, memo, line_ids=lids, binding=preview["binding"]))
    assert _notice(done) == notice
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
    assert toast["message"] == refusal_text(done["skipped_notice"]) and toast.get("persist") is True


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


async def test_back_to_memo_skips_own_item_less_line_and_names_it(client, session):
    g = await _gems(client)
    memo, invoice, drawn, own = await _drawn_invoice_with_own_certificate(client, g)
    answer = await _ok(await back_to_memo(client, g.h, invoice, [drawn, own]))
    assert _notice(answer) == [("documents.lines_skipped_one", {"lines": '2 "GIA Certificate"'})]
    left = (await _state(client, g, invoice))["line_items"]
    assert [li.get("description") for li in left] == ["GIA Certificate"]
    assert await _status(client, g, "DEMO-SAP-001") == "memo_out"


async def test_back_to_memo_refuses_when_only_own_item_less_lines(client, session):
    g = await _gems(client)
    memo, invoice, drawn, own = await _drawn_invoice_with_own_certificate(client, g)
    before = await _state(client, g, invoice)
    r = await back_to_memo(client, g.h, invoice, [own])
    assert r.status_code == 409, r.text
    assert _detail(r)["message_key"] == "docs.back_to_memo_not_drawn"
    assert (await _state(client, g, invoice))["line_items"] == before["line_items"]


# ── Class guard: no bulk line action reads an all-skipped choice as "every line" ─

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


# route -> (owner kind, request); the request gets (client, g, owner, the item-less line ids).
_ALL_SKIPPED = {
    ("POST", "/docs/{entity_id}/fulfill-lines"): ("invoice", lambda c, g, o, l: _act(c, g, o, "fulfill-lines", l)),
    ("POST", "/docs/{entity_id}/reserve-lines"): ("invoice", lambda c, g, o, l: _act(
        c, g, o, "reserve-lines", l, new_status="reserved")),
    ("POST", "/docs/{entity_id}/set-available"): ("invoice", lambda c, g, o, l: _act(c, g, o, "set-available", l)),
    ("POST", "/docs/{entity_id}/revert-lines"): ("invoice", lambda c, g, o, l: _act(c, g, o, "revert-lines", l)),
    ("POST", "/lists/{entity_id}/reserve-lines"): ("list", lambda c, g, o, l: _act(
        c, g, o, "reserve-lines", l, new_status="reserved")),
    ("POST", "/lists/{entity_id}/set-available"): ("list", lambda c, g, o, l: _act(c, g, o, "set-available", l)),
    ("POST", "/docs/{entity_id}/invoice-lines"): ("memo", lambda c, g, o, l: draw(c, g.h, o, line_ids=l)),
    ("POST", "/docs/{entity_id}/back-to-memo"): ("drawn", lambda c, g, o, l: back_to_memo(c, g.h, o, l)),
    ("POST", "/lists/{entity_id}/set-scanned"): ("audit", lambda c, g, o, l: _scan(c, g, o, l, True)),
}


async def test_every_bulk_line_action_is_covered_by_the_all_skipped_guard(client):
    assert _line_selection_routes() == set(_ALL_SKIPPED)


@pytest.mark.parametrize("route", [r[1] for r in _ALL_SKIPPED], ids=lambda p: p.split("/", 2)[1] + p.rsplit("/", 1)[1])
async def test_a_choice_of_only_item_less_lines_changes_nothing_and_is_refused(client, session, route):
    kind, send = next(v for k, v in _ALL_SKIPPED.items() if k[1] == route)
    g = await _gems(client)
    lines = [*(_line(g, s) for s in STONES), GIA]
    if kind == "audit":
        owner = await _audit_with_certificate(client, g)
        await _ok(await _scan(client, g, owner, (await _ids(client, g, owner))[:1], False))
    elif kind == "drawn":
        owner = (await _drawn_invoice_with_own_certificate(client, g))[1]
    elif kind == "list":
        owner = await _list(client, g, lines)
    else:
        owner = await _doc(client, g, lines, doc_type="memo" if kind == "memo" else "invoice")
        if kind == "memo":
            await _ship(client, g, owner, (await _ids(client, g, owner))[:5])
    before = await _state(client, g, owner)
    item_less = [li["line_id"] for li in before["line_items"] if not (li.get("item_id") or li.get("entity_id"))]
    assert item_less
    r = await send(client, g, owner, item_less)
    assert r.status_code in (409, 422), r.text
    assert _detail(r)["message_key"] in ("lines.no_stock_one", "docs.back_to_memo_not_drawn")
    after = await _state(client, g, owner)
    assert after["version"] == before["version"] and after["line_items"] == before["line_items"]
