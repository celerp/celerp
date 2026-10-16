# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Two-way sync between a client company and its accountant: the accountant's journal
entries and lock date come down on the client's own run, the client's own journals and
its reversals go up, and each side is told what the other did."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_firm_view import _locked_invoice, _today
from test_peer_sync_end_to_end import FIRM, _company_of, _paired, _route_to_app

pytestmark = pytest.mark.asyncio

ADJUSTED = "2026-03-31"


async def _two_way(company_id: str, credential: str) -> dict:
    """One run of the connector both ways, in its own order; each entity's result."""
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.celerp import CelerpConnector
    from celerp.connectors.sync_runner import sync_plan

    connector = CelerpConnector()
    ctx = ConnectorContext(company_id=company_id, store_handle=FIRM, access_token=credential)
    results = {}
    for entity in sync_plan(connector, SyncDirection.BOTH):
        results[entity] = await getattr(connector, f"sync_{entity}")(ctx)
    return results


async def _post_journal(client, tok: str, ts: str, amount: float, memo: str,
                        debit: str = "6500", credit: str = "6200") -> str:
    r = await client.post("/accounting/journal-entries", headers=auth(tok), json={
        "ts": ts, "memo": memo, "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": debit, "debit": amount, "credit": 0.0},
                    {"account": credit, "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _journals(engine, company_id: str) -> dict[str, dict]:
    from celerp.models.projections import Projection
    async with maker(engine)() as s:
        rows = (await s.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == uuid.UUID(company_id),
            Projection.entity_type == "journal_entry"))).all()
    return {str(eid): dict(state or {}) for eid, state in rows}


async def _notices(engine, company_id: str) -> list[tuple[str, str, str | None]]:
    from celerp.models.notification import Notification
    async with maker(engine)() as s:
        rows = (await s.execute(select(Notification.title, Notification.body, Notification.action_url)
                                .where(Notification.company_id == uuid.UUID(company_id))
                                .order_by(Notification.created_at))).all()
    return [tuple(row) for row in rows]


async def _lock(client, tok: str, day: str | None) -> None:
    r = await client.post("/accounting/period-lock", json={"lock_date": day}, headers=auth(tok))
    assert r.status_code == 200, r.text


async def _attention(client, firm_tok: str) -> list[dict]:
    r = await client.get("/auth/firm-view", headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    return next(row for row in r.json()["items"] if row["company_name"] == "Demo Trading")["attention"]


async def _pair_journals(real_client, real_engine):
    """The paired companies, with the key also opening journals and the lock date."""
    tok, firm_tok, client_company, _, contacts = await _paired(real_client, real_engine)
    granted = await real_client.post("/company-peers/grants", json={
        "source_company_id": client_company, "label": "Demo Trading",
        "scopes": ["contacts", "products", "documents", "inventory", "journals"]},
        headers=auth(firm_tok))
    assert granted.status_code == 201, granted.text
    return tok, firm_tok, client_company, granted.json()["credential"], contacts


async def test_journal_routes_open_only_to_a_key_with_journals(real_client, real_engine, monkeypatch):
    """The firm's journal feed, lock date and journal intake refuse a signed-in user,
    even the firm's owner, and a key that was not given journals; a client holding such
    a key still syncs everything else cleanly."""
    _, firm_tok, client_company, contacts_key, _ = await _paired(real_client, real_engine)
    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, contacts_key)
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}
    calls = (("GET", "/accounting/peer/journals", None), ("GET", "/accounting/peer/lock", None),
             ("POST", "/accounting/peer/journals", {"records": []}))
    peer = {"Authorization": f"Peer {contacts_key}", "X-Celerp-Source-Company": client_company}
    for headers in (auth(firm_tok), peer):
        for method, path, body in calls:
            r = await real_client.request(method, path, json=body, headers=headers)
            assert r.status_code == 403, (method, path, r.text)


def _from_firm(journals: dict) -> dict[str, dict]:
    return {eid: s for eid, s in journals.items() if s.get("peer_origin")}


async def test_two_way_sync(real_client, real_engine, monkeypatch):
    """J13 / AC23: the accountant's journal posts on the client once, with a notice naming
    its date, accounts, amount and the firm; the client's own manual journal reaches the
    accountant once; journals from the accountant and journals posted from documents are
    never sent back."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    firm_je = await _post_journal(real_client, firm_tok, ADJUSTED, 125.0, "Accrue audit fee")
    own_je = await _post_journal(real_client, tok, ADJUSTED, 40.0, "Petty cash rent")

    sent: list = []
    _route_to_app(monkeypatch, sent)
    results = await _two_way(client_company, credential)
    assert all(r.ok for r in results.values()), {k: r.errors for k, r in results.items()}
    order = [path for _, path in sent]
    assert order.index("/accounting/peer/journals") < order.index("/docs/import/batch")  # fetch first

    received = _from_firm(await _journals(real_engine, client_company))
    [(local_id, state)] = received.items()
    assert state["ts"] == ADJUSTED and state["status"] == "posted"
    assert state["peer_origin"]["journal"] == firm_je
    assert {(l["account"], l["debit"], l["credit"]) for l in state["entries"]} == {
        ("6500", 125.0, 0.0), ("6200", 0.0, 125.0)}
    notices = [n for n in await _notices(real_engine, client_company) if "Firm Practice" in n[1]]
    assert len(notices) == 1, notices
    title, body, url = notices[0]
    assert ADJUSTED in body and "6500" in body and "6200" in body and "125.00" in body
    assert "Posted by Firm Practice" in body
    assert url == "/settings/connectors/celerp#from-accountant"

    firm_books = await _journals(real_engine, firm_company)
    up = [s for s in firm_books.values() if (s.get("peer_source") or {}).get("journal") == own_je]
    assert len(up) == 1 and up[0]["memo"] == "Petty cash rent" and up[0]["ts"] == ADJUSTED
    assert not [s for s in firm_books.values()
                if (s.get("peer_source") or {}).get("journal") in (local_id,)]  # no echo
    assert not [s for s in firm_books.values()
                if s.get("peer_source") and s.get("je_type") != "manual"]  # no system postings

    sent.clear()
    again = await _two_way(client_company, credential)
    assert all(r.ok for r in again.values())
    assert _from_firm(await _journals(real_engine, client_company)).keys() == {local_id}
    assert len([n for n in await _notices(real_engine, client_company) if "Firm Practice" in n[1]]) == 1
    firm_books = await _journals(real_engine, firm_company)
    assert len([s for s in firm_books.values()
                if (s.get("peer_source") or {}).get("journal") == own_je]) == 1


async def test_firm_journal_in_client_locked_period_is_declined(real_client, real_engine, monkeypatch):
    """AC24: not applied; the client is told which journal and why; the firm view lists it."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _post_journal(real_client, firm_tok, ADJUSTED, 80.0, "Year-end accrual")
    await _lock(real_client, tok, "2026-06-30")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)

    assert _from_firm(await _journals(real_engine, client_company)) == {}
    notices = [n for n in await _notices(real_engine, client_company) if "Year-end accrual" in n[1]]
    assert len(notices) == 1 and "2026-06-30" in notices[0][1], notices
    declined = [i for i in await _attention(real_client, firm_tok) if i["kind"] == "declined"]
    assert len(declined) == 1 and declined[0]["label"] == "Year-end accrual"
    assert "2026-06-30" in declined[0]["reason"]

    await _two_way(client_company, credential)  # told once
    assert len([n for n in await _notices(real_engine, client_company)
                if "Year-end accrual" in n[1]]) == 1


async def _received(real_client, tok: str) -> list[dict]:
    r = await real_client.get("/accounting/journal-entries/from-accountant", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_reverse_dates_follow_both_locks(real_client, real_engine, monkeypatch):
    """AC27: the original date when open on both sides; today when locked on either side;
    refused, posting nothing, when today is locked on either side."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _post_journal(real_client, firm_tok, ADJUSTED, 10.0, "Open both sides")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [row] = await _received(real_client, tok)
    assert row["reverse_on"] == ADJUSTED  # the confirm names the date
    r = await real_client.post(f"/accounting/journal-entries/{row['id']}/reverse", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["ts"] == ADJUSTED
    again = await real_client.post(f"/accounting/journal-entries/{row['id']}/reverse", headers=auth(tok))
    assert again.json()["je_id"] == r.json()["je_id"]  # one reversal per journal

    await _post_journal(real_client, firm_tok, ADJUSTED, 20.0, "Firm closed March")
    await _lock(real_client, firm_tok, "2026-06-30")
    await _two_way(client_company, credential)
    row = next(i for i in await _received(real_client, tok) if i["memo"] == "Firm closed March")
    today = (await _today(real_engine, client_company)).isoformat()
    assert row["reverse_on"] == today
    r = await real_client.post(f"/accounting/journal-entries/{row['id']}/reverse", headers=auth(tok))
    assert r.status_code == 200 and r.json()["ts"] == today, r.text
    notices = [n for n in await _notices(real_engine, client_company) if "Firm closed March" in n[1]]
    assert any(today in body for _, body, _ in notices), notices

    await _post_journal(real_client, firm_tok, today, 30.0, "Dated today")
    await _two_way(client_company, credential)
    received = await _received(real_client, tok)
    assert received[0]["memo"] == "Dated today"  # newest first
    row = received[0]
    await _lock(real_client, tok, today)
    before = await _journals(real_engine, client_company)
    r = await real_client.post(f"/accounting/journal-entries/{row['id']}/reverse", headers=auth(tok))
    assert r.status_code == 422 and today in r.text, r.text
    assert await _journals(real_engine, client_company) == before


async def test_client_reversal_reaches_the_firm_view(real_client, real_engine, monkeypatch):
    """AC28 / J14: the firm view names the reversal with both dates and offers Post again,
    which prefills a new journal and posts nothing; Undo on the client clears it."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    firm_je = await _post_journal(real_client, firm_tok, ADJUSTED, 55.0, "Reclass fees")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    [row] = await _received(real_client, tok)
    reversal = await real_client.post(f"/accounting/journal-entries/{row['id']}/reverse", headers=auth(tok))
    assert reversal.status_code == 200, reversal.text
    await _two_way(client_company, credential)

    [item] = [i for i in await _attention(real_client, firm_tok) if i["kind"] == "reversal"]
    assert item["reason"] == f"Demo Trading reversed Reclass fees dated {ADJUSTED}; posted {ADJUSTED}"
    assert item["post_again"] == f"/accounting/journal/new?from={firm_je}"
    firm_books = await _journals(real_engine, firm_company)
    assert firm_books[firm_je]["status"] == "posted"  # never voided
    mirrored = [s for s in firm_books.values() if s.get("reverses") == firm_je]
    assert len(mirrored) == 1
    assert {(l["account"], l["debit"], l["credit"]) for l in mirrored[0]["entries"]} == {
        ("6500", 0.0, 55.0), ("6200", 55.0, 0.0)}

    count = len(firm_books)
    page = await real_client.get(f"/accounting/journal-entries/{firm_je}/prefill", headers=auth(firm_tok))
    assert page.status_code == 200, page.text
    prefill = page.json()
    assert prefill["memo"] == "Reclass fees" and prefill["ts"] >= (await _today(real_engine, firm_company)).isoformat()
    assert len(await _journals(real_engine, firm_company)) == count  # posts nothing

    undo = await real_client.post(
        f"/accounting/journal-entries/{reversal.json()['je_id']}/reverse", headers=auth(tok))
    assert undo.status_code == 200, undo.text
    await _two_way(client_company, credential)
    assert not [i for i in await _attention(real_client, firm_tok) if i["kind"] == "reversal"]


async def test_firm_lock_applies_forward_once(real_client, real_engine, monkeypatch):
    """J15: the firm's lock date moves the client's forward once with a notice; the client
    may move it back and the same date is not applied again; an earlier firm date never
    moves a later client date back; the firm's date is kept beside the client's own."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    _route_to_app(monkeypatch, [])
    await _lock(real_client, firm_tok, "2026-03-31")
    await _two_way(client_company, credential)
    lock = (await real_client.get("/accounting/period-lock", headers=auth(tok))).json()
    assert lock["lock_date"] == "2026-03-31"
    assert lock["firm_lock"] == {"date": "2026-03-31", "firm": "Firm Practice"}
    assert len([n for n in await _notices(real_engine, client_company)
                if "2026-03-31" in n[1] and "Firm Practice" in n[1]]) == 1

    await _lock(real_client, tok, "2026-01-31")  # the client moves it back
    await _two_way(client_company, credential)
    assert (await real_client.get("/accounting/period-lock", headers=auth(tok))).json()["lock_date"] == "2026-01-31"

    await _lock(real_client, tok, "2026-09-30")
    await _lock(real_client, firm_tok, "2026-06-30")
    await _two_way(client_company, credential)
    lock = (await real_client.get("/accounting/period-lock", headers=auth(tok))).json()
    assert lock["lock_date"] == "2026-09-30" and lock["firm_lock"]["date"] == "2026-06-30"


async def test_firm_cannot_void_what_the_client_sent(real_client, real_engine, monkeypatch):
    """AC4: a journal or document that arrived through sync cannot be voided or edited by
    a firm user; the refusal says to post a journal entry instead."""
    tok, firm_tok, client_company, credential, contacts = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    own_je = await _post_journal(real_client, tok, ADJUSTED, 15.0, "Client journal")
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    arrived = next(eid for eid, s in (await _journals(real_engine, firm_company)).items()
                   if (s.get("peer_source") or {}).get("journal") == own_je)
    r = await real_client.post(f"/accounting/journal-entries/{arrived}/void", json={}, headers=auth(firm_tok))
    assert r.status_code == 422 and "journal entry" in r.json()["detail"].lower(), r.text

    invoice = await _locked_invoice(real_client, real_engine, tok, firm_tok, client_company, contacts)
    await _lock(real_client, firm_tok, None)
    await _two_way(client_company, credential)
    docs = (await real_client.get("/docs", params={"doc_type": "invoice", "limit": 200},
                                  headers=auth(firm_tok))).json()["items"]
    synced = next(d for d in docs if d.get("ref_id") == invoice["ref_id"])
    doc_id = synced.get("entity_id") or synced["id"]
    r = await real_client.patch(f"/docs/{doc_id}", json={"fields_changed": {"notes": {"new": "firm edit"}}},
                                headers=auth(firm_tok))
    assert r.status_code == 422 and "journal entry" in r.json()["detail"].lower(), r.text
    r = await real_client.post(f"/docs/{doc_id}/void", json={}, headers=auth(firm_tok))
    assert r.status_code == 422 and "journal entry" in r.json()["detail"].lower(), r.text


async def test_lock_refusal_told_once_and_not_resent(real_client, real_engine, monkeypatch):
    """AC29: a document the firm's lock refused gives the client one notice naming the
    lock date and is not sent again until it changes; a change retries it once."""
    tok, firm_tok, client_company, credential, contacts = await _pair_journals(real_client, real_engine)
    invoice = await _locked_invoice(real_client, real_engine, tok, firm_tok, client_company, contacts)
    today = (await _today(real_engine, client_company)).isoformat()
    sent: list = []
    _route_to_app(monkeypatch, sent)
    await _two_way(client_company, credential)
    told = [n for n in await _notices(real_engine, client_company) if invoice["ref_id"] in n[1]]
    assert len(told) == 1 and today in told[0][1], told

    sent.clear()
    await _two_way(client_company, credential)
    assert ("POST", "/docs/import/batch") not in sent
    assert len([n for n in await _notices(real_engine, client_company) if invoice["ref_id"] in n[1]]) == 1

    yesterday = (await _today(real_engine, client_company) - timedelta(days=1)).isoformat()
    await _lock(real_client, firm_tok, yesterday)
    sent.clear()
    await _two_way(client_company, credential)
    assert sent.count(("POST", "/docs/import/batch")) == 1
    sent.clear()
    await _two_way(client_company, credential)
    assert ("POST", "/docs/import/batch") not in sent
