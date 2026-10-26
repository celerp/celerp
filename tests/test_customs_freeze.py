# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An issued shipping document keeps the customs codes it was issued with: Issue, retype,
import and a one-time startup pass copy them from the item, and later item edits never reach
the printed or shared document."""
from __future__ import annotations

import copy
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, text

from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection

from test_ui import (_authed, _list_page_stub, _mock_category_schemas, _mock_column_prefs,  # noqa: F401  (fixtures)
                     _mock_get_company, _mock_get_locations, _mock_get_modules, ui_client)

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
    r = await client.get(f"/lists/{ship}", headers=auth["headers"])
    assert r.json()["customs_saved"] is True


async def test_print_of_issued_shipping_doc_does_not_take_later_item_edits(client, auth, session):
    item_id = await _item(client, auth)
    ship = await _list(client, auth, "shipping_doc", item_id)
    await _issue(client, auth, ship)
    await _set_item_code(client, auth, item_id, "1234", "9999")

    assert (await _lines(client, auth, ship))[0]["hs_code"] == "1234"
    assert (await _shared_lines(session, auth, ship))[0]["hs_code"] == "1234"


@pytest.mark.parametrize("status, saved, shown", [
    ("finalized", True, False),   # issued with its codes saved: the item's later edit never shows
    ("finalized", False, True),   # issued, codes not saved yet: the item's codes show, never a blank
    ("draft", False, True),
])
async def test_print_view_of_issued_shipping_doc_does_not_take_later_item_edits(ui_client, status, saved, shown):
    stored = {"entity_id": "list:SHIP-1", "ref_id": "SHIP-1", "list_type": "shipping_doc", "status": status,
              **({"customs_saved": True} if saved else {}),
              "line_items": [{"name": "Teak bowl", "quantity": 1, "item_id": "item:bowl"}]}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_list", new=AsyncMock(return_value=stored)), \
         patch("ui.api_client.get_item", new=AsyncMock(return_value={"entity_id": "item:bowl", "hs_code": "9999"})):
        r = await ui_client.get("/lists/list:SHIP-1/print?layout=commercial_invoice")
    assert r.status_code == 200, r.text
    assert ("9999" in r.text) is shown


@pytest.mark.parametrize("saved, shown", [(True, False), (False, True)])
@pytest.mark.usefixtures("_mock_get_company", "_mock_category_schemas", "_mock_column_prefs", "_mock_get_locations",
                         "_mock_get_modules")
async def test_detail_page_of_a_shipping_doc_shows_the_codes_its_printout_shows(ui_client, saved, shown):
    """The page of an issued shipping list reads a line's customs codes by the rule its printout
    and share view do (backfill_item_customs): from the item until they are saved, never blank."""
    stored = {"entity_id": "list:SHIP-1", "ref_id": "SHIP-1", "list_type": "shipping_doc", "status": "finalized",
              **({"customs_saved": True} if saved else {}),
              "line_items": [{"name": "Teak bowl", "quantity": 1, "item_id": "item:bowl"}]}
    stub = _list_page_stub(stored)
    page = stub.side_effect

    async def _page(*args, **kwargs):
        return {**await page(*args, **kwargs),
                "item_meta": {"item:bowl": {"id": "item:bowl", "hs_code": "4419.90", "country_of_origin": "TH"}}}

    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_list_page", create=True, new=AsyncMock(side_effect=_page)):
        r = await ui_client.get("/lists/list:SHIP-1")
    assert r.status_code == 200, r.text
    assert ("4419.90" in r.text) is shown


async def test_every_reader_of_a_shipping_docs_lines_fills_the_items_codes_by_one_rule():
    """Class guard: outside the rule itself and what Issue saves, nothing calls customs_backfill,
    so no page, printout or share view of a shipping document decides it another way."""
    root = Path(__file__).resolve().parent.parent
    allowed = {root / "celerp" / "services" / "shipping.py",
               root / "default_modules" / "celerp-docs" / "celerp_docs" / "routes.py"}
    callers = {path for top in ("celerp", "ui", "default_modules") for path in (root / top).rglob("*.py")
               if "customs_backfill(" in path.read_text()}
    assert callers <= allowed, sorted(str(p.relative_to(root)) for p in callers - allowed)


async def test_retype_to_customs_type_freezes(client, auth):
    item_id = await _item(client, auth)
    quote = await _list(client, auth, "quotation", item_id)
    await _issue(client, auth, quote)

    r = await client.post(f"/lists/{quote}/change-type", headers=auth["headers"], json={"list_type": "shipping_doc"})
    assert r.status_code == 200, r.text
    assert (await _lines(client, auth, quote))[0]["hs_code"] == "1234"


def _import_record(n: str, item_id: str, status: str = "finalized", list_type: str = "shipping_doc") -> dict:
    return {"entity_id": f"list:CFIMP-{n}", "event_type": "list.created", "source": "csv",
            "idempotency_key": f"cf-imp-{n}-{uuid.uuid4().hex[:6]}",
            "data": {"ref_id": f"CFIMP-{n}", "list_type": list_type, "status": status,
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
    """An issued shipping document as an earlier release stored it."""
    ship = await _list(client, auth, "shipping_doc", item_id)
    await _issue(client, auth, ship)
    await _as_an_earlier_release_stored_it(session, auth, ship)
    return ship


async def _as_an_earlier_release_stored_it(session, auth, ship: str) -> None:
    """Strip what Issue saves today: an earlier release saved no codes and no item on the lines."""
    session.expire_all()
    row = await session.get(Projection, (auth["company_id"], ship))
    lines = [{k: v for k, v in li.items() if k not in ("hs_code", "country_of_origin", "item_at_issue")}
             for li in row.state["line_items"]]
    row.state = {**{k: v for k, v in row.state.items() if k != "customs_saved"}, "line_items": lines}
    await session.commit()


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


_FREEZE_BODY = (
    "For shipping documents issued before this update, Celerp has saved the item details currently "
    "shown on those documents. Future item changes will no longer change their issued copies. These "
    "details may differ from what the items showed when the documents were originally issued.")


async def test_freeze_notice_already_shown_reads_the_corrected_text():
    """A notice stored by an earlier start (with the item count it carried) shows the corrected text:
    the details saved are what the documents print now, not what they showed at Issue."""
    import json

    from ui.routes.notifications import _in_reader_language

    stored = {"items": [{"title": "t", "body": "old", "i18n": {
        "title": "notice.customs_freeze.title", "body": "notice.customs_freeze.body",
        "params": {"count": 3}}}]}
    shown = json.loads(_in_reader_language(json.dumps(stored).encode()))["items"][0]
    assert shown["body"] == _FREEZE_BODY
    assert "at Issue" not in shown["body"]


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
    assert notices[0].body == _FREEZE_BODY
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


async def test_an_issued_list_whose_codes_failed_to_save_prints_the_items_codes_until_saved(client, auth, session):
    """A list the startup pass could not save never prints blank: share and print read the item's
    codes until a later start saves them, and from then on the saved codes stand."""
    import celerp_docs.customs_freeze as customs_freeze

    await _reset_marker(session)
    item_id = await _item(client, auth)
    failing = await _issued_by_an_earlier_release(client, auth, session, item_id)
    real_emit = customs_freeze.emit_event

    async def _emit(sess, **kwargs):
        if kwargs.get("entity_id") == failing:
            raise RuntimeError("ledger write failed")
        return await real_emit(sess, **kwargs)

    with patch.object(customs_freeze, "emit_event", new=_emit):
        await _start(session)
    assert not (await _lines(client, auth, failing))[0].get("hs_code")
    assert (await _shared_lines(session, auth, failing))[0]["hs_code"] == "1234"

    await _start(session)
    assert await _marker(session)
    assert (await _lines(client, auth, failing))[0]["hs_code"] == "1234"
    await _set_item_code(client, auth, item_id, "1234", "9999")
    assert (await _shared_lines(session, auth, failing))[0]["hs_code"] == "1234"


async def test_startup_saves_the_flag_on_an_issued_list_whose_lines_already_hold_codes(client, auth, session):
    """An issued list whose lines carry their own codes is marked saved too, so a later item edit
    never fills a code the line left empty."""
    await _reset_marker(session)
    item_id = await _item(client, auth)
    ship = await _issued_by_an_earlier_release(client, auth, session, item_id)
    row = await session.get(Projection, (auth["company_id"], ship))
    row.state = {**row.state, "line_items": [{**li, "hs_code": "5555", "country_of_origin": "VN"}
                                             for li in row.state["line_items"]]}
    await session.commit()

    await _start(session)
    r = await client.get(f"/lists/{ship}", headers=auth["headers"])
    assert r.json()["customs_saved"] is True
    assert r.json()["line_items"][0]["hs_code"] == "5555"


@pytest.mark.parametrize("path", ["issue", "import", "batch_import", "retype", "startup"])
async def test_a_list_that_is_not_a_shipping_document_keeps_its_lines_as_written(client, auth, session, path):
    """Customs codes are filled only on shipping documents: any other list issued, imported, retyped
    or passed at startup keeps its lines without the item's codes and is never marked saved."""
    item_id = await _item(client, auth)
    if path in ("import", "batch_import"):
        record = _import_record(f"Q{uuid.uuid4().hex[:6]}", item_id, list_type="quotation")
        r = await (client.post("/lists/import", headers=auth["headers"], json=record) if path == "import" else
                   client.post("/lists/import/batch", headers=auth["headers"], json={"records": [record]}))
        assert r.status_code == 200, r.text
        quote = record["entity_id"]
    else:
        quote = await _list(client, auth, "quotation", item_id)
        await _issue(client, auth, quote)
    if path == "retype":
        r = await client.post(f"/lists/{quote}/change-type", headers=auth["headers"], json={"list_type": "transfer"})
        assert r.status_code == 200, r.text
    if path == "startup":
        await _reset_marker(session)
        await _start(session)

    r = await client.get(f"/lists/{quote}", headers=auth["headers"])
    assert r.json()["status"] == "finalized"
    assert not r.json().get("customs_saved")
    assert not r.json()["line_items"][0].get("hs_code")
    assert not r.json()["line_items"][0].get("country_of_origin")


@pytest.mark.parametrize("state, reads", [
    ({"status": "draft"}, True),
    ({"status": "draft", "customs_saved": True}, True),
    ({"status": "finalized"}, True),
    ({"status": "finalized", "customs_saved": True}, False),
])
async def test_one_rule_says_when_a_printout_reads_the_items_codes(state, reads):
    """The share page and the print view both ask reads_item_customs, and nothing else in the
    app reads ``customs_saved`` to decide it."""
    from celerp.services.shipping import reads_item_customs

    assert reads_item_customs(state) is reads
    root = Path(__file__).resolve().parent.parent
    readers = sorted(str(path.relative_to(root)) for top in ("celerp", "default_modules", "ui")
                     for path in (root / top).rglob("*.py") if 'get("customs_saved")' in path.read_text())
    assert readers == ["celerp/services/shipping.py", "default_modules/celerp-docs/celerp_docs/customs_freeze.py"]


async def _weighed_item(client, auth) -> str:
    """A lot of 5 bowls sold by the piece: the lot weighs 3 kg and each packed bowl 2 kg."""
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"CF-{uuid.uuid4().hex[:6]}", "name": "Teak bowl", "quantity": 5.0, "sell_by": "piece",
        "category": "Raw", "status": "available", "hs_code": "1234", "country_of_origin": "TH",
        "weight": 3.0, "weight_unit": "kg", "gross_weight": 2.0, "gross_weight_unit": "kg",
        "barcode": str(uuid.uuid4().int)[:12]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _whole_lot_list(client, auth, item_id: str) -> str:
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "shipping_doc", "contact_name": "Buyer",
        "line_items": [{**_line(item_id), "quantity": 5, "line_total": 200.0}]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _show_barcodes(session, auth) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, auth["company_id"])
    company.settings = {**(company.settings or {}), "line_item_identifier": "barcode"}
    await session.commit()


async def _reweigh_and_relabel(client, auth, item_id: str) -> None:
    """Every item field a shipping document prints, changed after Issue."""
    old = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    r = await client.patch(f"/items/{item_id}", headers=auth["headers"], json={"fields_changed": {
        "gross_weight": {"old": 2.0, "new": 7.5}, "weight": {"old": 3.0, "new": 9.0},
        "weight_unit": {"old": "kg", "new": "lb"}, "barcode": {"old": old.get("barcode"), "new": "8800000000017"}}})
    assert r.status_code == 200, r.text


def _printed(line: dict) -> tuple:
    return line.get("gross_weight"), line.get("gross_weight_unit"), line.get("weight"), line.get("weight_unit"), line.get("barcode")


async def _page_meta(client, auth, entity_id: str, item_id: str) -> tuple:
    r = await client.get(f"/lists/{entity_id}/page", headers=auth["headers"])
    assert r.status_code == 200, r.text
    meta = r.json()["item_meta"][item_id]
    return meta.get("gross_weight"), meta.get("weight"), meta.get("weight_unit"), meta.get("quantity"), meta.get("barcode")


async def test_issued_shipping_doc_keeps_the_weights_and_barcode_it_was_issued_with(client, auth, session):
    """Gross weight, the whole-lot weight and the barcode an issued shipping document shows are the
    item's as it was at Issue, on the share page and the detail page, however the item changes later."""
    await _show_barcodes(session, auth)
    item_id = await _weighed_item(client, auth)
    ship = await _whole_lot_list(client, auth, item_id)
    await _issue(client, auth, ship)
    shared = _printed((await _shared_lines(session, auth, ship))[0])
    page = await _page_meta(client, auth, ship, item_id)
    assert shared[:4] == (10.0, "kg", 3.0, "kg") and shared[4]

    await _reweigh_and_relabel(client, auth, item_id)
    assert _printed((await _shared_lines(session, auth, ship))[0]) == shared
    assert await _page_meta(client, auth, ship, item_id) == page


async def test_print_view_of_issued_shipping_doc_keeps_the_gross_weight_it_was_issued_with(client, auth, ui_client):
    item_id = await _weighed_item(client, auth)
    ship = await _whole_lot_list(client, auth, item_id)
    await _issue(client, auth, ship)
    await _reweigh_and_relabel(client, auth, item_id)
    stored = (await client.get(f"/lists/{ship}", headers=auth["headers"])).json()
    live = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()

    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_list", new=AsyncMock(return_value=stored)), \
         patch("ui.api_client.get_item", new=AsyncMock(return_value=live)):
        r = await ui_client.get(f"/lists/{ship}/print?layout=commercial_invoice")
    assert r.status_code == 200, r.text
    assert "37.5" not in r.text   # 5 x the item's new 7.5 kg
    assert "10 kg" in r.text or "10.0 kg" in r.text


async def test_reissue_after_revert_to_draft_takes_the_item_as_it_is_then(client, auth, session):
    item_id = await _weighed_item(client, auth)
    ship = await _whole_lot_list(client, auth, item_id)
    await _issue(client, auth, ship)
    await _reweigh_and_relabel(client, auth, item_id)
    r = await client.post(f"/lists/{ship}/revert-to-draft", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _issue(client, auth, ship)

    assert _printed((await _shared_lines(session, auth, ship))[0])[:2] == (37.5, "kg")


async def test_startup_saves_the_weights_an_earlier_release_printed(client, auth, session):
    await _reset_marker(session)
    item_id = await _weighed_item(client, auth)
    ship = await _whole_lot_list(client, auth, item_id)
    await _issue(client, auth, ship)
    await _as_an_earlier_release_stored_it(session, auth, ship)
    printed = _printed((await _shared_lines(session, auth, ship))[0])
    assert printed[:2] == (10.0, "kg")

    await _start(session)
    await _reweigh_and_relabel(client, auth, item_id)
    assert _printed((await _shared_lines(session, auth, ship))[0]) == printed


async def _set_docs(session, company_id, on: bool) -> None:
    from celerp.modules.registry import get_enabled, set_enabled
    from celerp.services.company_lock import locked_company

    session.expire_all()
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    enabled = get_enabled(settings) if "enabled_modules" in settings else {"celerp-docs", "celerp-inventory"}
    company.settings = set_enabled(settings, (enabled | {"celerp-docs"}) if on else (enabled - {"celerp-docs"}))
    await session.commit()


async def test_startup_freeze_waits_for_a_company_with_documents_off(client, auth, session):
    """A company with Documents off keeps its lists untouched and leaves the marker unset, so the
    start after it turns Documents back on saves them; nothing is counted as failed meanwhile."""
    from celerp_docs.customs_freeze import freeze_issued_customs_lists

    await _reset_marker(session)
    item_id = await _item(client, auth)
    ship = await _issued_by_an_earlier_release(client, auth, session, item_id)
    await _set_docs(session, auth["company_id"], on=False)
    before = await _updates(session, auth["company_id"])

    result = await freeze_issued_customs_lists(session)
    await session.commit()
    assert result["failed"] == 0 and result["staged"] == 1
    assert await _updates(session, auth["company_id"]) == before
    assert await _marker(session) is None

    await _set_docs(session, auth["company_id"], on=True)
    await _start(session)
    assert (await _lines(client, auth, ship))[0]["hs_code"] == "1234"
    assert await _marker(session)


async def test_a_company_with_documents_off_and_every_list_saved_lets_the_freeze_finish(client, auth, session):
    """Only a list still lacking its item details is work that waits: a company with Documents
    off whose issued lists are all saved does not keep the startup freeze running."""
    from celerp_docs.customs_freeze import freeze_issued_customs_lists

    item_id = await _item(client, auth)
    ship = await _list(client, auth, "shipping_doc", item_id)
    await _issue(client, auth, ship)
    await _set_docs(session, auth["company_id"], on=False)
    await _reset_marker(session)

    result = await freeze_issued_customs_lists(session)
    await session.commit()
    assert result["failed"] == 0 and result["staged"] == 0
    assert await _marker(session)
