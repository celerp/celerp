# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One-time COGS backfill for finalized invoices missing their COGS journal entry."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text

from celerp.events.engine import emit_event
from celerp.migrations._data_reconcile import get_meta
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection
from celerp.services.auto_je import compute_doc_cogs
from celerp.services.cogs_backfill import COGS_BACKFILL_KEY, run_cogs_backfill


async def _clear_marker(session) -> None:
    """The backfill is once-per-database: any app boot against this DB sets the
    marker, so each test resets it to the unset state its scenario assumes."""
    conn = await session.connection()
    await conn.run_sync(lambda c: get_meta(c, COGS_BACKFILL_KEY))
    await session.execute(
        text("DELETE FROM instance_meta WHERE key = :k"), {"k": COGS_BACKFILL_KEY})


async def _marker_value(session) -> str | None:
    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, COGS_BACKFILL_KEY))


async def _seed_company(session, name: str = "CogsCo") -> uuid.UUID:
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name=name, slug=f"cogs-{company_id.hex[:8]}"))
    await session.flush()
    return company_id


def _seed_parcel(session, company_id, entity_id: str, *, cost_total=None,
                 quantity=0.0, cost_price=None) -> None:
    """A parcel projection as compute_doc_cogs reads it."""
    state: dict = {"quantity": quantity}
    if cost_total is not None:
        state["cost_total"] = cost_total
    if cost_price is not None:
        state["cost_price"] = cost_price
    session.add(Projection(
        company_id=company_id, entity_id=entity_id, entity_type="item",
        state=state, version=1, updated_at=datetime.now(timezone.utc)))


def _seed_doc(session, company_id, doc_id: str, *, line_items, doc_type="invoice",
              status="finalized", finalized_at=None, issue_date=None,
              revert_count=0, total=110.0, tax=10.0) -> None:
    """A doc projection in the shape the legacy population carries."""
    state: dict = {
        "doc_type": doc_type, "status": status, "line_items": line_items,
        "total": total, "tax": tax, "revert_count": revert_count,
    }
    if finalized_at:
        state["finalized_at"] = finalized_at
    if issue_date:
        state["issue_date"] = issue_date
    session.add(Projection(
        company_id=company_id, entity_id=doc_id, entity_type="doc",
        state=state, version=1, updated_at=datetime.now(timezone.utc)))


def _fin_entries(total=110.0, revenue=100.0, tax=10.0, cogs=None) -> list[dict]:
    entries = [
        {"account": "1120", "debit": total, "credit": 0.0},
        {"account": "4100", "debit": 0.0, "credit": revenue},
        {"account": "2120", "debit": 0.0, "credit": tax},
    ]
    if cogs:
        entries.append({"account": "5100", "debit": cogs, "credit": 0.0})
        entries.append({"account": "1130-P", "debit": 0.0, "credit": cogs})
    return entries


async def _emit_je(session, company_id, je_id: str, *, entries, ts=None,
                   memo="Auto JE", void=False) -> None:
    data: dict = {"memo": memo, "entries": entries}
    if ts:
        data["ts"] = ts
    await emit_event(
        session, company_id=company_id, entity_id=je_id,
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data=data, actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={})
    if void:
        await emit_event(
            session, company_id=company_id, entity_id=je_id,
            entity_type="journal_entry", event_type="acc.journal_entry.voided",
            data={"reason": "test", "ts": ts}, actor_id=None, location_id=None,
            source="test", idempotency_key=str(uuid.uuid4()), metadata_={})


async def _doc_jes(session, company_id, doc_id: str) -> dict[str, dict]:
    """entity_id -> state for every JE projection belonging to the doc."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
    ))).scalars().all()
    prefix = f"je:auto:{doc_id}:"
    return {r.entity_id: r.state for r in rows if r.entity_id.startswith(prefix)}


def _posted_5100_debits(jes: dict[str, dict]) -> list[float]:
    return [float(e.get("debit") or 0)
            for st in jes.values() if st.get("status") == "posted"
            for e in st.get("entries", [])
            if e.get("account") == "5100" and float(e.get("debit") or 0) > 0]


def _posted_ids(jes: dict[str, dict]) -> set[str]:
    return {eid for eid, st in jes.items() if st.get("status") == "posted"}


async def _rowcount(session) -> int:
    return (await session.execute(
        select(func.count()).select_from(LedgerEntry))).scalar()


async def _notifications(session, company_id) -> list[Notification]:
    return list((await session.execute(select(Notification).where(
        Notification.company_id == company_id))).scalars().all())


async def _lock_period(session, company_id, lock_date: str) -> None:
    company = await session.get(Company, company_id)
    company.settings = {**(company.settings or {}), "lock_date": lock_date}
    session.add(company)
    await session.flush()


@pytest.mark.asyncio
async def test_backfill_posts_cogs_for_unfulfilled_invoice(session):
    """A never-fulfilled old-style invoice gets exactly one posted cogs-backfill
    JE, amount matching compute_doc_cogs, dated to its finalize JE."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p1", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0001"
    lines = [{"quantity": 1, "item_id": "item:p1", "line_total": 100.0}]
    _seed_doc(session, company_id, doc_id, line_items=lines,
              finalized_at="2024-03-05", issue_date="2024-03-01")
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-03-02")
    await session.commit()

    result = await run_cogs_backfill(session)
    await session.commit()
    assert result["posted"] == 1

    expected = (await compute_doc_cogs(
        session, company_id, {"line_items": lines})).total
    assert expected > 0

    jes = await _doc_jes(session, company_id, doc_id)
    backfill = jes.get(f"je:auto:{doc_id}:cogs-backfill")
    assert backfill is not None, "backfill JE was not posted"
    assert backfill["status"] == "posted"
    assert backfill["ts"] == "2024-03-02", "ts must copy the finalize JE's ts"
    assert backfill["memo"] == f"Auto JE for {doc_id} COGS backfill"
    assert backfill["entries"] == [
        {"account": "5100", "debit": expected, "credit": 0.0},
        {"account": "1130-P", "debit": 0.0, "credit": expected},
    ]
    assert _posted_5100_debits(jes) == [expected]
    assert await _marker_value(session) == "done"


@pytest.mark.asyncio
async def test_backfill_skips_invoice_with_fulfill_cogs(session):
    """An old invoice whose COGS was posted at fulfillment keeps its exact
    posted-JE id set and ledger row count."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p2", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0002"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p2", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-03-02")
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fulfill",
                   entries=[{"account": "5100", "debit": 20.0, "credit": 0.0},
                            {"account": "1130-P", "debit": 0.0, "credit": 20.0}],
                   ts="2024-03-09")
    await session.commit()

    before_ids = _posted_ids(await _doc_jes(session, company_id, doc_id))
    before_rows = await _rowcount(session)

    await run_cogs_backfill(session)
    await session.commit()

    assert _posted_ids(await _doc_jes(session, company_id, doc_id)) == before_ids
    assert await _rowcount(session) == before_rows


@pytest.mark.asyncio
async def test_backfill_full_cogs_for_partially_fulfilled_invoice(session):
    """The old path posted COGS only at full fulfillment, so a partially
    fulfilled invoice has none anywhere and gets the full amount."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p3a", cost_total=40.0, quantity=2.0)
    _seed_parcel(session, company_id, "item:p3b", cost_price=15.0, quantity=2.0)
    doc_id = "doc:INV-0003"
    lines = [
        {"quantity": 1, "item_id": "item:p3a", "line_total": 50.0},
        {"quantity": 2, "item_id": "item:p3b", "line_total": 50.0},
    ]
    _seed_doc(session, company_id, doc_id, line_items=lines)
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-04-01")
    await session.commit()

    await run_cogs_backfill(session)
    await session.commit()

    expected = (await compute_doc_cogs(session, company_id, {"line_items": lines})).total
    assert expected == 50.0  # 40/2 * 1 + 15 * 2
    jes = await _doc_jes(session, company_id, doc_id)
    assert _posted_5100_debits(jes) == [expected]


@pytest.mark.asyncio
async def test_backfill_covers_prefix_unvoid_restored_invoice(session):
    """A doc restored by a pre-fix unvoid (voided fin JE, posted fin:unvoid JE
    with no COGS) gets exactly one backfill JE, dated to the fin:unvoid JE."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p4", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0004"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p4", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-04-01", void=True)
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin:unvoid",
                   entries=_fin_entries(), ts="2024-04-02")
    await session.commit()

    await run_cogs_backfill(session)
    await session.commit()

    jes = await _doc_jes(session, company_id, doc_id)
    backfill = jes.get(f"je:auto:{doc_id}:cogs-backfill")
    assert backfill is not None and backfill["status"] == "posted"
    assert backfill["ts"] == "2024-04-02"
    assert _posted_5100_debits(jes) == [20.0]


@pytest.mark.asyncio
async def test_backfill_skips_new_code_invoice(session):
    """An invoice whose finalize JE already carries the 5100 leg keeps its exact
    posted-JE id set and ledger row count."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p5", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0005"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p5", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(cogs=20.0), ts="2026-01-05")
    await session.commit()

    before_ids = _posted_ids(await _doc_jes(session, company_id, doc_id))
    before_rows = await _rowcount(session)

    await run_cogs_backfill(session)
    await session.commit()

    assert _posted_ids(await _doc_jes(session, company_id, doc_id)) == before_ids
    assert await _rowcount(session) == before_rows


@pytest.mark.asyncio
async def test_backfill_rerun_without_marker_posts_nothing_new(session):
    """With the marker wiped, a second run finds nothing to do: the universe
    predicate and the emit idempotency keys are each sufficient on their own."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p6", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0006"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p6", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-05-01")
    await session.commit()

    first = await run_cogs_backfill(session)
    await session.commit()
    assert first["posted"] == 1
    rows_after_first = await _rowcount(session)
    ids_after_first = _posted_ids(await _doc_jes(session, company_id, doc_id))

    await _clear_marker(session)
    second = await run_cogs_backfill(session)
    await session.commit()

    assert second["posted"] == 0
    assert await _rowcount(session) == rows_after_first
    assert _posted_ids(await _doc_jes(session, company_id, doc_id)) == ids_after_first


@pytest.mark.asyncio
async def test_backfill_defers_on_locked_period_keeps_marker_unset(session):
    """A doc dated inside a locked period posts nothing, raises nothing, is
    counted deferred, and leaves the marker unset for the next boot."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p7", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0007"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p7", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-03-02")
    await _lock_period(session, company_id, "2024-12-31")
    await session.commit()

    result = await run_cogs_backfill(session)
    await session.commit()

    assert result["deferred"] == 1
    jes = await _doc_jes(session, company_id, doc_id)
    assert f"je:auto:{doc_id}:cogs-backfill" not in jes
    assert await _marker_value(session) is None
    notifs = await _notifications(session, company_id)
    assert len(notifs) == 1
    assert "1 deferred: accounting period locked" in notifs[0].body


@pytest.mark.asyncio
async def test_backfill_zero_cost_doc_posts_nothing_and_counts(session):
    """A doc computing zero COGS posts no JE and is reported as a count, never
    a fabricated amount; zero-cost alone does not hold the marker open."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p8", cost_price=0.0, quantity=1.0)
    doc_id = "doc:INV-0008"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p8", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-05-01")
    await session.commit()

    result = await run_cogs_backfill(session)
    await session.commit()

    assert result["zero_cost"] == 1
    assert result["posted"] == 0
    jes = await _doc_jes(session, company_id, doc_id)
    assert f"je:auto:{doc_id}:cogs-backfill" not in jes
    assert await _marker_value(session) == "done"
    notifs = await _notifications(session, company_id)
    assert len(notifs) == 1
    assert "1 zero cost" in notifs[0].body


@pytest.mark.asyncio
async def test_backfill_ignores_non_invoice_doc_types(session):
    """bill, memo, consignment_in, and credit_note docs keep their exact
    posted-JE id sets even when they hold finalize-family JEs."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p9", cost_total=40.0, quantity=2.0)
    lines = [{"quantity": 1, "item_id": "item:p9", "line_total": 100.0}]

    _seed_doc(session, company_id, "doc:BILL-1", line_items=lines, doc_type="bill")
    await _emit_je(session, company_id, "je:auto:doc:BILL-1:bill",
                   entries=[{"account": "1130-P", "debit": 110.0, "credit": 0.0},
                            {"account": "2110", "debit": 0.0, "credit": 110.0}],
                   ts="2024-05-01")
    _seed_doc(session, company_id, "doc:MEMO-1", line_items=lines, doc_type="memo")
    _seed_doc(session, company_id, "doc:CI-1", line_items=lines,
              doc_type="consignment_in")
    _seed_doc(session, company_id, "doc:CN-1", line_items=lines,
              doc_type="credit_note")
    await _emit_je(session, company_id, "je:auto:doc:CN-1:fin",
                   entries=_fin_entries(), ts="2024-05-01")
    await session.commit()

    before = {d: _posted_ids(await _doc_jes(session, company_id, d))
              for d in ("doc:BILL-1", "doc:MEMO-1", "doc:CI-1", "doc:CN-1")}
    before_rows = await _rowcount(session)

    result = await run_cogs_backfill(session)
    await session.commit()

    assert result["posted"] == 0
    after = {d: _posted_ids(await _doc_jes(session, company_id, d))
             for d in ("doc:BILL-1", "doc:MEMO-1", "doc:CI-1", "doc:CN-1")}
    assert after == before
    assert await _rowcount(session) == before_rows


@pytest.mark.asyncio
async def test_backfill_notifies_per_company_with_counts(session):
    """One bell notification per affected company carrying the invoice count,
    total, and the zero-cost, deferred, and errored counts."""
    await _clear_marker(session)
    company_a = await _seed_company(session, "CogsA")
    company_b = await _seed_company(session, "CogsB")

    # Company A: one posted, one zero-cost, one deferred, one errored.
    _seed_parcel(session, company_a, "item:a1", cost_total=40.0, quantity=2.0)
    _seed_parcel(session, company_a, "item:a2", cost_price=0.0, quantity=1.0)
    _seed_doc(session, company_a, "doc:INV-A1",
              line_items=[{"quantity": 1, "item_id": "item:a1", "line_total": 100.0}])
    await _emit_je(session, company_a, "je:auto:doc:INV-A1:fin",
                   entries=_fin_entries(), ts="2026-01-15")
    _seed_doc(session, company_a, "doc:INV-A2",
              line_items=[{"quantity": 1, "item_id": "item:a2", "line_total": 100.0}])
    await _emit_je(session, company_a, "je:auto:doc:INV-A2:fin",
                   entries=_fin_entries(), ts="2026-01-16")
    _seed_doc(session, company_a, "doc:INV-A3",
              line_items=[{"quantity": 1, "item_id": "item:a1", "line_total": 100.0}])
    await _emit_je(session, company_a, "je:auto:doc:INV-A3:fin",
                   entries=_fin_entries(), ts="2024-06-01")
    _seed_doc(session, company_a, "doc:INV-A4",
              line_items=[{"quantity": "not-a-number", "item_id": "item:a1",
                           "line_total": 100.0}])
    await _emit_je(session, company_a, "je:auto:doc:INV-A4:fin",
                   entries=_fin_entries(), ts="2026-01-17")
    await _lock_period(session, company_a, "2024-12-31")

    # Company B: one posted only.
    _seed_parcel(session, company_b, "item:b1", cost_total=60.0, quantity=2.0)
    _seed_doc(session, company_b, "doc:INV-B1",
              line_items=[{"quantity": 1, "item_id": "item:b1", "line_total": 100.0}])
    await _emit_je(session, company_b, "je:auto:doc:INV-B1:fin",
                   entries=_fin_entries(), ts="2026-02-01")
    await session.commit()

    await run_cogs_backfill(session)
    await session.commit()

    notifs_a = await _notifications(session, company_a)
    assert len(notifs_a) == 1
    a = notifs_a[0]
    assert a.title == "Cost of goods posted for past invoices"
    assert "1 invoice, total 20.00." in a.body
    assert "1 zero cost" in a.body
    assert "1 deferred: accounting period locked" in a.body
    assert "1 could not be computed" in a.body
    assert a.action_url.startswith("/accounting?q=COGS%20backfill")

    notifs_b = await _notifications(session, company_b)
    assert len(notifs_b) == 1
    b = notifs_b[0]
    assert "1 invoice, total 30.00." in b.body
    assert b.action_url == "/accounting?q=COGS%20backfill&from=2026-02-01&to=2026-02-01"


@pytest.mark.asyncio
async def test_backfill_scopes_per_company(session):
    """Two companies each hold doc:INV-0001; only the company whose doc lacks
    COGS gets a backfill JE, so lookups must be keyed by company."""
    await _clear_marker(session)
    company_a = await _seed_company(session, "ScopeA")
    company_b = await _seed_company(session, "ScopeB")
    doc_id = "doc:INV-0001"
    for cid in (company_a, company_b):
        _seed_parcel(session, cid, "item:s1", cost_total=40.0, quantity=2.0)
        _seed_doc(session, cid, doc_id,
                  line_items=[{"quantity": 1, "item_id": "item:s1", "line_total": 100.0}])
        await _emit_je(session, cid, f"je:auto:{doc_id}:fin",
                       entries=_fin_entries(), ts="2024-05-01")
    await _emit_je(session, company_a, f"je:auto:{doc_id}:fulfill",
                   entries=[{"account": "5100", "debit": 20.0, "credit": 0.0},
                            {"account": "1130-P", "debit": 0.0, "credit": 20.0}],
                   ts="2024-05-02")
    await session.commit()

    result = await run_cogs_backfill(session)
    await session.commit()

    assert result["posted"] == 1
    jes_a = await _doc_jes(session, company_a, doc_id)
    jes_b = await _doc_jes(session, company_b, doc_id)
    assert f"je:auto:{doc_id}:cogs-backfill" not in jes_a
    backfill_b = jes_b.get(f"je:auto:{doc_id}:cogs-backfill")
    assert backfill_b is not None and backfill_b["status"] == "posted"


@pytest.mark.asyncio
async def test_backfill_second_boot_while_locked_does_not_duplicate_notification(session):
    """A still-locked period leaves the marker unset, so every boot rescans;
    the unread notice dedups on its stable title so exactly one accumulates."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p10", cost_total=40.0, quantity=2.0)
    doc_id = "doc:INV-0010"
    _seed_doc(session, company_id, doc_id,
              line_items=[{"quantity": 1, "item_id": "item:p10", "line_total": 100.0}])
    await _emit_je(session, company_id, f"je:auto:{doc_id}:fin",
                   entries=_fin_entries(), ts="2024-03-02")
    await _lock_period(session, company_id, "2024-12-31")
    await session.commit()

    first = await run_cogs_backfill(session)
    await session.commit()
    assert first["deferred"] == 1
    assert await _marker_value(session) is None

    second = await run_cogs_backfill(session)
    await session.commit()
    assert second["deferred"] == 1

    notifs = await _notifications(session, company_id)
    unread = [n for n in notifs if not n.read]
    assert len(unread) == 1
    assert unread[0].title == "Cost of goods posted for past invoices"


@pytest.mark.asyncio
async def test_backfill_malformed_doc_errors_and_continues(session):
    """Legacy state that raises inside the cost computation marks that doc
    errored; the sibling doc still gets its JE and the marker stays unset."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:p11", cost_total=40.0, quantity=2.0)
    _seed_doc(session, company_id, "doc:INV-0011",
              line_items=[{"quantity": "not-a-number", "item_id": "item:p11",
                           "line_total": 100.0}])
    await _emit_je(session, company_id, "je:auto:doc:INV-0011:fin",
                   entries=_fin_entries(), ts="2024-05-01")
    _seed_doc(session, company_id, "doc:INV-0012",
              line_items=[{"quantity": 1, "item_id": "item:p11", "line_total": 100.0}])
    await _emit_je(session, company_id, "je:auto:doc:INV-0012:fin",
                   entries=_fin_entries(), ts="2024-05-02")
    await session.commit()

    result = await run_cogs_backfill(session)
    await session.commit()

    assert result["errored"] == 1
    assert result["posted"] == 1
    jes_bad = await _doc_jes(session, company_id, "doc:INV-0011")
    assert "je:auto:doc:INV-0011:cogs-backfill" not in jes_bad
    jes_ok = await _doc_jes(session, company_id, "doc:INV-0012")
    backfill = jes_ok.get("je:auto:doc:INV-0012:cogs-backfill")
    assert backfill is not None and backfill["status"] == "posted"
    assert await _marker_value(session) is None


@pytest.mark.asyncio
async def test_backfill_ledger_invariant_and_idempotent_rowcount(session):
    """Seeded matrix: after one run every doc holding a posted finalize-family
    JE with a 4100 credit and computed COGS > 0 has exactly one posted 5100
    debit, every posted JE balances, and a second run changes nothing."""
    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:m1", cost_total=40.0, quantity=2.0)
    _seed_parcel(session, company_id, "item:m0", cost_price=0.0, quantity=1.0)
    lines = [{"quantity": 1, "item_id": "item:m1", "line_total": 100.0}]
    zero_lines = [{"quantity": 1, "item_id": "item:m0", "line_total": 100.0}]
    nonstock_lines = [{"quantity": 1, "line_total": 100.0}]

    # Never fulfilled: affected.
    _seed_doc(session, company_id, "doc:M-1", line_items=lines)
    await _emit_je(session, company_id, "je:auto:doc:M-1:fin",
                   entries=_fin_entries(), ts="2024-01-10")
    # Fulfilled under the old path: COGS already posted at fulfillment.
    _seed_doc(session, company_id, "doc:M-2", line_items=lines)
    await _emit_je(session, company_id, "je:auto:doc:M-2:fin",
                   entries=_fin_entries(), ts="2024-01-11")
    await _emit_je(session, company_id, "je:auto:doc:M-2:fulfill",
                   entries=[{"account": "5100", "debit": 20.0, "credit": 0.0},
                            {"account": "1130-P", "debit": 0.0, "credit": 20.0}],
                   ts="2024-01-12")
    # Partially fulfilled under the old path: no COGS anywhere.
    _seed_doc(session, company_id, "doc:M-3", line_items=lines)
    await _emit_je(session, company_id, "je:auto:doc:M-3:fin",
                   entries=_fin_entries(), ts="2024-01-13")
    # Voided: no posted finalize-family JE.
    _seed_doc(session, company_id, "doc:M-4", line_items=lines, status="void")
    await _emit_je(session, company_id, "je:auto:doc:M-4:fin",
                   entries=_fin_entries(), ts="2024-01-14", void=True)
    # Reverted to draft: finalize JE voided.
    _seed_doc(session, company_id, "doc:M-5", line_items=lines, status="draft",
              revert_count=1)
    await _emit_je(session, company_id, "je:auto:doc:M-5:fin",
                   entries=_fin_entries(), ts="2024-01-15", void=True)
    # Unvoided by pre-fix code: fin voided, fin:unvoid posted, no COGS.
    _seed_doc(session, company_id, "doc:M-6", line_items=lines)
    await _emit_je(session, company_id, "je:auto:doc:M-6:fin",
                   entries=_fin_entries(), ts="2024-01-16", void=True)
    await _emit_je(session, company_id, "je:auto:doc:M-6:fin:unvoid",
                   entries=_fin_entries(), ts="2024-01-17")
    # Zero cost and non-stock: counted, never posted.
    _seed_doc(session, company_id, "doc:M-7", line_items=zero_lines)
    await _emit_je(session, company_id, "je:auto:doc:M-7:fin",
                   entries=_fin_entries(), ts="2024-01-18")
    _seed_doc(session, company_id, "doc:M-8", line_items=nonstock_lines)
    await _emit_je(session, company_id, "je:auto:doc:M-8:fin",
                   entries=_fin_entries(), ts="2024-01-19")
    await session.commit()

    result = await run_cogs_backfill(session)
    await session.commit()
    assert result["posted"] == 3  # M-1, M-3, M-6
    assert result["zero_cost"] == 2  # M-7, M-8

    docs = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "doc"))).scalars().all()
    for doc in docs:
        jes = await _doc_jes(session, company_id, doc.entity_id)
        has_posted_revenue = any(
            st.get("status") == "posted"
            and any(e.get("account") == "4100" and float(e.get("credit") or 0) > 0
                    for e in st.get("entries", []))
            for st in jes.values())
        cogs = (await compute_doc_cogs(session, company_id, doc.state)).total
        debits = _posted_5100_debits(jes)
        if has_posted_revenue and cogs > 0:
            assert len(debits) == 1, f"{doc.entity_id}: expected exactly one 5100 debit, got {debits}"
        # Every posted JE balances.
        for eid, st in jes.items():
            if st.get("status") != "posted":
                continue
            entries = st.get("entries", [])
            total_debit = sum(float(e.get("debit") or 0) for e in entries)
            total_credit = sum(float(e.get("credit") or 0) for e in entries)
            assert abs(total_debit - total_credit) < 0.01, f"{eid} does not balance"

    rows_after_first = await _rowcount(session)
    second = await run_cogs_backfill(session)
    await session.commit()
    assert second == {"changed": False}
    assert await _rowcount(session) == rows_after_first


@pytest.mark.asyncio
async def test_backfill_skips_ambiguous_multi_lot_invoice(session, caplog):
    """A historical invoice whose line quantity exceeds its bound splittable lot
    cannot be costed from that lot alone (the remainder came from sibling lots at
    unknown costs), so the backfill posts nothing for it, counts it as skipped,
    says so in the notification, and warns in the log. The skip is terminal: the
    completion marker still sets and the next boot is a no-op."""
    import logging

    await _clear_marker(session)
    company_id = await _seed_company(session)
    _seed_parcel(session, company_id, "item:amb1", cost_total=40.0, quantity=2.0)
    _seed_parcel(session, company_id, "item:amb2", cost_total=40.0, quantity=2.0)
    doc_ok = "doc:INV-AMB1"
    doc_amb = "doc:INV-AMB2"
    _seed_doc(session, company_id, doc_ok,
              line_items=[{"quantity": 1, "item_id": "item:amb1", "line_total": 100.0}],
              finalized_at="2024-04-05")
    _seed_doc(session, company_id, doc_amb,
              line_items=[{"quantity": 5, "item_id": "item:amb2", "line_total": 500.0}],
              finalized_at="2024-04-06")
    await _emit_je(session, company_id, f"je:auto:{doc_ok}:fin",
                   entries=_fin_entries(), ts="2024-04-05")
    await _emit_je(session, company_id, f"je:auto:{doc_amb}:fin",
                   entries=_fin_entries(total=550.0, revenue=500.0, tax=50.0), ts="2024-04-06")
    await session.commit()

    with caplog.at_level(logging.WARNING, logger="celerp.services.cogs_backfill"):
        result = await run_cogs_backfill(session)
    await session.commit()

    assert result["posted"] == 1, f"only the unambiguous invoice may post, got {result}"
    assert result["skipped"] == 1, f"the ambiguous invoice must count as skipped, got {result}"

    jes_ok = await _doc_jes(session, company_id, doc_ok)
    assert _posted_5100_debits(jes_ok) == [20.0], (
        f"unambiguous invoice must get its 1*20 backfill, got {_posted_5100_debits(jes_ok)}")
    jes_amb = await _doc_jes(session, company_id, doc_amb)
    assert f"je:auto:{doc_amb}:cogs-backfill" not in jes_amb, (
        "ambiguous invoice must receive no backfill JE at all")

    assert any(doc_amb in rec.getMessage() for rec in caplog.records), (
        f"skip must be warned with the doc id, got {[r.getMessage() for r in caplog.records]}")

    notes = await _notifications(session, company_id)
    assert len(notes) == 1
    assert "1 skipped: cost spans multiple lots, post manually." in notes[0].body, (
        f"notification must carry the skip, got: {notes[0].body}")

    assert await _marker_value(session) == "done", (
        "ambiguous skips are terminal and must not hold the completion marker open")

    rows_after_first = await _rowcount(session)
    second = await run_cogs_backfill(session)
    await session.commit()
    assert second == {"changed": False}
    assert await _rowcount(session) == rows_after_first
