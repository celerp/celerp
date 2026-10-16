# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The firm view: every company the signed-in user can open, with its accountant sync,
last sync and what needs attention, built on the existing users and roles."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from company_backup_support import token
from migration_support import auth, maker, migration_env, real_client, real_engine, staged_run  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_sync_end_to_end import _paired, _route_to_app, _sync, seed_demo

pytestmark = pytest.mark.asyncio


async def _firm_view(client, tok: str, **params) -> dict:
    r = await client.get("/auth/firm-view", params=params or None, headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


def _row(view: dict, name: str) -> dict:
    return next(row for row in view["items"] if row["company_name"] == name)


async def _locked_invoice(real_client, tok: str, firm_tok: str, contacts: list) -> dict:
    """The firm closes the books through today; the client then finalizes a new invoice
    dated today. Returns the invoice."""
    today = date.today().isoformat()
    r = await real_client.post("/accounting/period-lock", json={"lock_date": today},
                               headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    items = (await real_client.get("/items", params={"limit": 50}, headers=auth(tok))).json()["items"]
    item = items[0]
    r = await real_client.post("/docs", json={
        "doc_type": "invoice", "contact_id": contacts[0]["id"] if isinstance(contacts[0], dict) else contacts[0],
        "line_items": [{"item_id": item.get("entity_id") or item["id"], "name": item["name"],
                        "quantity": 1, "unit_price": 30.0, "line_total": 30.0}],
        "total": 30.0, "idempotency_key": "firm-view:locked"}, headers=auth(tok))
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    await seed_demo._advance(real_client, auth(tok), doc_id, ("finalize",), "firm-view:locked")
    return (await real_client.get(f"/docs/{doc_id}", headers=auth(tok))).json()


async def test_accountant_role_and_firm_console(real_client, real_engine, monkeypatch):
    """No new role: the firm view lists exactly the companies the user can open, newest
    activity first, with sync status, last sync and each refused record by its number."""
    tok, firm_tok, client_company, credential, contacts = await _paired(real_client, real_engine)
    view = await _firm_view(real_client, firm_tok)
    names = [row["company_name"] for row in view["items"]]
    assert sorted(names) == ["Demo Trading", "Firm Practice"], names
    copy = _row(view, "Demo Trading")
    assert copy["sync"] == "on" and copy["last_sync"] is None
    assert _row(view, "Firm Practice")["sync"] == "off"

    invoice = await _locked_invoice(real_client, tok, firm_tok, contacts)
    sent: list = []
    _route_to_app(monkeypatch, sent)
    await _sync(client_company, credential)

    view = await _firm_view(real_client, firm_tok)
    assert view["items"][0]["company_name"] == "Demo Trading"  # newest activity first
    copy = view["items"][0]
    assert copy["sync"] == "failing"
    assert copy["last_sync"] is not None
    refused = [item for item in copy["attention"] if item["kind"] == "refused"]
    assert [item["label"] for item in refused] == [invoice["ref_id"]], copy["attention"]
    assert date.today().isoformat() in refused[0]["reason"]
    assert _row(view, "Firm Practice")["attention"] == []

    # The client sees only its own company; it has no accountant key of its own.
    mine = await _firm_view(real_client, tok)
    assert [row["company_name"] for row in mine["items"]] == ["Demo Trading"]
    assert mine["items"][0]["sync"] == "off"

    # Once the firm reopens the period and the record arrives, the item clears.
    r = await real_client.post("/accounting/period-lock", json={"lock_date": None},
                               headers=auth(firm_tok))
    assert r.status_code == 200, r.text

    async def everything(*_args, **_kw):
        return None
    monkeypatch.setattr("celerp.connectors.sync_runner._last_success_watermark", everything)
    await _sync(client_company, credential)
    copy = _row(await _firm_view(real_client, firm_tok), "Demo Trading")
    assert copy["sync"] == "on" and copy["attention"] == []


async def test_firm_view_search_and_filter_round_trip(real_client, real_engine):
    _, firm_tok, *_ = await _paired(real_client, real_engine)
    assert [r["company_name"] for r in (await _firm_view(real_client, firm_tok, q="demo"))["items"]] == ["Demo Trading"]
    assert [r["company_name"] for r in (await _firm_view(real_client, firm_tok, sync="off"))["items"]] == ["Firm Practice"]
    assert (await _firm_view(real_client, firm_tok, q="nothing like it"))["items"] == []
    r = await real_client.get("/auth/firm-view", params={"sync": "sometimes"}, headers=auth(firm_tok))
    assert r.status_code == 422
    assert "On, Off, Failing or Key expiring" in r.text


async def test_firm_view_key_expiring_and_expired(real_client, real_engine):
    from celerp.models.company_peer import CompanyPeerGrant
    _, firm_tok, *_ = await _paired(real_client, real_engine)
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    async with maker(real_engine)() as s:
        await s.execute(update(CompanyPeerGrant).values(expires_at=soon))
        await s.commit()
    copy = _row(await _firm_view(real_client, firm_tok), "Demo Trading")
    assert copy["sync"] == "expiring"
    expiring = [i for i in copy["attention"] if i["kind"] == "key_expiring"]
    assert len(expiring) == 1 and soon.date().isoformat() in expiring[0]["reason"]
    assert expiring[0]["href"] == "/settings/accountant-access"

    async with maker(real_engine)() as s:
        await s.execute(update(CompanyPeerGrant).values(
            expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await s.commit()
    copy = _row(await _firm_view(real_client, firm_tok), "Demo Trading")
    assert copy["sync"] == "failing"
    assert any(i["kind"] == "key_expired" for i in copy["attention"])


async def test_firm_view_copies_not_opened_and_unreadable(real_client, real_engine, monkeypatch):
    from celerp.services import company_handoff_sender as sender
    _, firm_tok, client_company, *_ = await _paired(real_client, real_engine)
    view = await _firm_view(real_client, firm_tok)
    copy_id, practice_id = _row(view, "Demo Trading")["company_id"], _row(view, "Firm Practice")["company_id"]

    async def copies(company_id):
        if str(company_id) == practice_id:
            raise sender.HandoffSendError("Company copy activity is unavailable")
        return [
            {"id": str(uuid.uuid4()), "status": "sent", "company_opened": False,
             "recipient": "owner@client.example", "sent_at": "2026-10-10T09:00:00+00:00",
             "expires_at": "2026-10-24T09:00:00+00:00"},
            {"id": str(uuid.uuid4()), "status": "expired", "company_opened": False,
             "recipient": None, "sent_at": "2026-09-01T09:00:00+00:00",
             "expires_at": "2026-09-15T09:00:00+00:00"},
            {"id": str(uuid.uuid4()), "status": "imported", "company_opened": True,
             "recipient": "done@client.example", "sent_at": "2026-08-01T09:00:00+00:00",
             "expires_at": "2026-08-15T09:00:00+00:00"},
        ]
    monkeypatch.setattr(sender, "company_copies", copies)
    view = await _firm_view(real_client, firm_tok)
    items = [(i["kind"], i["label"]) for i in _row(view, "Demo Trading")["attention"]]
    assert items == [("copy_not_opened", "owner@client.example"), ("copy_expired", "--")], items
    practice = _row(view, "Firm Practice")
    assert practice["attention"] == []
    assert practice["unavailable"] == ["Company copy activity is unavailable"]
    assert copy_id != practice_id


async def test_firm_view_lists_migrations_not_finished(real_client, real_engine, migration_env):
    run_id, _, user_id = await staged_run(real_engine, email="mover@example.test")
    from company_backup_support import company
    own = await company(real_engine, user_id, "Mover Books", "mover")
    view = await _firm_view(real_client, await token(real_engine, user_id, own))
    staged = [row for row in view["items"] if row["company_name"] == "Fake Co"]
    assert len(staged) == 1, view
    assert staged[0]["sync"] is None
    [item] = staged[0]["attention"]
    assert item["kind"] == "migration" and item["href"] == f"/migrations/{run_id}"


async def test_firm_view_hides_sync_from_other_roles(real_client, real_engine):
    from celerp.models.accounting import UserCompany
    from company_backup_support import owner
    _, firm_tok, *_ = await _paired(real_client, real_engine)
    copy_id = _row(await _firm_view(real_client, firm_tok), "Demo Trading")["company_id"]
    clerk = await owner(real_engine, "clerk@example.test", "Clerk")
    async with maker(real_engine)() as s:
        s.add(UserCompany(user_id=clerk, company_id=uuid.UUID(copy_id), role="operator"))
        await s.commit()
    view = await _firm_view(real_client, await token(real_engine, clerk, copy_id, "operator"))
    [row] = view["items"]
    assert row["company_name"] == "Demo Trading"
    assert row["sync"] is None and row["last_sync"] is None and row["attention"] == []
    assert row["unavailable"] == ["Only the company's owner sees its sync and what needs attention."]
