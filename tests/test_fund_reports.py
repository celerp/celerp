# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The fund reports: the fund balance sheet, fund activity and the fund reconciliation.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank, 4100
sales revenue, 6200 rent), funds turned on with a restricted fund R (Library gift), two
board-designated funds U1 and U2 and the restricted net assets account
(test_fund_movements._setup). Money enters a fund through journals emitted directly on it.
"""
from __future__ import annotations

import re
import uuid

import pytest

from celerp.events.engine import emit_event
from test_change_fund import REASON, _change_doc, _outside_invoice
from test_fund_movement_posting import _bill, _close, _post, _set_role
from test_fund_movements import _emit, _setup
from test_income_accounts import _finalize_entry, _issued, _item, _line, ui_client  # noqa: F401

pytestmark = pytest.mark.asyncio

UNRESTRICTED = "unrestricted"


async def _get(client, auth, path, **params):
    r = await client.get(f"/accounting/funds/{path}".rstrip("/"), headers=auth["headers"], params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _gift(session, auth, fund, amount, ts):
    return await _emit(session, auth, [("1111", amount, 0, fund), ("4100", 0, amount, fund)], ts=ts)


def _row(report: dict, fund_id) -> dict:
    return next(f for f in report["funds"] if f["id"] == (fund_id or UNRESTRICTED))


async def test_funds_empty_state_and_reports_unrestricted_only(client, auth):
    """Funds on and no fund added: the list is empty and both reports show the unrestricted
    group only, holding everything."""
    r = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": True})
    assert r.status_code == 200, r.text
    assert (await _get(client, auth, ""))["items"] == []
    sheet = await _get(client, auth, "balance-sheet")
    activity = await _get(client, auth, "activity")
    assert [f["id"] for f in sheet["funds"]] == [UNRESTRICTED]
    assert [f["id"] for f in activity["funds"]] == [UNRESTRICTED]
    assert activity["movements"] == [] and activity["fund_changes"] == []


async def test_fund_balance_sheet_by_fund(client, session, auth):
    """Each balance-sheet account split by fund with its total, each fund's net assets, and the
    restricted net assets shown apart from what is available; as_of drops later entries."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"], 100, "2026-02-01")
    await _gift(session, auth, None, 50, "2026-02-01")
    await _bill(session, auth, f["R"], 30, ts="2026-03-01")
    sheet = await _get(client, auth, "balance-sheet")
    bank = next(a for a in sheet["accounts"] if a["code"] == "1111")
    assert bank["amounts"] == {f["R"]: 70.0, UNRESTRICTED: 50.0} and bank["total"] == 120.0
    assert {a["account_type"] for a in sheet["accounts"]} <= {"asset", "liability"}
    assert _row(sheet, f["R"])["net_assets"] == 70.0 and _row(sheet, None)["net_assets"] == 50.0
    assert _row(sheet, f["R"])["restricted"] is True
    assert sheet["restricted_net_assets"] == 70.0 and sheet["available_net_assets"] == 50.0
    earlier = await _get(client, auth, "balance-sheet", as_of="2026-02-28")
    assert _row(earlier, f["R"])["net_assets"] == 100.0


async def test_fund_activity_by_fund_lists_movements_and_reversal(client, session, auth):
    """For a period, each fund's opening, received, spent, released, transferred and closing net
    assets, closing = opening + received - spent + released + transferred; the period's
    movements are listed with their reason, and a reversed movement names its reversal."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"], 100, "2026-02-01")
    await _gift(session, auth, None, 50, "2026-02-01")
    await _gift(session, auth, f["R"], 5, "2026-03-02")
    await _bill(session, auth, f["R"], 30, ts="2026-03-01")
    spent = await _bill(session, auth, None, 20, ts="2026-03-01")
    r = await _post(client, auth, "release", fund=f["R"], to_fund=None,
                    backing=[{"je_id": spent, "line_index": 0, "amount": 20}])
    assert r.status_code == 200, r.text
    r = await _post(client, auth, "transfer", from_fund=None, to_fund=f["U1"], amount=10)
    assert r.status_code == 200, r.text
    transfer = r.json()["je_id"]
    activity = await _get(client, auth, "activity", date_from="2026-03-01", date_to="2026-03-31")
    columns = ("opening", "received", "spent", "released", "transferred", "closing")
    assert {c: _row(activity, f["R"])[c] for c in columns} == dict(zip(columns, (100.0, 5.0, 30.0, -20.0, 0.0, 55.0)))
    assert {c: _row(activity, None)[c] for c in columns} == dict(zip(columns, (50.0, 0.0, 20.0, 20.0, -10.0, 40.0)))
    assert {c: _row(activity, f["U1"])[c] for c in columns} == dict(zip(columns, (0.0, 0.0, 0.0, 0.0, 10.0, 10.0)))
    assert sorted(m["je_type"] for m in activity["movements"]) == ["fund_release", "fund_transfer"]
    assert {m["reason"] for m in activity["movements"]} == {"Board minute 12"}
    r = await client.post(f"/accounting/funds/movements/{transfer}/reverse", headers=auth["headers"],
                          json={"date": "2026-03-20", "reason": "Entered twice"})
    assert r.status_code == 200, r.text
    reversal = r.json()["je_id"]
    activity = await _get(client, auth, "activity", date_from="2026-03-01", date_to="2026-03-31")
    moved = {m["entity_id"]: m for m in activity["movements"]}
    assert moved[transfer]["reversed_by"] == reversal and moved[reversal]["reverses"] == transfer
    assert _row(activity, f["U1"])["closing"] == 0.0 and _row(activity, None)["transferred"] == 0.0
    assert moved[transfer]["amounts"] == {f["U1"]: 10.0, UNRESTRICTED: -10.0}


async def test_fund_activity_leaves_year_close_out(client, session, auth):
    """A year close moves each fund's income and costs into its net assets: the year's
    received and spent still show what came in and went out, and closing is unchanged."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"], 100, "2026-02-01")
    await _bill(session, auth, f["R"], 30, ts="2026-03-01")
    r = await _close(client, auth)
    assert r.status_code == 200, r.text
    row = _row(await _get(client, auth, "activity", date_from="2026-01-01", date_to="2026-12-31"), f["R"])
    assert (row["received"], row["spent"], row["closing"]) == (100.0, 30.0, 70.0)


async def test_fund_activity_lists_fund_changes(client, session, auth):
    """Change fund on a posted record is listed under fund changes with its reason, the entries
    it voided and the copies it posted."""
    f = await _setup(client, auth)
    doc = await _outside_invoice(client, auth, f["U1"])
    r = await _change_doc(client, session, auth, doc, document=f["U2"])
    assert r.status_code == 200, r.text
    changes = (await _get(client, auth, "activity"))["fund_changes"]
    assert [(c["record_id"], c["reason"]) for c in changes] == [(doc, REASON)]
    assert changes[0]["posted"] == list(r.json()["copies"].values())
    assert changes[0]["voided"] == list(r.json()["copies"])
    recon = await _get(client, auth, "reconciliation")
    assert [c["record_id"] for c in recon["fund_changes"]] == [doc]


async def test_fund_reconciliation_balances_and_lists_overdrawn(client, session, auth):
    """The funds and the unrestricted group add up to the company total on every account; a
    restricted fund overdrawn on an account, or with net assets below zero, is listed."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"], 100, "2026-02-01")
    await _gift(session, auth, None, 50, "2026-02-01")
    recon = await _get(client, auth, "reconciliation")
    assert recon["balanced"] is True and recon["differences"] == []
    assert recon["overdrawn"] == [] and recon["negative_net_assets"] == []
    await _bill(session, auth, f["R"], 130, ts="2026-03-01")
    recon = await _get(client, auth, "reconciliation")
    assert recon["balanced"] is True
    assert [(o["fund_id"], o["account"], o["balance"]) for o in recon["overdrawn"]] == [(f["R"], "1111", -30.0)]
    assert [(n["fund_id"], n["net_assets"]) for n in recon["negative_net_assets"]] == [(f["R"], -30.0)]
    before = await _get(client, auth, "reconciliation", as_of="2026-02-28")
    assert before["overdrawn"] == []


async def test_two_entry_cross_fund_mirror_flagged(client, session, auth):
    """Dr Expense[R] / Cr Bank[R] then Dr Bank[U] / Cr Expense[U]: each entry posts, and the
    reconciliation lists the pair naming both entries."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"], 100, "2026-02-01")
    first = await _emit(session, auth, [("6200", 40, 0, f["R"]), ("1111", 0, 40, f["R"])])
    second = await _emit(session, auth, [("1111", 40, 0, None), ("6200", 0, 40, None)])
    mirrors = (await _get(client, auth, "reconciliation"))["mirrors"]
    assert [sorted(m["entries"]) for m in mirrors] == [sorted([first, second])]
    await _emit(session, auth, [("6200", 7, 0, f["R"]), ("1111", 0, 7, f["R"])])
    await _emit(session, auth, [("1111", 7, 0, f["R"]), ("6200", 0, 7, f["R"])])
    assert len((await _get(client, auth, "reconciliation"))["mirrors"]) == 1


async def test_document_entries_never_flagged_as_mirror(client, session, auth):
    """A payment received on one fund and an unrelated refund paid from another, both posted
    for their documents and carrying no entry type, swap each other's lines: neither is a
    manual entry, so the reconciliation lists no mirror."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"], 100, "2026-02-01")
    for n, lines in enumerate(([("1111", 50, 0, None), ("1120", 0, 50, None)],
                               [("1120", 50, 0, f["R"]), ("1111", 0, 50, f["R"])])):
        await emit_event(session, company_id=auth["company_id"], entity_id=f"je:auto:doc:INV-{n}:pay:0",
                         entity_type="journal_entry", event_type="acc.journal_entry.created",
                         data={"memo": "Payment", "ts": "2026-03-01", "status": "posted", "record_id": f"doc:INV-{n}",
                               "entries": [{"account": a, "debit": d, "credit": c, **({"fund_id": fd} if fd else {})}
                                           for a, d, c, fd in lines]},
                         actor_id=auth["user_id"], location_id=None, source="test",
                         idempotency_key=f"test:mirror-doc:{n}:{uuid.uuid4().hex}")
    assert (await _get(client, auth, "reconciliation"))["mirrors"] == []


async def test_attribution_check_lists_document_entry_without_line_fund(client, session, auth):
    """An entry posted for a document is compared with the funds its lines carry: the
    document's own entry is clean; an entry that posts its income unrestricted while the line
    carries a fund, or a leg on another fund than the line it names, is listed by entry."""
    f = await _setup(client, auth)
    gift = await _item(client, auth, "Gift")
    doc = await _issued(client, auth, "invoice", [_line(gift, "Gift", 100, fund_id=f["R"])],
                        subtotal=100, tax=0, total=100)
    await _finalize_entry(session, auth, doc)
    assert (await _get(client, auth, "reconciliation"))["attribution"] == []

    async def planted(entries):
        je_id = f"je:auto:{doc}:{uuid.uuid4().hex[:6]}"
        await emit_event(session, company_id=auth["company_id"], entity_id=je_id, entity_type="journal_entry",
                         event_type="acc.journal_entry.created",
                         data={"memo": "Planted", "ts": "2026-03-01", "status": "posted", "record_id": doc,
                               "je_type": "auto_posted", "entries": entries},
                         actor_id=auth["user_id"], location_id=None, source="test",
                         idempotency_key=f"test:{je_id}", metadata_={})
        await session.commit()
        return je_id

    bare = await planted([{"account": "1111", "debit": 10, "credit": 0}, {"account": "4100", "debit": 0, "credit": 10}])
    other = await planted([{"account": "1111", "debit": 5, "credit": 0, "fund_id": f["U1"], "source_lines": [1]},
                           {"account": "4100", "debit": 0, "credit": 5, "fund_id": f["U1"], "source_lines": [1]}])
    listed = (await _get(client, auth, "reconciliation"))["attribution"]
    assert sorted({a["entity_id"] for a in listed}) == sorted([bare, other])
    by_entry = {a["entity_id"]: a for a in listed}
    assert by_entry[bare]["posted_fund"] is None and by_entry[bare]["line_fund"] == f["R"]
    assert by_entry[bare]["record_id"] == doc
    assert by_entry[other]["posted_fund"] == f["U1"] and by_entry[other]["line"] == 1


async def test_fund_report_routes_permissions(client, session, auth):
    """The fund reports read with view_financial_reports; a role without it is refused."""
    await _setup(client, auth)
    await _set_role(session, auth, "viewer")
    await session.commit()
    for path in ("balance-sheet", "activity", "reconciliation"):
        r = await client.get(f"/accounting/funds/{path}", headers=auth["headers"])
        assert r.status_code == 403, (path, r.text)


async def test_funds_tab_empty_state_add_and_archive(ui_client):
    """The Funds tab in Accounting settings: off offers Turn funds on; on with no fund shows the
    empty state and Add fund; a fund row clicks to edit and archives; a refusal heads the tab."""
    from unittest.mock import AsyncMock

    from test_income_accounts import _ui_api
    from ui.api_client import APIError

    off = {"name": "Test Corp", "current_role": "owner", "settings": {"modules": ["celerp-accounting"]}}
    on = {"name": "Test Corp", "current_role": "owner", "settings": {"modules": ["celerp-accounting"], "funds_enabled": True}}
    with _ui_api(get_company=AsyncMock(return_value=off), list_funds=AsyncMock(return_value={"items": []})):
        r = await ui_client.get("/settings/accounting", params={"tab": "funds"})
    page = r.text
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    assert "Turn funds on" in page and "No funds yet" not in page
    with _ui_api(get_company=AsyncMock(return_value=on), list_funds=AsyncMock(return_value={"items": []})):
        page = (await ui_client.get("/settings/accounting", params={"tab": "funds"})).text
    assert "No funds yet. Everything posts as unrestricted until you add one." in page and "Add fund" in page

    fund = {"id": "fund:1", "name": "Library gift", "restricted": True, "purpose": "Books",
            "opening_date": "2026-01-05", "archived": False}
    create = AsyncMock(return_value=fund)
    no_spending = AsyncMock(return_value={"items": []})
    with _ui_api(get_company=AsyncMock(return_value=on), list_funds=AsyncMock(return_value={"items": [fund]}),
                 create_fund=create, list_release_spending=no_spending):
        r = await ui_client.post("/settings/accounting/funds", data={
            "name": "Library gift", "restricted": "1", "purpose": "Books", "opening_date": "2026-01-05",
            "idempotency_key": "k1"})
    body = create.await_args.args[1]
    assert body["restricted"] is True and body["purpose"] == "Books" and body["request_key"]
    assert body["opening_date"] == "2026-01-05" and "Opening date" in r.text and "5 Jan 2026" in r.text
    assert "Library gift added." in r.text and 'hx-get="/settings/accounting/funds/fund%3A1/edit"' in r.text
    assert "editable-cell" in r.text and "/settings/accounting/funds/fund%3A1/archive" in r.text

    with _ui_api(list_funds=AsyncMock(return_value={"items": [fund]})):
        editor = (await ui_client.get("/settings/accounting/funds/fund%3A1/edit")).text
    assert 'type="date" name="opening_date" value="2026-01-05"' in editor
    update = AsyncMock(return_value={**fund, "opening_date": None})
    with _ui_api(update_fund=update):
        r = await ui_client.patch("/settings/accounting/funds/fund%3A1", data={
            "name": "Library gift", "purpose": "Books", "opening_date": ""})
    assert update.await_args.args[2] == {"name": "Library gift", "purpose": "Books", "opening_date": ""}
    assert "5 Jan 2026" not in r.text and r.status_code == 200

    refused = AsyncMock(side_effect=APIError(422, "These funds still hold balances: Library gift: 1111."))
    with _ui_api(get_company=AsyncMock(return_value=on), list_funds=AsyncMock(return_value={"items": [fund]}),
                 archive_fund=refused, list_release_spending=no_spending):
        r = await ui_client.post("/settings/accounting/funds/fund%3A1/archive")
    assert "error-banner" in r.text and "still hold balances" in r.text and "Library gift" in r.text


async def test_funds_tab_moves_money_between_funds(ui_client):
    """With funds on, the Funds tab offers the four movements: Transfer between funds,
    Release from restriction with the purpose spending it can name, Reallocate restricted
    funds with the donor's authorization, and Opening balances by fund with a line per
    account. Each posts with its own key and heads the tab with what it posted; a refusal
    heads the tab with the reason and keeps the form open with what was typed."""
    from unittest.mock import AsyncMock

    from test_income_accounts import _ui_api
    from ui.api_client import APIError

    on = {"name": "Test Corp", "current_role": "owner", "settings": {"modules": ["celerp-accounting"], "funds_enabled": True}}
    funds = [{"id": "fund:1", "name": "Library gift", "restricted": True, "purpose": "Books", "archived": False},
             {"id": "fund:2", "name": "Building reserve", "restricted": False, "purpose": None, "archived": False}]
    chart = {"items": [{"code": "1111", "name": "Bank", "account_type": "asset", "is_active": True},
                       {"code": "6200", "name": "Rent", "account_type": "expense", "is_active": True}]}
    spending = {"items": [{"je_id": "je:manual:1", "line_index": 0, "date": "2026-02-01", "memo": "Book order",
                           "account": "6200", "description": None, "left": 60.0}]}
    move = AsyncMock(return_value={"je_id": "je:fund:1"})

    def api(**more):
        return _ui_api(**{"get_company": AsyncMock(return_value=on), "list_funds": AsyncMock(return_value={"items": funds}),
                          "get_chart": AsyncMock(return_value=chart), "move_funds": move,
                          "list_release_spending": AsyncMock(return_value=spending), **more})

    with api():
        page = (await ui_client.get("/settings/accounting", params={"tab": "funds"})).text
    for title in ("Transfer between funds", "Release from restriction", "Reallocate restricted funds",
                  "Opening balances by fund"):
        assert title in page, title
    assert "Book order" in page and "60.00" in page and 'value="je:manual:1|0"' in page
    assert "1111 Bank" in page and "6200 Rent" not in page.split("Opening balances by fund")[1]

    form = {"idempotency_key": "k-t", "date": "2026-03-02", "reason": "Board minute 12", "from_fund": "fund:2",
            "to_fund": "unrestricted", "account": "", "amount": "50"}
    with api():
        r = await ui_client.post("/settings/accounting/funds/transfer", data=form)
    kind, body = move.await_args.args[1:]
    assert kind == "transfer" and body == {"request_key": "k-t", "date": "2026-03-02", "reason": "Board minute 12",
                                           "from_fund": "fund:2", "to_fund": None, "account": None, "amount": "50"}
    assert "Transfer between funds posted." in r.text and "success-banner" in r.text

    with api():
        r = await ui_client.post("/settings/accounting/funds/release", data={
            "idempotency_key": "k-r", "date": "2026-03-02", "reason": "Books bought", "fund": "fund:1",
            "to_fund": "unrestricted", "account": "1111", "backing": "je:manual:1|0", "release:je:manual:1|0": "25"})
    kind, body = move.await_args.args[1:]
    assert kind == "release" and body["fund"] == "fund:1" and body["to_fund"] is None and body["account"] == "1111"
    assert body["backing"] == [{"je_id": "je:manual:1", "line_index": 0, "amount": "25"}]

    refused = AsyncMock(side_effect=APIError(422, "Reallocating needs the reallocate_restricted_funds permission."))
    with api(move_funds=refused):
        r = await ui_client.post("/settings/accounting/funds/reallocate", data={
            "idempotency_key": "k-a", "date": "2026-03-02", "reason": "Donor letter", "from_fund": "fund:1",
            "to_fund": "fund:1", "account": "", "amount": "10", "donor_authorization": "Letter 2026-14"})
    assert refused.await_args.args[2]["donor_authorization"] == "Letter 2026-14"
    assert "error-banner" in r.text and "reallocate_restricted_funds" in r.text
    assert 'value="Letter 2026-14"' in r.text and 'value="Donor letter"' in r.text and "<details open" in r.text

    with api():
        r = await ui_client.post("/settings/accounting/funds/opening", data={
            "idempotency_key": "k-o", "date": "2026-01-01", "reason": "Opening by fund",
            "account": ["1111", "1111", ""], "fund": ["fund:1", "fund:2", ""], "amount": ["40", "15", ""]})
    kind, body = move.await_args.args[1:]
    assert kind == "opening" and body["assignments"] == [
        {"account": "1111", "fund": "fund:1", "amount": "40"}, {"account": "1111", "fund": "fund:2", "amount": "15"}]
    assert "Opening balances by fund posted." in r.text

    with api():
        line = (await ui_client.get("/settings/accounting/funds/opening-line", headers={"HX-Request": "true"})).text.strip()
    assert line.startswith("<tr") and 'name="account"' in line and 'name="fund"' in line and 'name="amount"' in line


_FUNDS = [{"id": "fund:1", "name": "Library gift", "restricted": True},
          {"id": UNRESTRICTED, "name": "Unrestricted", "restricted": False}]
_SHEET = {"as_of": "2026-03-31", "funds": [{**_FUNDS[0], "net_assets": 60.0}, {**_FUNDS[1], "net_assets": 40.0}],
          "accounts": [{"code": "1111", "name": "Bank", "account_type": "asset",
                        "amounts": {"fund:1": 60.0, UNRESTRICTED: 40.0}, "total": 100.0}],
          "restricted_net_assets": 60.0, "available_net_assets": 40.0}
_ACTIVITY = {"date_from": "2026-01-01", "date_to": "2026-03-31",
             "funds": [{**_FUNDS[0], "opening": 0.0, "received": 100.0, "spent": -15.0, "released": -25.0,
                        "transferred": 0.0, "closing": 60.0},
                       {**_FUNDS[1], "opening": 0.0, "received": 0.0, "spent": 0.0, "released": 25.0,
                        "transferred": 15.0, "closing": 40.0}],
             "movements": [
                 {"entity_id": "je:fund:3", "je_type": "fund_transfer", "date": "2026-03-03", "memo": None,
                  "reason": "Wrong fund", "reverses": "je:fund:2", "reversed_by": None, "amounts": {}},
                 {"entity_id": "je:fund:2", "je_type": "fund_transfer", "date": "2026-03-02", "memo": None,
                  "reason": "Board minute 12", "reverses": None, "reversed_by": "je:fund:3", "amounts": {}},
                 {"entity_id": "je:fund:1", "je_type": "fund_release", "date": "2026-03-01", "memo": None,
                  "reason": "Books bought", "reverses": None, "reversed_by": None,
                  "amounts": {"fund:1": -25.0, UNRESTRICTED: 25.0}}],
             "fund_changes": [{"record_id": "doc:BILL-7", "date": "2026-02-10", "reason": "Grant paid it",
                               "actor_id": None, "voided": ["je:a"], "posted": ["je:a:fund:1"]}]}
_RECONCILED = {"as_of": "2026-03-31", "balanced": True, "differences": [], "overdrawn": [], "negative_net_assets": [],
               "mirrors": [], "fund_changes": [], "attribution": []}
_UNRECONCILED = {**_RECONCILED, "balanced": False,
                 "differences": [{"date": "2026-02-01", "account": "1111", "company_total": 100.0, "funds_total": 90.0}],
                 "overdrawn": [{"fund_id": "fund:1", "fund": "Library gift", "account": "1111", "balance": -5.0}],
                 "negative_net_assets": [{"fund_id": "fund:1", "fund": "Library gift", "net_assets": -5.0}],
                 "mirrors": [{"entries": ["je:manual:4", "je:manual:5"]}],
                 "attribution": [{"entity_id": "je:doc:9", "record_id": "doc:INV-9", "doc_type": "invoice", "line": 1,
                                  "posted_fund": None, "line_fund": "fund:1"}]}


def _report_api(settings: dict | None = None, **more):
    from unittest.mock import AsyncMock

    from test_income_accounts import _ui_api

    company = {"name": "Test Corp", "currency": "USD", "current_role": "owner",
               "settings": {"modules": ["celerp-accounting"], **({"funds_enabled": True} if settings is None else settings)}}
    calls = {"get_company": AsyncMock(return_value=company), "list_funds": AsyncMock(return_value={"items": _FUNDS[:1]}),
             "get_fund_balance_sheet": AsyncMock(return_value=_SHEET),
             "get_fund_activity": AsyncMock(return_value=_ACTIVITY),
             "get_fund_reconciliation": AsyncMock(return_value=_RECONCILED),
             "get_fund_waiting": AsyncMock(return_value={"count": 3, "held": 2, "documents": ["doc:a"]})}
    return _ui_api(**{**calls, **more})


async def test_fund_report_pages(ui_client):
    """With funds on, the reports index and the report links name the fund balance sheet,
    fund activity and the fund reconciliation; each page shows its figures by fund with Print
    and CSV, and the outside records waiting for a fund choice head it. Funds off lists none."""
    with _report_api(settings={}):
        page = (await ui_client.get("/reports")).text
    assert "Fund balance sheet" not in page and "/reports/funds/" not in page
    with _report_api():
        page = (await ui_client.get("/reports")).text
    for path in ("/reports/funds/balance-sheet", "/reports/funds/activity", "/reports/funds/reconciliation"):
        assert f'href="{path}"' in page, path

    with _report_api():
        page = (await ui_client.get("/reports/funds/balance-sheet", params={"as_of": "2026-03-31"})).text
    assert "Library gift" in page and "1111 Bank" in page and "60.00" in page and "100.00" in page
    assert "Restricted net assets" in page and "Available net assets" in page
    assert "3 outside documents waiting for a fund choice" in page and 'href="/docs?fund_choice=needed"' in page
    assert "/reports/print/funds/balance-sheet?as_of=2026-03-31" in page
    assert "/reports/export/funds/balance-sheet/csv?as_of=2026-03-31" in page

    with _report_api():
        page = (await ui_client.get("/reports/funds/activity", params={"from": "2026-01-01", "to": "2026-03-31"})).text
    for word in ("Opening", "Received", "Spent", "Released", "Transferred", "Closing", "Books bought", "Grant paid it",
                 "doc:BILL-7", "Release from restriction"):
        assert word in page, word
    # Only the standing movement offers Reverse; the reversed one and its reversal name each other.
    assert page.count('hx-post="/reports/funds/movements/') == 1
    assert 'hx-post="/reports/funds/movements/je%3Afund%3A1/reverse"' in page
    assert "Reversed by je:fund:3" in page and "Reverses je:fund:2" in page

    with _report_api():
        page = (await ui_client.get("/reports/funds/reconciliation", params={"as_of": "2026-03-31"})).text
    assert "Every account's funds add up to the company ledger." in page
    with _report_api(get_fund_reconciliation=__import__("unittest.mock").mock.AsyncMock(return_value=_UNRECONCILED)):
        page = (await ui_client.get("/reports/funds/reconciliation", params={"as_of": "2026-03-31"})).text
    for word in ("90.00", "-$5.00", "je:manual:4", "doc:INV-9", "Library gift"):
        assert word in page, word
    assert "Every account's funds add up to the company ledger." not in page

    with _report_api():
        for key in ("balance-sheet", "activity", "reconciliation"):
            printed = await ui_client.get(f"/reports/print/funds/{key}")
            assert printed.status_code == 200 and "window.print" in printed.text, key
            exported = await ui_client.get(f"/reports/export/funds/{key}/csv")
            assert exported.status_code == 200 and "text/csv" in exported.headers["content-type"], key
            assert ("Balanced,yes" if key == "reconciliation" else "Library gift") in exported.text, key


async def test_fund_activity_reverses_a_movement(ui_client):
    """Reverse on a standing movement posts its reversal with the date and reason given and
    redraws the page headed by what it did; a refusal heads it with the reason and keeps what
    was typed."""
    from unittest.mock import AsyncMock

    from ui.api_client import APIError

    reverse = AsyncMock(return_value={"je_id": "je:fund:4"})
    with _report_api(reverse_fund_movement=reverse):
        r = await ui_client.post("/reports/funds/movements/je%3Afund%3A1/reverse",
                                 data={"date": "2026-03-05", "reason": "Released too early",
                                       "from": "2026-01-01", "to": "2026-03-31"})
    assert reverse.await_args.args[1:] == ("je:fund:1", {"date": "2026-03-05", "reason": "Released too early"})
    assert "success-banner" in r.text and "Movement reversed." in r.text and "Books bought" in r.text

    refused = AsyncMock(side_effect=APIError(422, "Reversing this takes money out of a restricted fund."))
    with _report_api(reverse_fund_movement=refused):
        r = await ui_client.post("/reports/funds/movements/je%3Afund%3A1/reverse",
                                 data={"date": "2026-03-05", "reason": "Released too early"})
    assert "error-banner" in r.text and "restricted fund" in r.text
    assert 'value="Released too early"' in r.text and "<details open" in r.text


async def test_period_lock_tab_reverses_a_year_close(ui_client):
    """The period lock tab offers Reverse close beside Close year: the year end and a reason
    undo the latest close of that year; a refusal heads the tab and keeps what was typed."""
    from unittest.mock import AsyncMock

    from ui.api_client import APIError

    lock = AsyncMock(return_value={"lock_date": None})
    owner = {"name": "Test Corp", "current_role": "owner", "settings": {"modules": ["celerp-accounting"]}}

    def _ui_api(**more):
        from test_income_accounts import _ui_api as patched

        return patched(get_company=AsyncMock(return_value=owner), **more)
    with _ui_api(get_period_lock=lock):
        r = await ui_client.get("/settings/accounting", params={"tab": "period-lock"})
    page = r.text
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    assert "Reverse close" in page and 'hx-post="/settings/accounting/close-year/reverse"' in page

    reverse = AsyncMock(return_value={"je_id": "je:close:1:reverse"})
    with _ui_api(get_period_lock=lock, reverse_year_close=reverse):
        r = await ui_client.post("/settings/accounting/close-year/reverse",
                                 data={"fiscal_year_end": "2025-12-31", "reason": "Late bill"})
    assert reverse.await_args.args[1:] == ("2025-12-31", "Late bill")
    assert "success-banner" in r.text and "The close of 2025-12-31 is reversed." in r.text

    refused = AsyncMock(side_effect=APIError(404, "No close of 2025-12-31 to reverse."))
    with _ui_api(get_period_lock=lock, reverse_year_close=refused):
        r = await ui_client.post("/settings/accounting/close-year/reverse",
                                 data={"fiscal_year_end": "2025-12-31", "reason": "Late bill"})
    assert "error-banner" in r.text and "No close of 2025-12-31" in r.text and 'value="Late bill"' in r.text


async def test_fund_changes_dated_on_the_business_day(client, session, auth):
    """A fund change is dated on the company's business day, not the server's UTC day: one made
    at 20:00 UTC on 31 March in Bangkok is listed on 1 April and falls inside an April period."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from celerp.models.ledger import LedgerEntry
    from celerp.services.company_lock import locked_company
    from celerp_accounting.fund_reports import FUND_CHANGE_EVENTS

    f = await _setup(client, auth)
    doc = await _outside_invoice(client, auth, f["U1"])
    assert (await _change_doc(client, session, auth, doc, document=f["U2"])).status_code == 200
    company = await locked_company(session, auth["company_id"])
    company.settings = {**(company.settings or {}), "timezone": "Asia/Bangkok"}
    await session.execute(update(LedgerEntry).where(LedgerEntry.event_type.in_(FUND_CHANGE_EVENTS),
                                                    LedgerEntry.entity_id == doc)
                          .values(ts=datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc)))
    await session.commit()
    april = await _get(client, auth, "activity", date_from="2026-04-01", date_to="2026-04-30")
    assert [(c["record_id"], c["date"]) for c in april["fund_changes"]] == [(doc, "2026-04-01")]
    march = await _get(client, auth, "activity", date_from="2026-03-01", date_to="2026-03-31")
    assert march["fund_changes"] == []


async def test_fund_movement_forms_dated_on_the_business_day(ui_client, monkeypatch):
    """Each movement form opens dated today in the company's timezone on the business clock,
    as a payment form does, not on the server's own date: at 12:00 UTC on 15 July 2027 a
    company on UTC+14 is already on 16 July."""
    from datetime import datetime as real_datetime
    from unittest.mock import AsyncMock

    import celerp.services.business_time as bt
    from test_income_accounts import _ui_api

    class Fixed(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2027, 7, 15, 12, 0, tzinfo=bt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(bt, "datetime", Fixed)
    on = {"name": "Test Corp", "current_role": "owner", "timezone": "Pacific/Kiritimati",
          "settings": {"modules": ["celerp-accounting"], "funds_enabled": True}}
    funds = [{"id": "fund:1", "name": "Library gift", "restricted": True, "purpose": "Books", "archived": False}]
    with _ui_api(get_company=AsyncMock(return_value=on), list_funds=AsyncMock(return_value={"items": funds}),
                 get_chart=AsyncMock(return_value={"items": []}),
                 list_release_spending=AsyncMock(return_value={"items": []})):
        page = (await ui_client.get("/settings/accounting", params={"tab": "funds"})).text
    dates = re.findall(r'<input[^>]*name="date"[^>]*>', page)
    assert len(dates) == 4 and all('value="2027-07-16"' in d for d in dates), dates
