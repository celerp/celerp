# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Ledger doctor - audit and repair tool for data integrity.

Checks:
1. missing_jes       - Docs with no corresponding journal entries
2. duplicate_jes     - Multiple JEs for the same doc trigger (finalize/payment/receive)
3. ghost_events      - Multiple doc.created events for the same entity_id
4. orphan_projections - Projections with no backing ledger events
5. stale_projections  - Projection state diverges from replayed ledger events
6. unbalanced_jes    - Journal entries where debit != credit
7. zero_amount_jes   - JEs with all-zero entries (noise)

Usage:
    POST /admin/doctor              -> dry-run (report only)
    POST /admin/doctor?fix=true     -> apply repairs
    POST /admin/doctor?checks=missing_jes,unbalanced_jes  -> run specific checks only
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.je_keys import je_idempotency_key

router = APIRouter()

ALL_CHECKS = [
    "missing_jes",
    "duplicate_jes",
    "ghost_events",
    "orphan_projections",
    "stale_projections",
    "unbalanced_jes",
    "zero_amount_jes",
]


# --- Individual checks ---

async def _check_missing_jes(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find docs that should have JEs but don't."""
    docs = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
        )
    )).scalars().all()

    # Build set of existing JE idempotency keys for fast lookup
    existing_keys = set((await session.execute(
        select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "journal_entry",
        )
    )).scalars().all())

    missing = []
    fixed = 0

    for doc in docs:
        state = doc.state
        doc_type = state.get("doc_type", "")
        status = state.get("status", "")
        entity_id = doc.entity_id
        total = float(state.get("total", 0) or 0)

        if status in ("void", "draft", "converted", "expired") or total <= 0:
            continue

        if doc_type == "invoice":
            # Check finalization JE
            fin_key = je_idempotency_key(entity_id, "invoice.finalized", "c")
            if fin_key not in existing_keys:
                missing.append({"doc_id": entity_id, "trigger": "finalize", "total": total})
                if fix:
                    await _emit_finalize_je(session, company_id, user_id, entity_id, state)
                    existing_keys.add(fin_key)
                    fixed += 1

            # Check payment JE (payment keys are cumulative-paid scoped)
            amount_paid = float(state.get("amount_paid", 0) or 0)
            if amount_paid > 0:
                paid_key = str(int(round(amount_paid * 100)))
                pay_key = je_idempotency_key(entity_id, "invoice.paid", "c")
                if pay_key not in existing_keys:
                    missing.append({"doc_id": entity_id, "trigger": "payment", "amount": amount_paid})
                    if fix:
                        await _emit_payment_je(session, company_id, user_id, entity_id, amount_paid, state, cumulative_paid=amount_paid)
                        existing_keys.add(pay_key)
                        fixed += 1

        elif doc_type == "purchase_order" and status not in ("draft",):
            rcv_key = je_idempotency_key(entity_id, "po.received", "c")
            if rcv_key not in existing_keys:
                missing.append({"doc_id": entity_id, "trigger": "po_received", "total": total})
                if fix:
                    await _emit_po_received_je(session, company_id, user_id, entity_id, state)
                    existing_keys.add(rcv_key)
                    fixed += 1

    return {"check": "missing_jes", "found": len(missing), "fixed": fixed, "details": missing[:50]}


async def _check_duplicate_jes(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find docs with multiple JEs for the same trigger."""
    jes = (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "journal_entry",
            LedgerEntry.event_type == "acc.journal_entry.created",
        ).order_by(LedgerEntry.id.asc())
    )).scalars().all()

    # Group by source doc_id + operation type derived from entity_id.
    # Entity ids have canonical form: je:auto:{doc_id}:{op} where op is fin, pay:{cents}, rcv.
    # We use entity_id (not metadata trigger) so that fin+pay for the same doc don't collide.
    by_doc_op: dict[str, list[LedgerEntry]] = {}
    for je in jes:
        # entity_id format: je:auto:{doc_id}:{op}
        # Strip the je:auto: prefix to get doc_id+op, which is our grouping key.
        eid = je.entity_id
        if eid.startswith("je:auto:"):
            group_key = eid[len("je:auto:"):]  # e.g. "doc:INV-2026-0001:fin"
        else:
            # Fallback: use entity_id as-is (handles any legacy format)
            group_key = eid
        by_doc_op.setdefault(group_key, []).append(je)

    duplicates = []
    fixed = 0
    for key, entries in by_doc_op.items():
        if len(entries) <= 1:
            continue
        # Keep earliest, flag rest
        keep = entries[0]
        for dup in entries[1:]:
            duplicates.append({
                "doc_trigger": key,
                "keep_id": keep.id,
                "duplicate_id": dup.id,
                "duplicate_entity_id": dup.entity_id,
            })
            if fix:
                # Void the duplicate by emitting a void event
                await emit_event(
                    session, company_id=company_id, entity_id=dup.entity_id,
                    entity_type="journal_entry", event_type="acc.journal_entry.voided",
                    data={"reason": "Doctor: duplicate JE"},
                    actor_id=user_id, location_id=None, source="doctor",
                    idempotency_key=f"doctor:void:{dup.idempotency_key}",
                    metadata_={"voided_by": "doctor", "kept_id": keep.id},
                )
                fixed += 1

    return {"check": "duplicate_jes", "found": len(duplicates), "fixed": fixed, "details": duplicates[:50]}


async def _check_ghost_events(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find entity_ids with multiple doc.created events (different idempotency keys)."""
    rows = (await session.execute(
        select(LedgerEntry.entity_id, func.count(LedgerEntry.id).label("cnt")).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.event_type == "doc.created",
        ).group_by(LedgerEntry.entity_id).having(func.count(LedgerEntry.id) > 1)
    )).all()

    ghosts = [{"entity_id": r[0], "count": r[1]} for r in rows]
    # Ghost events are flagged only - auto-fix is dangerous (which is canonical?)
    return {"check": "ghost_events", "found": len(ghosts), "fixed": 0, "details": ghosts[:50],
            "note": "Ghost events require manual review - cannot auto-determine canonical record"}


async def _check_orphan_projections(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find projections with no backing ledger events."""
    projections = (await session.execute(
        select(Projection.entity_id, Projection.entity_type).where(
            Projection.company_id == company_id,
        )
    )).all()

    orphans = []
    fixed = 0
    for entity_id, entity_type in projections:
        count = (await session.execute(
            select(func.count(LedgerEntry.id)).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.entity_id == entity_id,
            )
        )).scalar()
        if count == 0:
            orphans.append({"entity_id": entity_id, "entity_type": entity_type})
            if fix:
                proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
                if proj:
                    await session.delete(proj)
                    fixed += 1

    return {"check": "orphan_projections", "found": len(orphans), "fixed": fixed, "details": orphans[:50]}


async def _check_stale_projections(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find projections whose state differs from what replaying ledger events would produce."""
    projections = (await session.execute(
        select(Projection).where(Projection.company_id == company_id)
    )).scalars().all()

    stale = []
    fixed = 0
    for proj in projections:
        events = (await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.entity_id == proj.entity_id,
            ).order_by(LedgerEntry.id.asc())
        )).scalars().all()

        if not events:
            continue  # Orphan - handled by orphan_projections check

        replayed: dict = {}
        for ev in events:
            replayed = ProjectionEngine._apply(replayed, ev.event_type, ev.data)

        # Compare key fields (ignore metadata like updated_at)
        if replayed != proj.state:
            diff_keys = [k for k in set(list(replayed.keys()) + list(proj.state.keys()))
                         if replayed.get(k) != proj.state.get(k)]
            stale.append({
                "entity_id": proj.entity_id,
                "entity_type": proj.entity_type,
                "diff_keys": diff_keys[:10],
            })
            if fix:
                proj.state = replayed
                fixed += 1

    return {"check": "stale_projections", "found": len(stale), "fixed": fixed, "details": stale[:50]}


async def _check_unbalanced_jes(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find journal entries where sum(debit) != sum(credit)."""
    jes = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
        )
    )).scalars().all()

    unbalanced = []
    for je in jes:
        state = je.state
        if state.get("status") == "void":
            continue
        entries = state.get("entries", [])
        total_debit = sum(Decimal(str(e.get("debit", 0) or 0)) for e in entries)
        total_credit = sum(Decimal(str(e.get("credit", 0) or 0)) for e in entries)
        if abs(total_debit - total_credit) >= Decimal("0.01"):
            unbalanced.append({
                "entity_id": je.entity_id,
                "total_debit": float(total_debit),
                "total_credit": float(total_credit),
                "diff": float(total_debit - total_credit),
            })
    # Never auto-fix accounting data
    return {"check": "unbalanced_jes", "found": len(unbalanced), "fixed": 0, "details": unbalanced[:50],
            "note": "Unbalanced JEs require manual correction - will not auto-fix accounting data"}


async def _check_zero_amount_jes(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find journal entries where all entries are zero (noise)."""
    jes = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
        )
    )).scalars().all()

    zeros = []
    fixed = 0
    for je in jes:
        state = je.state
        if state.get("status") == "void":
            continue
        entries = state.get("entries", [])
        total = sum(abs(float(e.get("debit", 0) or 0)) + abs(float(e.get("credit", 0) or 0)) for e in entries)
        if total < 0.01:
            zeros.append({"entity_id": je.entity_id})
            if fix:
                # Find the ledger event and void it
                ledger_events = (await session.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.company_id == company_id,
                        LedgerEntry.entity_id == je.entity_id,
                        LedgerEntry.event_type == "acc.journal_entry.created",
                    )
                )).scalars().all()
                for ev in ledger_events:
                    await emit_event(
                        session, company_id=company_id, entity_id=je.entity_id,
                        entity_type="journal_entry", event_type="acc.journal_entry.voided",
                        data={"reason": "Doctor: zero-amount JE"},
                        actor_id=user_id, location_id=None, source="doctor",
                        idempotency_key=f"doctor:void-zero:{ev.idempotency_key}",
                        metadata_={"voided_by": "doctor"},
                    )
                    fixed += 1
                    break  # One void event per JE is enough

    return {"check": "zero_amount_jes", "found": len(zeros), "fixed": fixed, "details": zeros[:50]}


# --- JE emission helpers (shared with import hook) ---

async def _emit_finalize_je(
    session: AsyncSession, company_id, user_id, doc_id: str, state: dict,
) -> None:
    total = float(state.get("total", 0) or 0)
    subtotal = float(state.get("subtotal", total) or total)
    tax = float(state.get("tax", 0) or 0)
    ts = state.get("issue_date") or state.get("created_at") or state.get("finalized_at")

    je_id = f"je:auto:{doc_id}:fin"
    entries = [
        {"account": "1120", "debit": total, "credit": 0.0},
        {"account": "4100", "debit": 0.0, "credit": subtotal},
    ]
    if tax > 0:
        entries.append({"account": "2120", "debit": 0.0, "credit": tax})

    await emit_event(
        session, company_id=company_id, entity_id=je_id,
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data={"memo": f"Auto JE for {doc_id} finalized", "entries": entries, "ts": ts},
        actor_id=user_id, location_id=None, source="auto_je",
        idempotency_key=je_idempotency_key(doc_id, "invoice.finalized", "c"),
        metadata_={"trigger": "doc.finalized", "doc_id": doc_id},
    )
    await emit_event(
        session, company_id=company_id, entity_id=je_id,
        entity_type="journal_entry", event_type="acc.journal_entry.posted",
        data={}, actor_id=user_id, location_id=None, source="auto_je",
        idempotency_key=je_idempotency_key(doc_id, "invoice.finalized", "p"),
        metadata_={"trigger": "doc.finalized", "doc_id": doc_id},
    )


async def _emit_payment_je(
    session: AsyncSession,
    company_id,
    user_id,
    doc_id: str,
    amount: float,
    state: dict,
    *,
    cumulative_paid: float | None = None,
) -> None:
    ts = state.get("issue_date") or state.get("created_at")
    paid_key = str(int(round((cumulative_paid or amount) * 100)))
    je_id = f"je:auto:{doc_id}:pay:{paid_key}"

    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={
            "memo": f"Auto JE for {doc_id} payment",
            "entries": [
                {"account": "1110", "debit": amount, "credit": 0.0},
                {"account": "1120", "debit": 0.0, "credit": amount},
            ],
            "ts": ts,
        },
        actor_id=user_id,
        location_id=None,
        source="auto_je",
        idempotency_key=je_idempotency_key(doc_id, "invoice.paid", "c"),
        metadata_={
            "trigger": "doc.payment.received",
            "doc_id": doc_id,
            "payment_key": paid_key,
            "cumulative_paid": cumulative_paid,
        },
    )
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={},
        actor_id=user_id,
        location_id=None,
        source="auto_je",
        idempotency_key=je_idempotency_key(doc_id, "invoice.paid", "p"),
        metadata_={
            "trigger": "doc.payment.received",
            "doc_id": doc_id,
            "payment_key": paid_key,
            "cumulative_paid": cumulative_paid,
        },
    )


async def _emit_po_received_je(
    session: AsyncSession, company_id, user_id, doc_id: str, state: dict,
) -> None:
    total = float(state.get("total", 0) or 0)
    purchase_kind = str(state.get("purchase_kind") or "inventory").strip().lower()
    debit_account = {"inventory": "1130", "expense": "6950", "asset": "1210"}.get(purchase_kind, "1130")
    ts = state.get("issue_date") or state.get("created_at")
    je_id = f"je:auto:{doc_id}:rcv"

    await emit_event(
        session, company_id=company_id, entity_id=je_id,
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data={"memo": f"Auto JE for {doc_id} received", "entries": [
            {"account": debit_account, "debit": total, "credit": 0.0},
            {"account": "2110", "debit": 0.0, "credit": total},
        ], "ts": ts},
        actor_id=user_id, location_id=None, source="auto_je",
        idempotency_key=je_idempotency_key(doc_id, "po.received", "c"),
        metadata_={"trigger": "doc.received", "doc_id": doc_id, "purchase_kind": purchase_kind},
    )
    await emit_event(
        session, company_id=company_id, entity_id=je_id,
        entity_type="journal_entry", event_type="acc.journal_entry.posted",
        data={}, actor_id=user_id, location_id=None, source="auto_je",
        idempotency_key=je_idempotency_key(doc_id, "po.received", "p"),
        metadata_={"trigger": "doc.received", "doc_id": doc_id, "purchase_kind": purchase_kind},
    )


# --- Check dispatcher ---

_CHECK_FNS = {
    "missing_jes": _check_missing_jes,
    "duplicate_jes": _check_duplicate_jes,
    "ghost_events": _check_ghost_events,
    "orphan_projections": _check_orphan_projections,
    "stale_projections": _check_stale_projections,
    "unbalanced_jes": _check_unbalanced_jes,
    "zero_amount_jes": _check_zero_amount_jes,
}


@router.post("/doctor")
async def run_doctor(
    fix: bool = Query(False, description="Apply repairs (default: dry-run report only)"),
    checks: str | None = Query(None, description="Comma-separated check names (default: all)"),
    rebuild: bool = Query(False, description="Rebuild all projections after fixes"),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    check_names = [c.strip() for c in checks.split(",")] if checks else ALL_CHECKS
    invalid = [c for c in check_names if c not in _CHECK_FNS]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unknown checks: {invalid}")

    results = []
    for name in check_names:
        result = await _CHECK_FNS[name](session, company_id, user.id, fix=fix)
        results.append(result)

    if fix:
        await session.commit()

    if rebuild and fix:
        await ProjectionEngine.rebuild(session, company_id=company_id)
        await session.commit()

    total_found = sum(r["found"] for r in results)
    total_fixed = sum(r["fixed"] for r in results)

    return {
        "mode": "fix" if fix else "dry-run",
        "checks_run": check_names,
        "total_found": total_found,
        "total_fixed": total_fixed,
        "rebuilt": rebuild and fix,
        "results": results,
    }


@router.get("/relay/status")
async def relay_status(
    _user=Depends(get_current_user),
) -> dict:
    """Return the current Cloud Relay (cloudflared) status.

    Returns:
      {"status": "inactive" | "connecting" | "active" | "error",
       "gateway_connected": bool}
    """
    from celerp.gateway.client import get_client
    client = get_client()
    return {
        "status": client.relay_status if client else "inactive",
        "gateway_connected": client is not None,
    }
