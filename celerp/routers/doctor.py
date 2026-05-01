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

import json
import os
from datetime import datetime, timezone
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
    "legacy_item_prices",
    "inverted_doc_dates",
    "fractional_piece_quantities",
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
                        await _emit_payment_je(session, company_id, user_id, entity_id, amount_paid, state, payment_index=len(state.get("payments", [])) - 1 if state.get("payments") else 0)
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

    return {"check": "missing_jes", "found": len(missing), "fixed": fixed, "auto_fixable": True, "details": missing[:50]}


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

    return {"check": "duplicate_jes", "found": len(duplicates), "fixed": fixed, "auto_fixable": True, "details": duplicates[:50]}


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
    return {"check": "ghost_events", "found": len(ghosts), "fixed": 0, "auto_fixable": False, "details": ghosts[:50],
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

    return {"check": "orphan_projections", "found": len(orphans), "fixed": fixed, "auto_fixable": True, "details": orphans[:50]}


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

    return {"check": "stale_projections", "found": len(stale), "fixed": fixed, "auto_fixable": True, "details": stale[:50]}


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
    return {"check": "unbalanced_jes", "found": len(unbalanced), "fixed": 0, "auto_fixable": False, "details": unbalanced[:50],
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

    return {"check": "zero_amount_jes", "found": len(zeros), "fixed": fixed, "auto_fixable": True, "details": zeros[:50]}


async def _check_legacy_item_prices(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find items where prices were stored as price_type/new_price in event payload
    (old seeder format) instead of top-level cost_price/retail_price fields.

    These items show price_type/new_price at top-level in their projection state
    but have no cost_price or retail_price, so dashboard valuation shows 0.

    Fix: emit item.pricing.set events mapping new_price -> cost_price (best-effort
    recovery; retail_price cannot be recovered from this format).
    """
    items = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()

    affected = []
    fixed = 0
    for item in items:
        state = item.state
        price_type = state.get("price_type")
        new_price = state.get("new_price")
        if price_type and new_price is not None and state.get(price_type) is None:
            affected.append({"entity_id": item.entity_id, "price_type": price_type, "new_price": new_price})
            if fix:
                await emit_event(
                    session,
                    company_id=company_id,
                    entity_id=item.entity_id,
                    entity_type="item",
                    event_type="item.pricing.set",
                    data={"price_type": price_type, "new_price": float(new_price)},
                    actor_id=user_id,
                    location_id=None,
                    source="doctor",
                    idempotency_key=f"doctor:legacy-price:{item.entity_id}:{price_type}",
                )
                fixed += 1

    return {"check": "legacy_item_prices", "found": len(affected), "fixed": fixed, "auto_fixable": True, "details": affected[:50]}


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
    payment_index: int = 0,
) -> None:
    """Delegate to auto_je.create_for_doc_payment for DRY compliance.

    Doctor repair: uses the first recorded bank_account from state, falling back to
    "1111" (the default bank account that always exists) when original is unknown.
    This is the one legitimate fallback - historical repair where real bank is unknowable.
    """
    from celerp.services import auto_je as _auto_je
    bank_code = (state.get("payments") or [{}])[0].get("bank_account") or "1111"  # Doctor fallback: default bank always exists
    ts = state.get("issue_date") or state.get("created_at") or __import__("datetime").date.today().isoformat()
    await _auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=user_id, doc_id=doc_id,
        amount=amount, payment_index=payment_index,
        bank_account_code=bank_code,
        doc_type=state.get("doc_type", "invoice"),
        payment_date=ts,
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


async def _check_inverted_doc_dates(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find docs where due_date is set and is earlier than issue_date.

    These can occur from data imported before backend validation was added (v1.0.10).
    Fix: set due_date = issue_date (the user almost certainly only checked the
    issue date field, so this is the least-surprising repair).
    """
    docs = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
        )
    )).scalars().all()

    affected = []
    fixed = 0
    for doc in docs:
        state = doc.state
        issue = (state.get("issue_date") or "")[:10]
        due = (state.get("due_date") or "")[:10]
        if not (issue and due):
            continue
        if due >= issue:
            continue
        affected.append({
            "entity_id": doc.entity_id,
            "issue_date": issue,
            "due_date": due,
            "doc_type": state.get("doc_type"),
            "ref_id": state.get("ref_id"),
        })
        if fix:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=doc.entity_id,
                entity_type="doc",
                event_type="doc.updated",
                data={"fields_changed": {"due_date": {"old": due, "new": issue}}},
                actor_id=user_id,
                location_id=None,
                source="doctor",
                idempotency_key=f"doctor:inverted-due-date:{doc.entity_id}",
            )
            fixed += 1

    return {"check": "inverted_doc_dates", "found": len(affected), "fixed": fixed, "auto_fixable": True, "details": affected[:50]}


async def _check_fractional_piece_quantities(
    session: AsyncSession, company_id, user_id, *, fix: bool,
) -> dict:
    """Find inventory items and document line items with fractional quantities for
    units that require whole numbers (decimals=0, e.g. 'piece').

    These arise from fulfillment splits on invoices created before quantity precision
    validation was enforced (v1.0.10). This check is report-only (never auto-fixed)
    because the correct quantity is ambiguous for finalized/voided documents.

    Remediation guidance per finding:
      doc_line (draft)    -> edit the line item quantity before finalizing
      doc_line (final)    -> void and re-issue, or issue a credit note
      item                -> adjust item quantity via the inventory screen
    """
    from decimal import Decimal, ROUND_HALF_UP
    from celerp.services.units import DEFAULT_UNITS, build_unit_map

    # Build unit map (company-specific if configured, else defaults)
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    units = (company.settings or {}).get("units") if company else None
    unit_map = build_unit_map(units if units else DEFAULT_UNITS)

    # Only units with decimals=0 are affected
    zero_decimal_units: set[str] = {name for name, cfg in unit_map.items() if cfg.get("decimals", 0) == 0}

    def _has_fraction(qty: float) -> bool:
        d = Decimal(str(qty))
        return d != d.to_integral_value(rounding=ROUND_HALF_UP)

    # Build SKU -> sell_by map from item projections (authoritative source)
    items_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()

    sku_sell_by: dict[str, str] = {}
    findings: list[dict] = []

    # Check inventory items first
    for row in items_rows:
        state = row.state
        sell_by = state.get("sell_by") or ""
        sku = state.get("sku") or ""
        if sku and sell_by:
            sku_sell_by[sku] = sell_by
        if sell_by not in zero_decimal_units:
            continue
        qty = float(state.get("quantity") or 0)
        if not _has_fraction(qty):
            continue
        findings.append({
            "kind": "item",
            "item_id": row.entity_id,
            "sku": sku,
            "name": state.get("name") or sku,
            "quantity": qty,
            "sell_by": sell_by,
            "action_required": "adjust_item_quantity",
        })

    # Check document line items
    doc_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
        )
    )).scalars().all()

    for row in doc_rows:
        state = row.state
        doc_status = state.get("status") or ""
        for li in (state.get("line_items") or []):
            sku = li.get("sku") or ""
            sell_by = li.get("sell_by") or (sku_sell_by.get(sku) if sku else "") or ""
            if sell_by not in zero_decimal_units:
                continue
            qty = float(li.get("quantity") or 0)
            if not _has_fraction(qty):
                continue
            if doc_status in ("draft", "proforma"):
                action = "edit_line_item_quantity"
            elif doc_status in ("void",):
                action = "no_action_needed_doc_voided"
            else:
                action = "void_and_reissue_or_credit_note"
            findings.append({
                "kind": "doc_line",
                "doc_id": row.entity_id,
                "ref_id": state.get("ref_id"),
                "doc_type": state.get("doc_type"),
                "doc_status": doc_status,
                "sku": sku,
                "name": li.get("name") or li.get("description") or sku,
                "quantity": qty,
                "sell_by": sell_by,
                "action_required": action,
            })

    return {
        "check": "fractional_piece_quantities",
        "found": len(findings),
        "fixed": 0,
        "auto_fixable": False,
        "details": findings[:100],
    }


# --- Check dispatcher ---

_CHECK_FNS = {
    "missing_jes": _check_missing_jes,
    "duplicate_jes": _check_duplicate_jes,
    "ghost_events": _check_ghost_events,
    "orphan_projections": _check_orphan_projections,
    "stale_projections": _check_stale_projections,
    "unbalanced_jes": _check_unbalanced_jes,
    "zero_amount_jes": _check_zero_amount_jes,
    "legacy_item_prices": _check_legacy_item_prices,
    "inverted_doc_dates": _check_inverted_doc_dates,
    "fractional_piece_quantities": _check_fractional_piece_quantities,
}


async def _write_upgrade_report(
    results: list[dict],
    company_id,
    from_version: str,
    session: AsyncSession,
) -> str:
    """Write a structured JSON upgrade report to disk and store the path in company settings.

    Returns the absolute path of the written report file.
    """
    from celerp.models.company import Company
    from celerp.config import settings as app_settings

    to_version = getattr(app_settings, "version", "unknown")
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")

    auto_fixed: dict = {}
    manual_required: dict = {}

    for r in results:
        check = r["check"]
        details = r.get("details") or []
        if not details:
            continue
        if r.get("auto_fixable", False) and r.get("fixed", 0) > 0:
            auto_fixed[check] = details
        elif not r.get("auto_fixable", True) and r.get("found", 0) > 0:
            manual_required[check] = details

    report = {
        "from_version": from_version,
        "to_version": to_version,
        "generated_at": now.isoformat(),
        "company_id": str(company_id),
        "auto_fixed": auto_fixed,
        "manual_attention_required": manual_required,
    }

    # Determine report directory
    data_dir = getattr(app_settings, "data_dir", None) or os.path.expanduser("~/.celerp")
    report_dir = os.path.join(str(data_dir), "upgrade-reports", str(company_id))
    os.makedirs(report_dir, exist_ok=True)
    filename = f"{from_version}-to-{to_version}-{timestamp}.json"
    report_path = os.path.join(report_dir, filename)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # Store path in company settings for UI retrieval
    company = await session.get(Company, company_id)
    if company:
        settings = dict(company.settings or {})
        settings["upgrade_report_path"] = report_path
        company.settings = settings
        session.add(company)

    return report_path


@router.post("/doctor")
async def run_doctor(
    fix: bool = Query(False, description="Apply repairs (default: dry-run report only)"),
    checks: str | None = Query(None, description="Comma-separated check names (default: all)"),
    rebuild: bool = Query(False, description="Rebuild all projections after fixes"),
    from_version: str | None = Query(None, description="Previous version string - triggers upgrade report when provided with fix=true"),
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

    response: dict = {
        "mode": "fix" if fix else "dry-run",
        "checks_run": check_names,
        "total_found": total_found,
        "total_fixed": total_fixed,
        "rebuilt": rebuild and fix,
        "results": results,
    }

    # Write upgrade report when from_version is provided with fix=true
    if fix and from_version:
        report_path = await _write_upgrade_report(results, company_id, from_version, session)
        response["upgrade_report"] = report_path

    return response


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
