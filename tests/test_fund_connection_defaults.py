# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A standing fund per connection: a local user who can manage funds sets the fund that
records from one connection take at their first arrival. A connected company's document
arrives with the default as its document choice, tagged with where it came from, and posts
as the user who set it; an accountant's journal posts on arrival with the default for every
line. The default never fills a record whose sender named a fund, a revision of a record
already here, or a file import, and once its setter can no longer choose funds it is shown
inactive and records wait in the hold again.

Initial conditions: the demo company paired with its accountant's copy of it
(test_peer_two_way_sync._pair_journals), the key also opening journals and documents; the
books receiving the records use funds and have a restricted fund."""

from __future__ import annotations

import uuid

import pytest

from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_fund_holds import (
    _LINES, DAY, _actor, _copy_of, _entries_of, _funds_client, _held, _journal, _revise, _sent_with,
    _state, _user_of,
)
from test_peer_sync_end_to_end import _company_of, _route_to_app
from test_peer_two_way_sync import _from_firm, _journals, _pair_journals, _two_way

pytestmark = pytest.mark.asyncio

BANK, EXPENSE = "1111", "6200"
ROUTE = "/accounting/funds/connection-defaults"


async def _defaults(real_client, tok: str) -> dict[tuple[str, str], dict]:
    r = await real_client.get(ROUTE, headers=auth(tok))
    assert r.status_code == 200, r.text
    return {(i["origin"], i["source_key"]): i for i in r.json()["items"]}


async def _set_default(real_client, tok: str, origin: str, source_key: str, value, *, version: int | None = None):
    if version is None:
        version = (await _defaults(real_client, tok)).get((origin, source_key), {}).get("version", 0)
    return await real_client.put(ROUTE, headers=auth(tok), json={
        "origin": origin, "source_key": source_key, "value": value, "expected_version": version,
        "request_key": uuid.uuid4().hex})


async def _member(real_client, tok: str, role: str) -> tuple[str, str]:
    """A second user of the books ``tok`` signs in to, with ``role``: (token, user id)."""
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    r = await real_client.post("/companies/me/users", headers=auth(tok), json={
        "name": role.title(), "email": email, "password": "testpass123", "role": role})
    assert r.status_code == 200, r.text
    user_id = r.json()["id"]
    r = await real_client.post("/auth/login", json={"email": email, "password": "testpass123"})
    assert r.status_code == 200, r.text
    member_tok = r.json()["access_token"]
    switched = await real_client.post(f"/auth/switch-company/{_company_of(tok)}", headers=auth(member_tok))
    return (switched.json()["access_token"] if switched.status_code == 200 else member_tok), user_id


async def _set_role(real_client, tok: str, user_id: str, role: str) -> None:
    r = await real_client.patch(f"/companies/me/users/{user_id}", headers=auth(tok), json={"role": role})
    assert r.status_code == 200, r.text


async def _client_doc(real_client, tok: str, *, issued: bool) -> str:
    vendor = await real_client.post("/crm/contacts", json={"name": "Utility Co", "contact_type": "vendor"},
                                    headers=auth(tok))
    assert vendor.status_code in (200, 201), vendor.text
    r = await real_client.post("/docs", headers=auth(tok), json={
        "doc_type": "bill", "contact_id": vendor.json()["id"], "date": DAY, "line_items": _LINES,
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code in (200, 201), r.text
    doc = r.json()["id"]
    if issued:
        assert (await real_client.post(f"/docs/{doc}/finalize", headers=auth(tok))).status_code == 200
    return doc


def _tag(origin: str, source_key: str) -> str:
    return f"default:{origin}:{source_key}"


async def test_connection_default_route_validates(real_client, real_engine, monkeypatch):
    """Without the fund permission, for a connection these books do not have, with a fund
    that is archived or another company's, or from a stale copy: refused naming why, and
    nothing is written. A user without the permission still reads the defaults."""
    tok, firm_tok, client_company, _, _ = await _pair_journals(real_client, real_engine)
    fund = await _funds_client(real_client, firm_tok)
    own = await _funds_client(real_client, tok)  # the client's fund: foreign to the firm
    key = ("connected_company", client_company)
    assert (await _defaults(real_client, firm_tok))[key]["fund_choice"] is None

    operator, _ = await _member(real_client, firm_tok, "operator")
    r = await _set_default(real_client, operator, *key, fund, version=0)
    assert r.status_code == 403, r.text
    assert key in await _defaults(real_client, operator)

    for origin, source in (("connected_company", str(uuid.uuid4())), ("accountant", client_company),
                           ("somewhere", client_company), ("accounting_system", "quickbooks")):
        r = await _set_default(real_client, firm_tok, origin, source, fund, version=0)
        assert r.status_code == 422 and r.json()["detail"]["message_key"] == "fund.connection_unknown", r.text

    r = await _set_default(real_client, firm_tok, *key, own, version=0)
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.fund.unknown", r.text
    archived = await real_client.post("/accounting/funds", headers=auth(firm_tok), json={
        "request_key": uuid.uuid4().hex, "name": "Closed appeal", "restricted": False, "purpose": None})
    archived = archived.json()["id"]
    assert (await real_client.post(f"/accounting/funds/{archived}/archive", headers=auth(firm_tok))).status_code == 200
    r = await _set_default(real_client, firm_tok, *key, archived, version=0)
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.fund.archived", r.text

    r = await _set_default(real_client, firm_tok, *key, fund, version=3)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version", r.text
    assert (await _defaults(real_client, firm_tok))[key]["fund_choice"] is None

    r = await _set_default(real_client, firm_tok, *key, fund, version=0)
    assert r.status_code == 200, r.text
    row = (await _defaults(real_client, firm_tok))[key]
    assert (row["fund_choice"], row["version"], row["active"]) == (fund, 1, True)
    assert row["set_by"] == str(await _user_of(firm_tok)) and row["set_at"]
    r = await _set_default(real_client, firm_tok, *key, "unrestricted", version=0)  # copy now stale
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version", r.text


async def test_connection_default_set_locally_and_tagged(real_client, real_engine, monkeypatch):
    """The firm sets a default for its client's documents: the client's next issued bill
    arrives with that fund as its document choice, tagged as the default for the client,
    and posts at once as the user who set it, every leg on the fund; nothing waits."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    assert (await _set_default(real_client, firm_tok, "connected_company", client_company, fund)).status_code == 200
    bill = await _client_doc(real_client, tok, issued=True)

    _route_to_app(monkeypatch, [])
    results = await _two_way(client_company, credential)
    assert results["documents_out"].ok, results["documents_out"].errors
    assert await _held(real_client, firm_tok, "doc_funds") == []
    copy = await _copy_of(real_engine, firm_company, bill)
    state = await _state(real_client, firm_tok, copy)
    assert (state["fund_choice"], state["fund_choice_source"]) == (fund, _tag("connected_company", client_company))
    [(je_id, entry)] = _entries_of(await _journals(real_engine, firm_company), copy)
    assert {e.get("fund_id") for e in entry["entries"] if e["account"] != "2111"} | {fund} == {fund}
    assert await _actor(real_engine, firm_company, je_id) == await _user_of(firm_tok)


async def test_connection_default_never_applies_to_sender_fund_or_revision(real_client, real_engine, monkeypatch):
    """A document whose sender names a fund is refused at arrival for that key and never
    filled; a draft filled at its first arrival whose sender then changes its lines loses
    the choice with the reason and is not filled again; a file import takes no default."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    assert (await _set_default(real_client, firm_tok, "connected_company", client_company, fund)).status_code == 200
    _route_to_app(monkeypatch, [])

    draft = await _client_doc(real_client, tok, issued=False)
    assert (await _two_way(client_company, credential))["documents_out"].ok
    copy = await _copy_of(real_engine, firm_company, draft)
    assert (await _state(real_client, firm_tok, copy))["fund_choice"] == fund
    await _revise(real_client, tok, draft, line_items=[{**_LINES[0], "unit_price": 70.0, "line_total": 70.0},
                                                       _LINES[1]])
    assert (await _two_way(client_company, credential))["documents_out"].ok
    state = await _state(real_client, firm_tok, copy)
    assert state.get("fund_choice") is None and state.get("fund_choice_source") is None
    assert state["fund_choice_reason"]

    with monkeypatch.context() as patched:
        _sent_with(patched, "fund_id", fund)
        sent = await _client_doc(real_client, tok, issued=False)
        results = await _two_way(client_company, credential)
    assert not results["documents_out"].ok
    assert await _copy_of(real_engine, firm_company, sent) is None

    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await real_client.post("/docs/import/batch", headers=auth(firm_tok), json={"records": [{
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "bill", "status": "draft", "ref_id": eid.split(":")[1], "total": 50.0,
                 "line_items": [_LINES[0]]}}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert (await _state(real_client, firm_tok, eid)).get("fund_choice") is None


async def test_connection_default_inactive_when_setter_loses_permission(real_client, real_engine, monkeypatch):
    """A manager sets the default and is then made an operator: the default reads inactive
    naming them, and the client's next issued bill waits in the hold unfilled."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    manager, manager_id = await _member(real_client, firm_tok, "manager")
    key = ("connected_company", client_company)
    assert (await _set_default(real_client, manager, *key, fund)).status_code == 200
    await _set_role(real_client, firm_tok, manager_id, "operator")
    row = (await _defaults(real_client, firm_tok))[key]
    assert (row["fund_choice"], row["active"], row["set_by"]) == (fund, False, manager_id)

    bill = await _client_doc(real_client, tok, issued=True)
    _route_to_app(monkeypatch, [])
    assert not (await _two_way(client_company, credential))["documents_out"].ok
    assert len(await _held(real_client, firm_tok, "doc_funds")) == 1
    assert await _copy_of(real_engine, firm_company, bill) is None


async def test_connection_default_cleared_keeps_filled_documents(real_client, real_engine, monkeypatch):
    """Clearing the default leaves a draft it filled with its choice, which a user can
    still change; the next document from the connection arrives with no choice."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, firm_tok)
    key = ("connected_company", client_company)
    assert (await _set_default(real_client, firm_tok, *key, fund)).status_code == 200
    _route_to_app(monkeypatch, [])
    first = await _client_doc(real_client, tok, issued=False)
    assert (await _two_way(client_company, credential))["documents_out"].ok
    copy = await _copy_of(real_engine, firm_company, first)

    r = await _set_default(real_client, firm_tok, *key, None)
    assert r.status_code == 200, r.text
    assert (await _defaults(real_client, firm_tok))[key]["fund_choice"] is None
    assert (await _state(real_client, firm_tok, copy))["fund_choice"] == fund
    version = (await _state(real_client, firm_tok, copy))["version"]
    r = await real_client.post(f"/docs/{copy}/fund-choices", headers=auth(firm_tok),
                               json={"expected_version": version, "document": "unrestricted"})
    assert r.status_code == 200, r.text
    state = await _state(real_client, firm_tok, copy)
    assert (state["fund_choice"], state["fund_choice_source"]) == ("unrestricted", "user")

    second = await _client_doc(real_client, tok, issued=False)
    assert (await _two_way(client_company, credential))["documents_out"].ok
    assert (await _state(real_client, firm_tok, await _copy_of(real_engine, firm_company, second))).get(
        "fund_choice") is None


async def test_firm_journal_takes_connection_default(real_client, real_engine, monkeypatch):
    """At the client, the accountant's journal waits for funds until a default is set for
    the accountant's connection; on the next sync it posts with the default on every line,
    tagged, as the user who set it. Once that user can no longer choose funds the next
    journal waits again."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    fund = await _funds_client(real_client, tok)
    lines = [(EXPENSE, 40.0, 0.0), (BANK, 0.0, 40.0)]
    await _journal(real_client, firm_tok, "Reclass program costs", lines)
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)
    assert len(await _held(real_client, tok)) == 1  # no default: held

    manager, manager_id = await _member(real_client, tok, "manager")
    assert (await _set_default(real_client, manager, "accountant", firm_company, fund)).status_code == 200
    await _two_way(client_company, credential)
    assert await _held(real_client, tok) == []
    [(je_id, entry)] = list(_from_firm(await _journals(real_engine, client_company)).items())
    assert {e.get("fund_id") for e in entry["entries"]} == {fund}
    assert {e.get("fund_choice") for e in entry["entries"]} == {fund}
    assert entry["fund_choice_source"] == _tag("accountant", firm_company)
    assert str(await _actor(real_engine, client_company, je_id)) == manager_id

    await _set_role(real_client, tok, manager_id, "operator")
    await _journal(real_client, firm_tok, "Second reclass", lines)
    await _two_way(client_company, credential)
    assert len(await _held(real_client, tok)) == 1
