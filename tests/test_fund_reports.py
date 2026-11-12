# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The fund reports: the fund balance sheet, fund activity and the fund reconciliation.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank, 4100
sales revenue, 6200 rent), funds turned on with a restricted fund R (Library gift), two
board-designated funds U1 and U2 and the restricted net assets account
(test_fund_movements._setup). Money enters a fund through journals emitted directly on it.
"""
from __future__ import annotations

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

    fund = {"id": "fund:1", "name": "Library gift", "restricted": True, "purpose": "Books", "archived": False}
    create = AsyncMock(return_value=fund)
    with _ui_api(get_company=AsyncMock(return_value=on), list_funds=AsyncMock(return_value={"items": [fund]}),
                 create_fund=create):
        r = await ui_client.post("/settings/accounting/funds", data={
            "name": "Library gift", "restricted": "1", "purpose": "Books", "idempotency_key": "k1"})
    body = create.await_args.args[1]
    assert body["restricted"] is True and body["purpose"] == "Books" and body["request_key"]
    assert "Library gift added." in r.text and 'hx-get="/settings/accounting/funds/fund%3A1/edit"' in r.text
    assert "editable-cell" in r.text and "/settings/accounting/funds/fund%3A1/archive" in r.text

    refused = AsyncMock(side_effect=APIError(422, "These funds still hold balances: Library gift: 1111."))
    with _ui_api(get_company=AsyncMock(return_value=on), list_funds=AsyncMock(return_value={"items": [fund]}),
                 archive_fund=refused):
        r = await ui_client.post("/settings/accounting/funds/fund%3A1/archive")
    assert "error-banner" in r.text and "still hold balances" in r.text and "Library gift" in r.text
