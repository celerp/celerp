# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Doctor's duplicate journal entry check only reports. It never voids an entry, in
dry-run or fix mode.

An entry is one journal entry record. Writing it again (a restatement, an account
remap) replaces it, so it is never a duplicate of itself. A duplicate is a second live
entry recording the same posting of the same document; Doctor names both and says what
to check, and the user decides. Covered: original recognition, restatement, reversal,
reposting, account remap, imported history, a genuine duplicate and a repeated repair.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client) -> tuple[str, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "Doctor Co", "email": f"dup-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    me = (await client.get("/companies/me", headers=_h(tok))).json()
    return tok, uuid.UUID(me["id"])


async def _finalized_invoice(client, tok, total=500) -> str:
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0, "total": total})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    return doc


async def _doctor(client, tok, *, fix: bool) -> dict:
    r = await client.post(f"/admin/doctor?checks=duplicate_jes{'&fix=true' if fix else ''}", headers=_h(tok))
    assert r.status_code == 200, r.text
    return next(c for c in r.json()["results"] if c["check"] == "duplicate_jes")


async def _books(session, cid) -> tuple[int, dict[str, str]]:
    """The ledger row count and every journal entry's status: what a repair could change."""
    session.expire_all()
    rows = (await session.execute(sa.select(sa.func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid))).scalar_one()
    statuses = dict((await session.execute(sa.select(Projection.entity_id, Projection.state["status"].as_string()).where(
        Projection.company_id == cid, Projection.entity_type == "journal_entry"))).all())
    return rows, statuses


async def _voids(session, cid) -> int:
    session.expire_all()
    return (await session.execute(sa.select(sa.func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.event_type == "acc.journal_entry.voided",
        LedgerEntry.source == "doctor"))).scalar_one()


async def _restate(session, cid, je_id: str, *, account: str | None = None) -> None:
    """Write the entry again on its own record, as a rate true-up or an account remap
    does: the same entry, restated."""
    from celerp.events.engine import emit_event

    session.expire_all()
    je = await session.get(Projection, (cid, je_id))
    first = (await session.execute(sa.select(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.entity_id == je_id,
        LedgerEntry.event_type == "acc.journal_entry.created").order_by(LedgerEntry.id).limit(1))).scalar_one()
    entries = [dict(e) for e in je.state["entries"]]
    if account:
        # Moved to another account by hand: the line names no role, so it is classified
        # by the account it now sits on.
        entries[-1] = {k: v for k, v in entries[-1].items() if k != "account_roles"} | {"account": account}
    await emit_event(session, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                     event_type="acc.journal_entry.created",
                     data={**{k: v for k, v in je.state.items() if k not in ("status",)}, "entries": entries},
                     actor_id=None, location_id=None, source="auto_je",
                     idempotency_key=f"{je_id}:restated:{uuid.uuid4().hex[:6]}", metadata_=dict(first.metadata_ or {}))
    await session.commit()


async def _assert_clean_and_untouched(client, session, cid, tok) -> None:
    before = await _books(session, cid)
    for fix in (False, True):
        found = await _doctor(client, tok, fix=fix)
        assert found["found"] == 0, found
        assert found["fixed"] == 0
    assert await _books(session, cid) == before
    assert await _voids(session, cid) == 0


async def test_original_recognition_is_not_a_duplicate(client, session):
    tok, cid = await _owner(client)
    await _finalized_invoice(client, tok)
    await _assert_clean_and_untouched(client, session, cid, tok)


async def test_a_restated_entry_is_not_a_duplicate_and_stays_posted(client, session):
    """Red statement: the check grouped created events by entity id, so a restated entry
    read as its own duplicate and fix mode voided the live entry."""
    tok, cid = await _owner(client)
    doc = await _finalized_invoice(client, tok)
    je = f"je:auto:{doc}:fin"
    await _restate(session, cid, je)
    await _assert_clean_and_untouched(client, session, cid, tok)
    assert (await _books(session, cid))[1][je] == "posted"


async def test_an_account_remap_is_not_a_duplicate(client, session):
    tok, cid = await _owner(client)
    doc = await _finalized_invoice(client, tok)
    je = f"je:auto:{doc}:fin"
    await _restate(session, cid, je, account="4200")
    await _assert_clean_and_untouched(client, session, cid, tok)
    assert (await _books(session, cid))[1][je] == "posted"


async def test_the_opening_stock_entry_restated_is_not_a_duplicate(client, session):
    """The sample stock booked at registration, restated, stays on the books."""
    tok, cid = await _owner(client)
    r = await client.post("/companies/me/business-type", json={"vertical": "agricultural"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    _rows, statuses = await _books(session, cid)
    opening = [e for e, s in statuses.items() if "opening-stock" in e and s == "posted"]
    assert opening, statuses
    await _restate(session, cid, opening[0])
    await _assert_clean_and_untouched(client, session, cid, tok)
    assert (await _books(session, cid))[1][opening[0]] == "posted"


async def test_a_reversal_is_not_a_duplicate(client, session):
    tok, cid = await _owner(client)
    doc = await _finalized_invoice(client, tok)
    r = await client.post(f"/docs/{doc}/void", headers=_h(tok), json={"reason": "Raised in error"})
    assert r.status_code == 200, r.text
    await _assert_clean_and_untouched(client, session, cid, tok)


async def test_reposting_after_revert_is_not_a_duplicate(client, session):
    tok, cid = await _owner(client)
    doc = await _finalized_invoice(client, tok)
    r = await client.post(f"/docs/{doc}/revert-to-draft", headers=_h(tok), json={})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    await _assert_clean_and_untouched(client, session, cid, tok)


async def test_imported_history_is_not_a_duplicate(client, session):
    tok, cid = await _owner(client)
    records = [{"entity_id": f"doc:imp-{i}-{uuid.uuid4().hex[:6]}", "event_type": "doc.created",
                "data": {"doc_type": "invoice", "total": 300, "subtotal": 300, "tax": 0,
                         "status": "paid", "amount_paid": 300},
                "source": "import:test", "idempotency_key": f"imp-{i}-{uuid.uuid4().hex[:6]}"} for i in range(2)]
    r = await client.post("/docs/import/batch", headers=_h(tok), json={"records": records})
    assert r.status_code == 200 and r.json()["created"] == 2, r.text
    await _assert_clean_and_untouched(client, session, cid, tok)


async def test_a_genuine_duplicate_is_reported_and_never_voided(client, session):
    """A second live entry recording the same posting of the same document is named,
    with what to check, and fix mode leaves both entries as they are."""
    from celerp.events.engine import emit_event

    tok, cid = await _owner(client)
    doc = await _finalized_invoice(client, tok)
    je = f"je:auto:{doc}:fin"
    session.expire_all()
    state = (await session.get(Projection, (cid, je))).state
    meta = (await session.execute(sa.select(LedgerEntry.metadata_).where(
        LedgerEntry.company_id == cid, LedgerEntry.entity_id == je,
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().first()
    copy = f"je:legacy:{uuid.uuid4().hex[:8]}"
    await emit_event(session, company_id=cid, entity_id=copy, entity_type="journal_entry",
                     event_type="acc.journal_entry.created", data={**state, "status": "posted"},
                     actor_id=None, location_id=None, source="import:legacy",
                     idempotency_key=f"{copy}:c", metadata_=dict(meta or {}))
    await session.commit()

    before = await _books(session, cid)
    for fix in (False, True, True):
        found = await _doctor(client, tok, fix=fix)
        assert found["found"] == 1, found
        assert found["fixed"] == 0 and found["auto_fixable"] is False
        detail = found["details"][0]
        assert {detail["entry_id"], detail["duplicate_entry_id"]} == {je, copy}, detail
        assert detail["doc_id"] == doc
        assert "void" in detail["what_to_check"].lower()
    assert await _books(session, cid) == before
    assert await _voids(session, cid) == 0
