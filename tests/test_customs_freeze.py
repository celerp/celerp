# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An issued shipping document keeps the customs codes it was issued with: Issue, retype,
import and a one-time startup pass copy them from the item, and later item edits never reach
the printed or shared document."""
from __future__ import annotations

import copy
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, text

from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

pytestmark = pytest.mark.asyncio


async def _item(client, auth, hs_code: str = "1234") -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"CF-{uuid.uuid4().hex[:6]}", "name": "Teak bowl", "quantity": 5.0, "sell_by": "piece",
        "category": "Raw", "status": "available", "hs_code": hs_code, "country_of_origin": "TH"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _set_item_code(client, auth, item_id: str, old: str, new: str) -> None:
    r = await client.patch(f"/items/{item_id}", headers=auth["headers"],
                           json={"fields_changed": {"hs_code": {"old": old, "new": new}}})
    assert r.status_code == 200, r.text


def _line(item_id: str) -> dict:
    return {"name": "Teak bowl", "quantity": 1, "unit_price": 40.0, "line_total": 40.0, "item_id": item_id}


async def _list(client, auth, list_type: str, item_id: str) -> str:
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": list_type, "contact_name": "Buyer", "line_items": [_line(item_id)]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _issue(client, auth, entity_id: str) -> None:
    r = await client.post(f"/lists/{entity_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text


async def _lines(client, auth, entity_id: str) -> list[dict]:
    r = await client.get(f"/lists/{entity_id}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["line_items"]


async def _shared_lines(session, auth, entity_id: str) -> list[dict]:
    from celerp_docs.routes_share import _enrich_share_lines

    session.expire_all()
    row = await session.get(Projection, (auth["company_id"], entity_id))
    state = {"doc_type": "list", **copy.deepcopy(row.state)}
    await _enrich_share_lines(session, auth["company_id"], state)
    return state["line_items"]


async def test_issue_freezes_customs_codes_from_the_item(client, auth):
    item_id = await _item(client, auth)
    ship = await _list(client, auth, "shipping_doc", item_id)
    assert not (await _lines(client, auth, ship))[0].get("hs_code")

    await _issue(client, auth, ship)
    line = (await _lines(client, auth, ship))[0]
    assert line["hs_code"] == "1234"
    assert line["country_of_origin"] == "TH"


async def test_print_of_issued_shipping_doc_does_not_take_later_item_edits(client, auth, session):
    item_id = await _item(client, auth)
    ship = await _list(client, auth, "shipping_doc", item_id)
    await _issue(client, auth, ship)
    await _set_item_code(client, auth, item_id, "1234", "9999")

    assert (await _lines(client, auth, ship))[0]["hs_code"] == "1234"
    assert (await _shared_lines(session, auth, ship))[0]["hs_code"] == "1234"


@pytest.mark.parametrize("status, shown", [("finalized", False), ("draft", True)])
async def test_print_view_of_issued_shipping_doc_does_not_take_later_item_edits(ui_client, status, shown):
    stored = {"entity_id": "list:SHIP-1", "ref_id": "SHIP-1", "list_type": "shipping_doc", "status": status,
              "line_items": [{"name": "Teak bowl", "quantity": 1, "item_id": "item:bowl"}]}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_list", new=AsyncMock(return_value=stored)), \
         patch("ui.api_client.get_item", new=AsyncMock(return_value={"entity_id": "item:bowl", "hs_code": "9999"})):
        r = await ui_client.get("/lists/list:SHIP-1/print?layout=commercial_invoice")
    assert r.status_code == 200, r.text
    assert ("9999" in r.text) is shown


async def test_retype_to_customs_type_freezes(client, auth):
    item_id = await _item(client, auth)
    quote = await _list(client, auth, "quotation", item_id)
    await _issue(client, auth, quote)

    r = await client.post(f"/lists/{quote}/change-type", headers=auth["headers"], json={"list_type": "shipping_doc"})
    assert r.status_code == 200, r.text
    assert (await _lines(client, auth, quote))[0]["hs_code"] == "1234"


def _import_record(n: str, item_id: str, status: str = "finalized") -> dict:
    return {"entity_id": f"list:CFIMP-{n}", "event_type": "list.created", "source": "csv",
            "idempotency_key": f"cf-imp-{n}-{uuid.uuid4().hex[:6]}",
            "data": {"ref_id": f"CFIMP-{n}", "list_type": "shipping_doc", "status": status,
                     "contact_name": "Buyer", "line_items": [_line(item_id)]}}


async def test_imported_issued_shipping_list_is_frozen(client, auth):
    item_id = await _item(client, auth)
    issued, draft = _import_record("1", item_id), _import_record("2", item_id, status="draft")
    for record in (issued, draft):
        r = await client.post("/lists/import", headers=auth["headers"], json=record)
        assert r.status_code == 200, r.text

    assert (await _lines(client, auth, issued["entity_id"]))[0]["hs_code"] == "1234"
    assert not (await _lines(client, auth, draft["entity_id"]))[0].get("hs_code")


async def test_batch_import_of_issued_shipping_lists_is_frozen(client, auth):
    item_id = await _item(client, auth)
    records = [_import_record("3", item_id), _import_record("4", item_id)]
    r = await client.post("/lists/import/batch", headers=auth["headers"], json={"records": records})
    assert r.status_code == 200, r.text
    for record in records:
        assert (await _lines(client, auth, record["entity_id"]))[0]["hs_code"] == "1234"


# The startup pass: shipping documents issued by an earlier release hold no codes of their own.

async def _reset_marker(session) -> None:
    from celerp.migrations._data_reconcile import get_meta
    from celerp_docs.customs_freeze import CUSTOMS_FREEZE_KEY

    conn = await session.connection()
    await conn.run_sync(lambda c: get_meta(c, CUSTOMS_FREEZE_KEY))  # creates the table on a fresh database
    await session.execute(text("DELETE FROM instance_meta WHERE key = :k"), {"k": CUSTOMS_FREEZE_KEY})
    await session.commit()


async def _marker(session) -> str | None:
    from celerp.migrations._data_reconcile import get_meta
    from celerp_docs.customs_freeze import CUSTOMS_FREEZE_KEY

    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, CUSTOMS_FREEZE_KEY))


async def _issued_by_an_earlier_release(client, auth, session, item_id: str) -> str:
    """An issued shipping document as an earlier release stored it: its lines carry no codes."""
    ship = await _list(client, auth, "shipping_doc", item_id)
    await _issue(client, auth, ship)
    session.expire_all()
    row = await session.get(Projection, (auth["company_id"], ship))
    lines = [{k: v for k, v in li.items() if k not in ("hs_code", "country_of_origin")}
             for li in row.state["line_items"]]
    row.state = {**row.state, "line_items": lines}
    await session.commit()
    return ship


async def _printed_by_an_earlier_release(session, auth, entity_id: str, item_id: str) -> dict:
    """The first line as an earlier release printed it: every code read from the item."""
    from celerp.services.shipping import customs_backfill

    session.expire_all()
    line = dict((await session.get(Projection, (auth["company_id"], entity_id))).state["line_items"][0])
    customs_backfill(line, (await session.get(Projection, (auth["company_id"], item_id))).state)
    return line


async def _start(session) -> None:
    from celerp_docs.customs_freeze import customs_freeze_hook

    await customs_freeze_hook(session=session)
    await session.commit()


async def _updates(session, company_id) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == "list.updated"))).scalar_one()


async def _freeze_notices(session, company_id) -> list[Notification]:
    rows = (await session.execute(
        select(Notification).where(Notification.company_id == company_id))).scalars().all()
    return [n for n in rows if (n.i18n or {}).get("title") == "notice.customs_freeze.title"]


async def test_startup_freeze_tells_each_affected_company_once(client, auth, session):
    from test_helpers import company_auth

    await _reset_marker(session)
    item_id = await _item(client, auth)
    for _ in range(2):
        await _issued_by_an_earlier_release(client, auth, session, item_id)
    other = await company_auth(session, uuid.uuid4(), uuid.uuid4())

    await _start(session)
    notices = await _freeze_notices(session, auth["company_id"])
    assert len(notices) == 1
    assert notices[0].i18n["params"]["count"] == 2
    assert notices[0].action_url == "/lists"
    assert await _freeze_notices(session, other["company_id"]) == []

    await _reset_marker(session)
    await _start(session)
    assert len(await _freeze_notices(session, auth["company_id"])) == 1


async def test_startup_freeze_keeps_printed_codes_and_is_idempotent(client, auth, session):
    await _reset_marker(session)
    item_id = await _item(client, auth)
    ship = await _issued_by_an_earlier_release(client, auth, session, item_id)
    printed = (await _printed_by_an_earlier_release(session, auth, ship, item_id))["hs_code"]
    assert printed == "1234"

    await _start(session)
    assert (await _lines(client, auth, ship))[0]["hs_code"] == printed
    assert await _marker(session)
    emitted = await _updates(session, auth["company_id"])

    await _reset_marker(session)
    await _start(session)
    assert await _updates(session, auth["company_id"]) == emitted


async def test_startup_freeze_failed_list_rolls_back_alone_and_leaves_marker_unset(client, auth, session):
    import celerp_docs.customs_freeze as customs_freeze

    await _reset_marker(session)
    item_id = await _item(client, auth)
    failing = await _issued_by_an_earlier_release(client, auth, session, item_id)
    frozen = await _issued_by_an_earlier_release(client, auth, session, item_id)
    real_emit = customs_freeze.emit_event

    async def _emit(sess, **kwargs):
        if kwargs.get("entity_id") == failing:
            raise RuntimeError("ledger write failed")
        return await real_emit(sess, **kwargs)

    with patch.object(customs_freeze, "emit_event", new=_emit):
        await _start(session)

    assert not (await _lines(client, auth, failing))[0].get("hs_code")
    assert (await _lines(client, auth, frozen))[0]["hs_code"] == "1234"
    assert await _marker(session) is None
