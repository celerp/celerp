# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A production action sent from the app is recorded once, however often it is sent.

The page mints a key for each action before sending it, and sends the same key again when
the same action is sent again: after a lost answer, a double click, or a retry. The server
then records the action once and answers the retry with the original result. A new action
gets a new key. These tests drive the app's own client and pages against the real server.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import ui.api_client as api
from mfg_runs import give_back, issue, product, receive, reopen, run
from test_cost_restatement import _item, _state
from test_mfg_creation_contract import _count
from test_mfg_make_selected import _setup
from test_mfg_wip_upgrade import _events
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio


class _Server(httpx.AsyncBaseTransport):
    """The real server; the answer to the first POST to ``lose`` is lost on the way back."""

    def __init__(self, lose: str | None):
        from celerp.main import app
        self.inner, self.lose = ASGITransport(app=app), lose

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.method == "POST" and request.url.path == self.lose:
            self.lose = None
            await response.aread()
            raise httpx.ReadError("connection dropped", request=request)
        return response


@contextmanager
def _app(lose: str | None = None):
    server = _Server(lose)
    with patch("ui.api_client._get_transport", new=lambda: server):
        yield


def _token(auth) -> str:
    return auth["headers"]["Authorization"].split()[1]


async def _page(auth, path: str, data: dict | list):
    from urllib.parse import urlencode

    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        return await c.post(path, content=urlencode(data).encode(), cookies={"celerp_token": _token(auth)},
                            headers={"content-type": "application/x-www-form-urlencoded"})


async def _run(client, auth, qty: float = 4) -> tuple[str, str, str]:
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 1)])
    return raw, made, await run(client, auth, made, qty)


# ── Build, partial Issue and partial Receive: the answer is lost, the action sent again ──

@pytest.mark.parametrize("complete", [False, True], ids=["build", "build-and-complete"])
async def test_a_build_sent_again_after_its_answer_was_lost_makes_one_run(client, session, auth, complete):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 1)])

    with _app(lose=f"/manufacturing/items/{made}/build"):
        with pytest.raises(APIError):
            await api.build_item(_token(auth), made, 2, complete, idempotency_key="build-1")
        again = await api.build_item(_token(auth), made, 2, complete, idempotency_key="build-1")

    assert await _count(session, auth, entity_type="mfg_order") == 1
    assert (await _state(session, auth, again["id"]))["status"] == ("completed" if complete else "planned")
    assert (await _state(session, auth, raw))["quantity"] == (8.0 if complete else 10.0)
    with _app():
        await api.build_item(_token(auth), made, 2, complete, idempotency_key="build-2")
    assert await _count(session, auth, entity_type="mfg_order") == 2


async def test_a_partial_issue_sent_again_after_its_answer_was_lost_issues_once(client, session, auth):
    raw, _, order = await _run(client, auth)

    with _app(lose=f"/manufacturing/{order}/issue"):
        with pytest.raises(APIError):
            await api.issue_mfg_order(_token(auth), order, [{"item_id": raw, "quantity": 1}], idempotency_key="iss-1")
        await api.issue_mfg_order(_token(auth), order, [{"item_id": raw, "quantity": 1}], idempotency_key="iss-1")

    assert (await _state(session, auth, order))["inputs"][0]["issued_qty"] == 1.0
    assert (await _state(session, auth, raw))["quantity"] == 9.0
    with _app():
        await api.issue_mfg_order(_token(auth), order, [{"item_id": raw, "quantity": 1}], idempotency_key="iss-2")
    assert (await _state(session, auth, order))["inputs"][0]["issued_qty"] == 2.0


async def test_a_partial_receive_sent_again_after_its_answer_was_lost_receives_one_lot(client, session, auth):
    _, made, order = await _run(client, auth)
    assert (await issue(client, auth, order, key="all")).status_code == 200

    with _app(lose=f"/manufacturing/{order}/receive"):
        with pytest.raises(APIError):
            await api.receive_mfg_order(_token(auth), order, 1, idempotency_key="rcv-1")
        again = await api.receive_mfg_order(_token(auth), order, 1, idempotency_key="rcv-1")

    receipts = (await _state(session, auth, order))["receipts"]
    assert [r["quantity"] for r in receipts] == [1.0]
    assert again["lot_item_id"] == receipts[0]["lot_item_id"]
    with _app():
        await api.receive_mfg_order(_token(auth), order, 1, idempotency_key="rcv-2")
    assert [r["quantity"] for r in (await _state(session, auth, order))["receipts"]] == [1.0, 1.0]


# ── The app's client never sends a production action without its key ──

_CALLS = {
    "build_item": ("item:p", 1.0), "start_mfg_order": ("mfg:1",), "issue_mfg_order": ("mfg:1",),
    "receive_mfg_order": ("mfg:1",), "complete_mfg_order": ("mfg:1",), "cancel_mfg_order": ("mfg:1",),
    "return_mfg_materials": ("mfg:1",), "undo_mfg_receipt": ("mfg:1", "item:lot"),
    "reopen_mfg_order": ("mfg:1",), "hold_mfg_order": ("mfg:1",), "resume_mfg_order": ("mfg:1",),
    "schedule_mfg_order": ("mfg:1", {"priority": "high"}), "reconcile_mfg_order": ("mfg:1", {"components": []}),
    "manufacturing_make_work_orders": ([{"item_id": "item:p", "doc_id": ""}],),
    "manufacturing_bulk_run_action": (["mfg:1"], "hold"),
}


@pytest.mark.parametrize("name", sorted(_CALLS))
async def test_every_production_action_sends_the_key_it_was_given_and_needs_one(name):
    sent = []

    def answer(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    fn = getattr(api, name)
    with patch("ui.api_client._get_transport", new=lambda: httpx.MockTransport(answer)):
        await fn("tok", *_CALLS[name], idempotency_key="k")
        await fn("tok", *_CALLS[name], idempotency_key="k")
        with pytest.raises(TypeError):
            await fn("tok", *_CALLS[name])

    assert [body["idempotency_key"] for body in sent] == ["k", "k"]


# ── The pages: each rendered action carries its own key ──

async def test_each_render_brings_new_keys_and_each_run_its_own(client, session, auth):
    _, made, _ = await _run(client, auth)
    await run(client, auth, made, 1)
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        with _app():
            pages = [(await c.get(f"/api/items/{made}/production-block", cookies={"celerp_token": _token(auth)})).text
                     for _ in range(2)]
            queues = [(await c.get("/manufacturing/production", cookies={"celerp_token": _token(auth)})).text
                      for _ in range(2)]

    first, second = (re.findall(r'idempotency_key(?:&quot;|")[^0-9a-f]+([0-9a-f-]{36})', p) for p in pages)
    assert len(first) == 2 and len(set(first)) == 2 and not set(first) & set(second)
    tables = [re.findall(r'data-operation-key="([0-9a-f-]{36})"', q) for q in queues]
    assert len(tables[0]) == 1 and tables[0] != tables[1]


async def _settled(session, auth) -> int:
    session.expire_all()
    return await _events(session, auth)


async def _act(auth, made: str, order: str, action: str, key: str):
    r = await _page(auth, f"/api/items/{made}/runs/{order}/act", {"action": action, "idempotency_key": key})
    assert r.status_code == 200 and "flash--error" not in r.text, r.text
    return r


async def _issued(client, auth) -> tuple[str, str]:
    _, made, order = await _run(client, auth)
    assert (await issue(client, auth, order, key="all")).status_code == 200
    return made, order


async def _received(client, auth) -> tuple[str, str]:
    made, order = await _issued(client, auth)
    assert (await receive(client, auth, order, 1, key="one")).status_code == 200
    return made, order


async def _completed(client, auth) -> tuple[str, str]:
    made, order = await _received(client, auth)
    with _app():
        await _act(auth, made, order, "complete", "close")
    return made, order


async def _planned(client, auth) -> tuple[str, str]:
    _, made, order = await _run(client, auth)
    return made, order


@pytest.mark.parametrize("action, setup", [
    ("start", _planned), ("hold", _issued), ("complete", _received), ("return", _issued),
    ("undo", _received), ("cancel", _planned), ("reopen", _completed),
])
async def test_a_run_action_sent_again_from_the_same_page_is_recorded_once(client, session, auth, action, setup):
    made, order = await setup(client, auth)
    if action == "undo":
        action = f"undo:{(await _state(session, auth, order))['receipts'][0]['lot_item_id']}"
    with _app():
        await _act(auth, made, order, action, "page-1")
        if action == "return":  # the run holds materials again, so a second return could take them
            assert (await issue(client, auth, order, key="again")).status_code == 200
        events = await _settled(session, auth)

        await _act(auth, made, order, action, "page-1")

    assert await _settled(session, auth) == events


async def test_a_different_choice_from_the_same_page_is_a_new_action(client, session, auth):
    made, order = await _issued(client, auth)
    with _app():
        await _act(auth, made, order, "hold", "page-1")
        await _act(auth, made, order, "resume", "page-1")

    assert (await _state(session, auth, order))["status"] == "in_progress"


async def _bulk(auth, action: str, orders: list[str], key: str) -> dict:
    r = await _page(auth, f"/manufacturing/runs/bulk/{action}?status=active",
                    [*(("selected", o) for o in orders), ("idempotency_key", key)])
    assert r.status_code == 200, r.text
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


@pytest.mark.parametrize("action", ["issue", "complete"])
async def test_a_bulk_action_sent_again_from_the_same_page_is_recorded_once(client, session, auth, action):
    orders = []
    for _ in range(2):
        _, _, order = await _run(client, auth, qty=2)
        if action == "complete":
            assert (await issue(client, auth, order, key="all")).status_code == 200
            assert (await receive(client, auth, order, 1, key="one")).status_code == 200
        orders.append(order)
    with _app():
        assert (await _bulk(auth, action, orders, "page-1"))["type"] == "success"
        for o in orders:  # undone meanwhile, so the action sent again could be taken again
            undo = give_back if action == "issue" else reopen
            r = await undo(client, auth, o, key="undone")
            assert r.status_code == 200, r.text
        events = await _settled(session, auth)

        await _bulk(auth, action, orders, "page-1")

    assert await _settled(session, auth) == events
    if action == "issue":
        assert [(await _state(session, auth, o))["inputs"][0]["issued_qty"] for o in orders] == [0.0, 0.0]
    else:
        assert [(await _state(session, auth, o))["status"] for o in orders] == ["in_progress", "in_progress"]


@pytest.mark.parametrize("complete", [False, True], ids=["make", "make-and-complete"])
async def test_make_selected_sent_again_from_the_same_page_makes_once(client, session, auth, complete):
    """The run made for the later order is pegged to the earlier one, so the later order still
    shows short when the same action is sent again."""
    made, _, late = await _setup(client, auth)
    path = "/manufacturing/make-selected" + ("?complete=1" if complete else "")
    with _app():
        for _ in range(2):
            r = await _page(auth, path, {"selected": f"{made}|{late}", "idempotency_key": "page-1"})
            assert r.status_code == 200, r.text

    assert await _count(session, auth, entity_type="mfg_order") == 1
    assert await _count(session, auth, event_type="mfg.order.completed") == (1 if complete else 0)


async def test_a_page_rendered_without_a_key_is_refused_as_out_of_date(client, session, auth):
    made, order = await _planned(client, auth)
    events = await _settled(session, auth)
    with _app():
        r = await _page(auth, f"/api/items/{made}/runs/{order}/act", {"action": "start"})
        toast = await _bulk(auth, "start", [order], "")

    assert "This page is out of date. Reload it and try again." in r.text and "flash--error" in r.text
    assert toast == {"message": "This page is out of date. Reload it and try again.", "type": "error"}
    assert await _settled(session, auth) == events


async def test_a_refused_schedule_edit_says_why(client, session, auth):
    made, order = await _planned(client, auth)
    with _app():
        await _act(auth, made, order, "cancel", "page-1")
        r = await _page(auth, f"/manufacturing/runs/{order}/schedule", {"priority": "high", "idempotency_key": "e1"})

    assert r.status_code == 200, r.text
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast == {"message": "This run is cancelled, so it cannot be rescheduled.", "type": "error"}


# ── The server's answer is lost on the way to the app: the page it shows keeps the key ──

def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


def _run_key(html: str, order: str) -> str:
    select = re.search(rf'<select[^>]*runs/{re.escape(order)}/act[^>]*>', html).group(0)
    return re.search(r'idempotency_key(?:&quot;|")[^\w-]+([\w-]+)', select).group(1)


def _table_key(html: str) -> str:
    return re.search(r'data-operation-key="([^"]+)"', html).group(1)


async def test_a_run_action_whose_answer_was_lost_is_sent_again_with_its_key(client, session, auth):
    made, order = await _issued(client, auth)
    with _app(lose=f"/manufacturing/{order}/return"):
        lost = await _page(auth, f"/api/items/{made}/runs/{order}/act", {"action": "return", "idempotency_key": "p1"})
        assert "flash--error" in lost.text
        assert (await issue(client, auth, order, key="again")).status_code == 200
        events = await _settled(session, auth)

        await _act(auth, made, order, "return", _run_key(lost.text, order))

    assert _run_key(lost.text, order) == "p1"
    assert await _settled(session, auth) == events


async def test_a_bulk_action_whose_answer_was_lost_is_sent_again_with_its_key(client, session, auth):
    _, _, order = await _run(client, auth, qty=2)
    with _app(lose="/manufacturing/bulk-action"):
        lost = await _page(auth, "/manufacturing/runs/bulk/issue?status=active",
                           [("selected", order), ("idempotency_key", "p1")])
        assert _toast(lost)["type"] == "error"
        assert (await give_back(client, auth, order, key="undone")).status_code == 200
        events = await _settled(session, auth)

        await _bulk(auth, "issue", [order], _table_key(lost.text))

    assert _table_key(lost.text) == "p1"
    assert await _settled(session, auth) == events
    assert (await _state(session, auth, order))["inputs"][0]["issued_qty"] == 0.0


async def test_make_selected_whose_answer_was_lost_is_sent_again_with_its_key(client, session, auth):
    made, _, late = await _setup(client, auth)
    with _app(lose="/manufacturing/to-make/make"):
        lost = await _page(auth, "/manufacturing/make-selected", {"selected": f"{made}|{late}", "idempotency_key": "p1"})
        assert _toast(lost)["type"] == "error"

        r = await _page(auth, "/manufacturing/make-selected",
                        {"selected": f"{made}|{late}", "idempotency_key": _table_key(lost.text)})

    assert _table_key(lost.text) == "p1" and _toast(r)["type"] == "success"
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_kept_key_sent_with_another_selection_is_refused_and_replaced(client, session, auth):
    """The answer is lost and the queue keeps the action's key; the person then ticks another
    run as well. That is a different action: it is refused with a plain reason, and the queue
    it answers with brings a new key, so the next try goes through."""
    _, _, one = await _run(client, auth, qty=2)
    _, _, two = await _run(client, auth, qty=2)
    with _app(lose="/manufacturing/bulk-action"):
        lost = await _page(auth, "/manufacturing/runs/bulk/hold?status=active",
                           [("selected", one), ("idempotency_key", "p1")])
        assert _table_key(lost.text) == "p1"

        refused = await _page(auth, "/manufacturing/runs/bulk/hold?status=active",
                              [("selected", one), ("selected", two), ("idempotency_key", "p1")])

        assert _toast(refused)["type"] == "error"
        assert "key" not in _toast(refused)["message"].lower()
        assert _table_key(refused.text) != "p1"
        assert (await _state(session, auth, two))["status"] == "planned"
        await _bulk(auth, "hold", [one, two], _table_key(refused.text))
    session.expire_all()
    assert (await _state(session, auth, two))["status"] == "on_hold"


async def test_make_selected_kept_key_with_another_selection_is_refused_and_replaced(client, session, auth):
    made, early, late = await _setup(client, auth)
    with _app(lose="/manufacturing/to-make/make"):
        lost = await _page(auth, "/manufacturing/make-selected", {"selected": f"{made}|{late}", "idempotency_key": "p1"})
        assert _table_key(lost.text) == "p1"

        refused = await _page(auth, "/manufacturing/make-selected",
                              [("selected", f"{made}|{late}"), ("selected", f"{made}|{early}"),
                               ("idempotency_key", "p1")])

    assert _toast(refused)["type"] == "error" and _table_key(refused.text) != "p1"
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_bulk_cancel_of_a_run_holding_materials_says_why_and_books_nothing(client, session, auth):
    """The run is skipped for the reason the run gives, not counted as 'not in a valid state',
    in the user's language, and nothing is written but the record of the action itself."""
    from urllib.parse import urlencode

    from sqlalchemy import func, select

    from celerp.models.ledger import LedgerEntry
    from ui.app import app as ui_app

    made, order = await _issued(client, auth)
    session.expire_all()
    kept = select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.event_type != "mfg.operation.recorded")
    before = await session.scalar(kept)
    with _app():
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            r = await c.post("/manufacturing/runs/bulk/cancel?status=active",
                             content=urlencode([("selected", order), ("idempotency_key", "page-1")]).encode(),
                             cookies={"celerp_token": _token(auth), "celerp_lang": "de"},
                             headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200, r.text
    message = json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]
    de = json.loads((__import__("pathlib").Path(__file__).resolve().parents[1] / "ui/locales/de.json").read_text())
    assert message == ". ".join([de["manufacturing.bulk_cancelled"].format(n=0),
                                 de["manufacturing.bulk_skipped"].format(n=1),
                                 de["mfg.cancel_moved"].rstrip(".")]) + "."
    assert "Status" not in de["manufacturing.bulk_skipped"]
    session.expire_all()
    assert (await _state(session, auth, order))["status"] == "in_progress"
    assert await session.scalar(kept) == before


async def test_a_bulk_action_naming_a_run_that_does_not_exist_skips_it_with_a_keyed_reason(client, auth):
    r = await client.post("/manufacturing/bulk-action", headers=auth["headers"],
                          json={"run_ids": ["mfg:gone"], "action": "hold", "idempotency_key": "k"})
    assert r.status_code == 200, r.text
    assert r.json() == {"done": [], "skipped": [{
        "id": "mfg:gone", "reason": "Production run mfg:gone was not found.",
        "message_key": "mfg.run_not_found", "params": {"order": "mfg:gone"}}]}
