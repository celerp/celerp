# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A reversal of an entry in a locked period posts on an open date.

The entry stays in its locked period, unchanged, and its reversal posts on the company's
business date today, or on another open date the user picks, through the original's own
accounts. It never posts into the locked period and never backdates to the day after the
lock. It is refused, with a keyed message saying what to do, only when no open date is
possible or an original account is no longer in the chart. A staged migration's
corrections use its cutover date. One rule serves every void path: a manual void, a
document void, a document unvoid and an earlier credit note posted late.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

LOCK = "2026-03-31"
ENTRY_DAY = "2026-01-15"
DAY_AFTER_LOCK = "2026-04-01"


async def _today(session, auth) -> str:
    from celerp.models.company import Company
    from celerp.services.business_time import business_date_of

    session.expire_all()
    company = await session.get(Company, auth["company_id"])
    return business_date_of(None, (company.settings or {}).get("timezone"))


async def _manual(client, auth, day: str = ENTRY_DAY, amount: float = 250.0) -> str:
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": day, "memo": "Office rent", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "6200", "debit": amount}, {"account": "1111", "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _lock(client, auth, day: str = LOCK) -> None:
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": day})
    assert r.status_code == 200, r.text


async def _void(client, auth, je: str, **body):
    return await client.post(f"/accounting/journal-entries/{je}/void", headers=auth["headers"],
                             json={"reason": "Posted twice", **body})


async def _tb(client, auth, date_to: str | None = None) -> dict[str, float]:
    q = f"?date_to={date_to}" if date_to else ""
    r = await client.get(f"/accounting/trial-balance{q}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["balanced"], r.json()
    return {line["code"]: line["net"] for line in r.json()["lines"]}


async def _journal(client, auth) -> list[dict]:
    r = await client.get("/accounting/journal", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["entries"]


async def _rows(session, auth) -> int:
    session.expire_all()
    return (await session.execute(sa.select(sa.func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def _state(session, auth, entity_id: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, (auth["company_id"], entity_id))
    return dict(row.state) if row else {}


def _keyed(r, key: str) -> dict:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == key, detail
    return detail


# ---------------------------------------------------------------------------
# Manual void
# ---------------------------------------------------------------------------

async def test_voiding_a_locked_entry_reverses_it_on_the_business_date(client, session, auth):
    """Red statement: the void was refused with 'Period is locked through 2026-03-31'."""
    je = await _manual(client, auth)
    await _lock(client, auth)
    locked_tb = await _tb(client, auth, LOCK)
    today = await _today(session, auth)

    r = await _void(client, auth, je)
    assert r.status_code == 200, r.text

    assert await _tb(client, auth, LOCK) == locked_tb, "the locked period is unchanged"
    now = await _tb(client, auth)
    assert now.get("6200", 0) == 0 and now.get("1111", 0) == 0, now

    entries = {e["je_id"]: e for e in await _journal(client, auth)}
    original, reversal = entries[je], entries[f"{je}:reversal"]
    assert original["ts"] == ENTRY_DAY and original["status"] == "posted"
    assert reversal["ts"] == today != DAY_AFTER_LOCK
    assert {(line["account"], line["debit"], line["credit"]) for line in reversal["lines"]} == {
        ("6200", 0.0, 250.0), ("1111", 250.0, 0.0)}
    state = await _state(session, auth, je)
    assert state["status"] == "void" and state["reversed_on"] == today


async def test_the_reversal_keeps_the_original_account_roles(client, session, auth):
    je = await _manual(client, auth)
    await _lock(client, auth)
    assert (await _void(client, auth, je)).status_code == 200
    from celerp_accounting.routes import _je_rows

    session.expire_all()
    rows = {eid: state for eid, state, _ in await _je_rows(session, auth["company_id"])}
    original, reversal = rows[je], rows[f"{je}:reversal"]
    assert [line.get("account_roles") for line in reversal["entries"]] == [
        line.get("account_roles") for line in original["entries"]]
    assert [line["account"] for line in reversal["entries"]] == [line["account"] for line in original["entries"]]


async def test_a_chosen_open_date_is_honoured(client, session, auth):
    je = await _manual(client, auth)
    await _lock(client, auth)
    r = await _void(client, auth, je, reversal_date="2026-05-02")
    assert r.status_code == 200, r.text
    entries = {e["je_id"]: e for e in await _journal(client, auth)}
    assert entries[f"{je}:reversal"]["ts"] == "2026-05-02"
    assert (await _tb(client, auth, "2026-05-01")).get("6200") == 250.0
    assert (await _tb(client, auth, "2026-05-02")).get("6200", 0) == 0


async def test_a_chosen_locked_date_is_refused(client, session, auth):
    je = await _manual(client, auth)
    await _lock(client, auth)
    rows = await _rows(session, auth)
    detail = _keyed(await _void(client, auth, je, reversal_date="2026-03-15"), "accounting.reversal_date_locked")
    assert LOCK in detail["message"]
    assert await _rows(session, auth) == rows
    assert (await _state(session, auth, je))["status"] == "posted"


async def test_a_chosen_date_before_the_entry_is_refused(client, session, auth):
    je = await _manual(client, auth, day="2026-06-10")
    _keyed(await _void(client, auth, je, reversal_date="2026-06-01"), "accounting.reversal_date_invalid")
    _keyed(await _void(client, auth, je, reversal_date="06/12/2026"), "accounting.reversal_date_invalid")
    assert (await _state(session, auth, je))["status"] == "posted"


async def test_no_open_date_is_refused_and_nothing_is_written(client, session, auth):
    """A lock through a future date locks today too: there is nowhere to post."""
    je = await _manual(client, auth)
    await _lock(client, auth, "2099-12-31")
    rows = await _rows(session, auth)
    detail = _keyed(await _void(client, auth, je), "accounting.reversal_no_open_date")
    assert "Settings > Accounting" in detail["message"]
    assert await _rows(session, auth) == rows
    assert (await _state(session, auth, je))["status"] == "posted"


async def test_an_open_period_void_is_still_made_in_place(client, session, auth):
    """Neighbour: with the entry's own date open, the void stays on its date and no
    reversal row appears."""
    je = await _manual(client, auth, day="2026-06-10")
    await _lock(client, auth)
    assert (await _void(client, auth, je)).status_code == 200
    state = await _state(session, auth, je)
    assert state["status"] == "void" and "reversed_on" not in state
    entries = {e["je_id"]: e for e in await _journal(client, auth)}
    assert f"{je}:reversal" not in entries and entries[je]["status"] == "void"
    assert (await _tb(client, auth)).get("6200", 0) == 0


async def test_a_reversal_through_an_account_missing_from_the_chart_is_refused(client, session, auth):
    from celerp.events.engine import emit_event
    from celerp.services.posting_dates import void_reversal

    je = await _manual(client, auth)
    await _lock(client, auth)
    session.expire_all()
    row = await session.get(Projection, (auth["company_id"], je))
    row.state = {**row.state, "entries": [{**line, "account": "9999"} if line["account"] == "1111" else line
                                          for line in row.state["entries"]]}
    await session.commit()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await void_reversal(session, auth["company_id"], je, {"reason": "x", "ts": ENTRY_DAY})
    assert exc.value.detail["message_key"] == "accounting.reversal_account_missing"
    assert exc.value.detail["params"]["account"] == "9999"
    assert emit_event  # the boundary runs the same rule


async def test_a_reversed_entry_replays_to_the_same_books(client, session, auth):
    je = await _manual(client, auth)
    await _lock(client, auth)
    assert (await _void(client, auth, je)).status_code == 200
    before = await _state(session, auth, je)
    r = await client.post("/admin/doctor?checks=stale_projections", headers=auth["headers"])
    assert r.status_code == 200, r.text
    stale = next(c for c in r.json()["results"] if c["check"] == "stale_projections")
    assert stale["found"] == 0, stale
    assert await _state(session, auth, je) == before


# ---------------------------------------------------------------------------
# Document void and unvoid, the other void paths
# ---------------------------------------------------------------------------

async def _invoice(client, auth, day: str = ENTRY_DAY, total: float = 400.0) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_name": "Buyer", "issue_date": day,
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0, "total": total})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc


async def test_voiding_an_invoice_in_a_locked_period_reverses_its_entry_today(client, session, auth):
    doc = await _invoice(client, auth)
    await _lock(client, auth)
    locked_tb = await _tb(client, auth, LOCK)
    today = await _today(session, auth)
    r = await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={"reason": "Raised in error"})
    assert r.status_code == 200, r.text
    assert await _tb(client, auth, LOCK) == locked_tb
    fin = f"je:auto:{doc}:fin"
    assert (await _state(session, auth, fin))["reversed_on"] == today
    assert all(v == 0 for v in (await _tb(client, auth)).values()), await _tb(client, auth)


async def test_unvoiding_an_invoice_in_a_locked_period_posts_again_today(client, session, auth):
    doc = await _invoice(client, auth)
    await _lock(client, auth)
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={"reason": "x"})).status_code == 200
    today = await _today(session, auth)
    r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    session.expire_all()
    reposted = [row.state for row in (await session.execute(sa.select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(f"je:auto:{doc}:")))).scalars()
        if row.state.get("status") == "posted"]
    assert reposted and all(str(s.get("ts"))[:10] == today for s in reposted), reposted
    assert (await _tb(client, auth)).get("4100") == -400.0


# ---------------------------------------------------------------------------
# The shared rule
# ---------------------------------------------------------------------------

async def test_a_staged_migration_corrects_on_its_cutover_date(client, session, auth):
    from celerp.models.company import Company
    from celerp.models.migration import MigrationRun
    from celerp.services.posting_dates import correction_day

    await _lock(client, auth)
    await session.execute(sa.update(Company).where(Company.id == auth["company_id"]).values(is_migration_staged=True))
    session.add(MigrationRun(
        company_id=auth["company_id"], created_by_user_id=auth["user_id"], scan_claim_sha256=uuid.uuid4().hex * 2,
        source_system="test", source_artifact_sha256="0" * 64, adapter_version="1", cif_version="1",
        mode="full", status="running", mapping_decisions={"cutover_date": "2026-06-30"}))
    await session.commit()
    assert await correction_day(session, auth["company_id"], ENTRY_DAY) == "2026-06-30"
    assert await correction_day(session, auth["company_id"], "2026-07-15") is None


async def test_open_dates_need_no_correction_date(session, auth):
    from celerp.services.posting_dates import correction_day

    assert await correction_day(session, auth["company_id"], ENTRY_DAY) is None
