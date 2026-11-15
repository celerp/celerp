# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The funds, fund report, revenue recognition, year close, document fund and connection
default pages, and a record held for funds on the Sync accounts page, each submitted through
the UI app to the real API in process: every save leaves the stored effect its page reports.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank,
4100 sales revenue, 6200 rent), signed in to the UI as its admin (``_ui``). Fund tests turn
funds on with ``test_fund_movements._setup`` (restricted fund R for scholarships,
board-designated funds U1 and U2) unless they turn funds on through the page itself. The held
record test pairs a client and its accountant on the real engine
(``test_peer_fund_holds._held_firm_journal``)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from income_books import _item, _line
from migration_support import real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_change_fund import _outside_invoice
from test_fund_movement_posting import DATE, _bill, _close, _gift, _held
from test_fund_movements import _setup
from test_outside_lines import _doc, _imported
from test_revenue_recognition import _deferred_item, _freeze, _invoice, _runs, _service

pytestmark = pytest.mark.asyncio

FUNDS = "/settings/accounting/funds"
HX = {"HX-Request": "true"}


@asynccontextmanager
async def _ui(auth):
    from test_helpers import ui_signed_in

    async with ui_signed_in(auth["headers"]["Authorization"].removeprefix("Bearer ")) as ui:
        yield ui


def _key() -> str:
    return str(uuid.uuid4())


async def _funds(client, auth) -> dict[str, dict]:
    r = await client.get("/accounting/funds", headers=auth["headers"], params={"include_archived": "true"})
    assert r.status_code == 200, r.text
    return {f["name"]: f for f in r.json()["items"]}


async def _transfer_entry(session, auth) -> str:
    from sqlalchemy import select

    from celerp.models.projections import Projection

    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    [je_id] = [r.entity_id for r in rows if r.state.get("je_type") == "fund_transfer"]
    return je_id


async def test_funds_tab_turns_on_adds_renames_archives_and_restores(client, auth):
    """Turn funds on, add a fund, rename it, archive it and restore it, each from the Funds tab."""
    async with _ui(auth) as ui:
        r = await ui.post(f"{FUNDS}/setting", data={"enabled": "1"}, headers=HX)
        assert r.status_code == 200, r.text
        company = (await client.get("/companies/me", headers=auth["headers"])).json()
        assert company["settings"].get("funds_enabled") is True

        r = await ui.post(FUNDS, headers=HX, data={"name": "Roof appeal", "restricted": "1", "purpose": "Roof",
                                                    "opening_date": "", "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        fund = (await _funds(client, auth))["Roof appeal"]
        assert fund["restricted"] is True and fund["purpose"] == "Roof"

        r = await ui.patch(f"{FUNDS}/{fund['id']}", headers=HX,
                           data={"name": "Roof and gutters", "purpose": "Roof", "opening_date": ""})
        assert r.status_code == 200, r.text
        assert "Roof and gutters" in await _funds(client, auth)

        r = await ui.post(f"{FUNDS}/{fund['id']}/archive", headers=HX)
        assert r.status_code == 200, r.text
        assert (await _funds(client, auth))["Roof and gutters"]["archived"] is True
        r = await ui.post(f"{FUNDS}/{fund['id']}/unarchive", headers=HX)
        assert r.status_code == 200, r.text
        assert (await _funds(client, auth))["Roof and gutters"]["archived"] is False


async def test_movement_forms_post_and_fund_activity_reverses(client, session, auth):
    """Opening balances, a transfer, a reallocation and a release, each posted from its form on
    the Funds tab, move the money they name; Reverse on fund activity moves the transfer back."""
    f = await _setup(client, auth)
    await _gift(session, auth, None, 200)
    await _gift(session, auth, f["U1"], 100)
    await _gift(session, auth, f["R"], 100)
    spent = await _bill(session, auth, None, 40)
    r2 = (await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": "Library gift two", "restricted": True,
        "purpose": "Scholarships"})).json()["id"]
    await session.commit()
    common = {"date": DATE, "reason": "Board minute 12"}
    async with _ui(auth) as ui:
        r = await ui.post(f"{FUNDS}/opening", headers=HX, data={
            **common, "account": "1111", "fund": f["U2"], "amount": "30", "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        assert await _held(session, auth, f["U2"]) == 30

        r = await ui.post(f"{FUNDS}/transfer", headers=HX, data={
            **common, "from_fund": f["U1"], "to_fund": f["U2"], "amount": "25", "account": "",
            "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        assert (await _held(session, auth, f["U1"]), await _held(session, auth, f["U2"])) == (75, 55)

        r = await ui.post(f"{FUNDS}/reallocate", headers=HX, data={
            **common, "from_fund": f["R"], "to_fund": r2, "amount": "10", "account": "",
            "donor_authorization": "Donor letter 3", "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        assert await _held(session, auth, r2) == 10

        odd = f"{spent}|²"  # a line number no entry has: the form answers, nothing is released
        r = await ui.post(f"{FUNDS}/release", headers=HX, data={
            **common, "fund": f["R"], "to_fund": "", "account": "", "backing": odd, f"release:{odd}": "40",
            "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        assert await _held(session, auth, f["R"]) == 90

        ref = f"{spent}|0"
        r = await ui.post(f"{FUNDS}/release", headers=HX, data={
            **common, "fund": f["R"], "to_fund": "", "account": "", "backing": ref, f"release:{ref}": "40",
            "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        assert await _held(session, auth, f["R"]) == 50

        transfer = await _transfer_entry(session, auth)
        r = await ui.post(f"/reports/funds/movements/{transfer}/reverse", headers=HX,
                          data={"date": DATE, "reason": "Entered twice", "from": "2026-01-01", "to": "2026-12-31"})
        assert r.status_code == 200, r.text
        assert (await _held(session, auth, f["U1"]), await _held(session, auth, f["U2"])) == (100, 30)


async def test_year_close_reversed_from_period_lock_tab(client, session, auth):
    """A closed year, unlocked, is reversed from the period lock tab with its reason."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    await session.commit()
    assert (await _close(client, auth)).status_code == 200
    await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": None})
    async with _ui(auth) as ui:
        r = await ui.post("/settings/accounting/close-year/reverse", headers=HX,
                          data={"fiscal_year_end": "2026-12-31", "reason": "Late bills"})
        assert r.status_code == 200, r.text
    again = await client.post("/accounting/close-year/2026-12-31/reverse", headers=auth["headers"],
                              json={"reason": "Late bills"})
    assert again.status_code == 200 and again.json().get("je_id"), again.text


async def test_income_account_saved_from_its_cell(client, auth):
    """An item's income account chosen in its cell is stored on the item."""
    item = await _item(client, auth, "Annual membership")
    async with _ui(auth) as ui:
        r = await ui.patch("/settings/accounting/income-account", headers=HX,
                           params={"kind": "item", "key": item}, data={"value": "4100"})
        assert r.status_code == 200, r.text
    stored = (await client.get(f"/items/{item}", headers=auth["headers"])).json()
    assert stored["income_account"] == "4100"


async def test_recognition_setting_month_post_and_reverse(client, session, auth, monkeypatch):
    """Automatic posting turned on from its card is stored; a month posted from the page posts
    its run, and Reverse puts it back to outstanding."""
    _freeze(monkeypatch, "2026-01-20")
    plan = await _deferred_item(client, auth)
    await _invoice(client, auth, [_service(plan, 900, start="2026-01-01", end="2026-03-31")])
    _freeze(monkeypatch, "2026-02-02")
    period = {"from": "2026-01-01", "to": "2026-03-31"}
    async with _ui(auth) as ui:
        r = await ui.post("/accounting/recognition/setting", headers=HX, data={"enabled": "1"})
        assert r.status_code == 200, r.text
        setting = (await client.get("/accounting/recognition/setting", headers=auth["headers"])).json()
        assert setting["enabled"] is True

        r = await ui.post("/accounting/recognition/2026-01/post", headers=HX, data=period)
        assert r.status_code == 200, r.text
        assert {run["status"] for run in await _runs(client, auth) if run["month"] == "2026-01"} == {"posted"}
        r = await ui.post("/accounting/recognition/2026-01/reverse", headers=HX, data=period)
        assert r.status_code == 200, r.text
        assert {run["status"] for run in await _runs(client, auth) if run["month"] == "2026-01"} == {"outstanding"}


async def test_document_fund_chosen_and_changed_from_the_document(client, session, auth):
    """A draft from outside takes its fund from the document page; a posted one moves to
    another fund with Change fund and its reason."""
    f = await _setup(client, auth)
    a = await _item(client, auth, "Outside A")
    draft = await _imported(client, auth, [_line(a, "Outside A", 40)])
    posted = await _outside_invoice(client, auth, f["U1"])
    async with _ui(auth) as ui:
        r = await ui.post(f"/docs/{draft}/funds", headers=HX, data={
            "target": "document", "value": f["U2"], "expected_version": (await _doc(client, auth, draft))["version"]})
        assert r.status_code == 200, r.text
        assert (await _doc(client, auth, draft))["fund_choice"] == f["U2"]

        r = await ui.post(f"/docs/{posted}/funds/change", headers=HX, data={
            "target": "document", "value": f["U2"], "reason": "Charged to the wrong grant",
            "expected_version": (await _doc(client, auth, posted))["version"], "idempotency_key": _key()})
        assert r.status_code == 200, r.text
        assert (await _doc(client, auth, posted))["fund_choice"] == f["U2"]


async def test_connection_fund_default_saved_from_connectors(client, session, auth, connector_modules_running):
    """A connected accounting system's standing fund chosen on its connector page is stored."""
    from celerp.models.connector_config import ConnectorConfig

    f = await _setup(client, auth)
    session.add(ConnectorConfig(company_id=str(auth["company_id"]), connector="quickbooks"))
    await session.commit()
    async with _ui(auth) as ui:
        r = await ui.post("/settings/connectors/quickbooks/fund-default", headers=HX, data={
            "origin": "accounting_system", "source_key": "quickbooks", "value": f["R"], "expected_version": "0",
            "request_key": uuid.uuid4().hex})
        assert r.status_code == 200, r.text
    items = (await client.get("/accounting/funds/connection-defaults", headers=auth["headers"])).json()["items"]
    [saved] = [i for i in items if (i["origin"], i["source_key"]) == ("accounting_system", "quickbooks")]
    assert saved["fund_choice"] == f["R"]


async def test_record_held_for_funds_posted_from_sync_accounts(real_client, real_engine, monkeypatch):
    """A journal from the accountant held for a fund on each line, posted from the Sync accounts
    page with every line unrestricted: the hold clears and one journal posts, unrestricted."""
    from test_peer_fund_holds import BANK, EXPENSE, _held, _held_firm_journal
    from test_peer_two_way_sync import _from_firm, _journals

    tok, _, client_company, _, _, item, _ = await _held_firm_journal(
        real_client, real_engine, monkeypatch, [(BANK, 100.0, 0.0), (EXPENSE, 0.0, 100.0)])
    from test_helpers import ui_signed_in

    async with ui_signed_in(tok) as ui:
        r = await ui.post(f"/accounting/sync-accounts/holds/{item['id']}/funds", headers=HX, data={
            "revision": str(item["fix"]["revision"]), "request_key": uuid.uuid4().hex, "all": "unrestricted"})
        assert r.status_code == 200, r.text
    assert await _held(real_client, tok) == []
    [state] = _from_firm(await _journals(real_engine, client_company)).values()
    assert state["fund_choice"] == "unrestricted" and state["status"] == "posted"
