# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A journal entry's effect is cancelled at most once: by voiding it, or by one reversal
that is itself still standing. Voiding an entry a standing reversal already cancels is
refused with what to do instead; reversing one that is void or already reversed is
refused; and when the accountant voids an entry the client has meanwhile reversed, the
client's reversal is voided with it, with a notice. Both companies' trial balances are
checked after every step, and a refusal changes nothing."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_sync_end_to_end import _company_of, _route_to_app
from test_peer_two_way_sync import (ADJUSTED, _journals, _notices, _pair_journals, _post_journal,
                                    _received, _two_way)

pytestmark = pytest.mark.asyncio


async def _net(client, tok: str) -> dict[str, Decimal]:
    """Debit less credit per account on the trial balance."""
    r = await client.get("/accounting/trial-balance", headers=auth(tok))
    assert r.status_code == 200, r.text
    return {line["code"]: Decimal(str(line["total_debit"])) - Decimal(str(line["total_credit"]))
            for line in r.json()["lines"]}


async def _ledger_rows(engine, company_id: str) -> int:
    from celerp.models.ledger import LedgerEntry
    async with maker(engine)() as s:
        return await s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == uuid.UUID(company_id)))


async def _books(client, engine, tok: str, firm_tok: str, client_company: str, firm_company: str):
    return (await _net(client, tok), await _net(client, firm_tok))


async def _start(real_client, real_engine, monkeypatch, amount: float = 100.0, memo: str = "Accrue audit fee"):
    """The firm posts a journal and the client takes it; both books before it, and its ids."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    _route_to_app(monkeypatch, [])
    await _two_way(client_company, credential)  # anything the copy left to settle
    before = await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company)
    firm_je = await _post_journal(real_client, firm_tok, ADJUSTED, amount, memo)
    await _two_way(client_company, credential)
    [row] = await _received(real_client, tok)
    return tok, firm_tok, client_company, firm_company, credential, firm_je, row["id"], before


def _moved(books, before) -> dict[str, Decimal]:
    """What the steps since ``before`` left on each account, per company."""
    return tuple({code: net - base.get(code, Decimal(0)) for code, net in now.items()
                  if net != base.get(code, Decimal(0))} for now, base in zip(books, before))


async def _reverse(real_client, tok: str, je_id: str):
    return await real_client.post(f"/accounting/journal-entries/{je_id}/reverse", headers=auth(tok))


async def _void(real_client, tok: str, je_id: str):
    return await real_client.post(f"/accounting/journal-entries/{je_id}/void", json={}, headers=auth(tok))


async def test_firm_cannot_void_an_entry_the_client_reversed(real_client, real_engine, monkeypatch):
    """Original, then the client's reversal reaches the firm, then the firm voids the
    original: refused with what to do instead, nothing written in either company, and
    every later sync changes nothing."""
    tok, firm_tok, client_company, firm_company, credential, firm_je, local, before = await _start(
        real_client, real_engine, monkeypatch)
    assert _moved(await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company), before) == (
        {"6500": Decimal(100), "6200": Decimal(-100)}, {"6500": Decimal(100), "6200": Decimal(-100)})
    assert (await _reverse(real_client, tok, local)).status_code == 200
    await _two_way(client_company, credential)
    settled = await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company)
    assert _moved(settled, before) == ({}, {})

    rows = await _ledger_rows(real_engine, firm_company)
    r = await _void(real_client, firm_tok, firm_je)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "Reversal of Accrue audit fee" in detail and ADJUSTED in detail and "undo" in detail.lower(), detail
    assert await _ledger_rows(real_engine, firm_company) == rows  # nothing written
    assert (await _journals(real_engine, firm_company))[firm_je]["status"] == "posted"
    assert await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company) == settled

    bulk = await real_client.post("/accounting/journal-entries/bulk-void", json={"je_ids": [firm_je]},
                                  headers=auth(firm_tok))
    assert bulk.status_code == 200, bulk.text
    assert (await _journals(real_engine, firm_company))[firm_je]["status"] == "posted"
    for _ in range(2):  # retries change nothing
        await _two_way(client_company, credential)
        assert await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company) == settled


async def test_firm_void_before_sync_takes_the_client_reversal_with_it(real_client, real_engine, monkeypatch):
    """The client reverses the firm's journal and, before that reaches the firm, the firm
    voids it: on the next sync the client's entry and its reversal are both voided, with a
    notice, and the firm never posts the reversal. Both books end where they began."""
    tok, firm_tok, client_company, firm_company, credential, firm_je, local, before = await _start(
        real_client, real_engine, monkeypatch)
    reversal = await _reverse(real_client, tok, local)
    assert reversal.status_code == 200, reversal.text
    assert (await _void(real_client, firm_tok, firm_je)).status_code == 200
    await _two_way(client_company, credential)

    client_books = await _journals(real_engine, client_company)
    assert client_books[local]["status"] == "void"
    assert client_books[reversal.json()["je_id"]]["status"] == "void"
    assert not [s for s in (await _journals(real_engine, firm_company)).values() if s.get("reverses") == firm_je]
    assert _moved(await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company), before) == ({}, {})
    told = [body for _, body, _ in await _notices(real_engine, client_company)
            if "Accrue audit fee" in body and "reversal" in body.lower() and "void" in body.lower()]
    assert len(told) == 1, told

    settled = await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company)
    rows = (await _ledger_rows(real_engine, client_company), await _ledger_rows(real_engine, firm_company))
    for _ in range(2):
        await _two_way(client_company, credential)
    assert await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company) == settled
    assert len([body for _, body, _ in await _notices(real_engine, client_company)
                if "Accrue audit fee" in body and "reversal" in body.lower() and "void" in body.lower()]) == 1
    assert (await _ledger_rows(real_engine, client_company), await _ledger_rows(real_engine, firm_company)) == rows


async def test_an_undone_reversal_lets_the_firm_void_and_cannot_be_undone_again(
        real_client, real_engine, monkeypatch):
    """The client reverses and then undoes its reversal; the firm may then void its
    journal, and the void reaches the client. Undoing the undo afterwards would cancel the
    voided entry a second time and is refused, posting nothing."""
    tok, firm_tok, client_company, firm_company, credential, firm_je, local, before = await _start(
        real_client, real_engine, monkeypatch)
    reversal = await _reverse(real_client, tok, local)
    undo = await _reverse(real_client, tok, reversal.json()["je_id"])
    assert undo.status_code == 200, undo.text
    await _two_way(client_company, credential)
    assert _moved(await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company), before) == (
        {"6500": Decimal(100), "6200": Decimal(-100)}, {"6500": Decimal(100), "6200": Decimal(-100)})

    assert (await _void(real_client, firm_tok, firm_je)).status_code == 200, "a standing entry may be voided"
    await _two_way(client_company, credential)
    assert (await _journals(real_engine, client_company))[local]["status"] == "void"
    settled = await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company)
    assert _moved(settled, before) == ({}, {})

    rows = await _ledger_rows(real_engine, client_company)
    again = await _reverse(real_client, tok, undo.json()["je_id"])
    assert again.status_code == 422, again.text
    assert await _ledger_rows(real_engine, client_company) == rows
    await _two_way(client_company, credential)
    assert await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company) == settled


async def test_a_voided_entry_cannot_be_reversed(real_client, real_engine):
    """Within one company: a void entry, or one already reversed, cannot be reversed by a
    reversal arriving from the client either; the firm refuses it and posts nothing."""
    from celerp.services import peer_journals

    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    firm_company = _company_of(firm_tok)
    firm_je = await _post_journal(real_client, firm_tok, ADJUSTED, 30.0, "Voided first")
    assert (await _void(real_client, firm_tok, firm_je)).status_code == 200
    from celerp.db import get_session_ctx
    from celerp.models.company_peer import CompanyPeerGrant
    async with get_session_ctx() as session:
        grant = await session.scalar(select(CompanyPeerGrant).where(
            CompanyPeerGrant.company_id == uuid.UUID(firm_company),
            CompanyPeerGrant.source_company_id == uuid.UUID(client_company)).limit(1))
        rows = await _ledger_rows(real_engine, firm_company)
        with pytest.raises(Exception) as refused:
            await peer_journals.receive_journal(session, grant=grant, source_entity_id="je:client-reversal",
                                                source_revision=1, data={
                "memo": "Reversal of Voided first", "ts": ADJUSTED, "status": "posted",
                "reverses": {"origin": firm_je},
                "entries": [{"account": "6500", "debit": 0.0, "credit": 30.0},
                            {"account": "6200", "debit": 30.0, "credit": 0.0}]})
        await session.rollback()
    assert getattr(refused.value, "status_code", None) == 422, refused.value
    assert "Voided first" in refused.value.detail and "Nothing was posted" in refused.value.detail
    assert await _ledger_rows(real_engine, firm_company) == rows


async def test_racing_void_and_reversal_cancel_once(real_client, real_engine, monkeypatch):
    """The firm voids its journal while the client's reversal of it is arriving: whichever
    lands first wins and the other is refused, so the firm's books end where they began,
    and after the next syncs the client's do too."""
    tok, firm_tok, client_company, firm_company, credential, firm_je, local, before = await _start(
        real_client, real_engine, monkeypatch)
    assert (await _reverse(real_client, tok, local)).status_code == 200
    await asyncio.gather(_two_way(client_company, credential), _void(real_client, firm_tok, firm_je),
                         return_exceptions=True)
    for _ in range(3):
        await _two_way(client_company, credential)
    assert _moved(await _books(real_client, real_engine, tok, firm_tok, client_company, firm_company), before) == ({}, {})
