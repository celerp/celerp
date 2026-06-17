# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""One-time migration: collapse a company's two legacy self-contacts into one.

Companies seeded before the self-contact unification got TWO registration contacts (a ``customer`` and a
``vendor``, idempotency keys ``reg:contact:customer:{cid}`` / ``reg:contact:vendor:{cid}``). The model is
now a single ``contact_type="both"`` + ``is_self=true`` record. This collapses the pair: the customer
record is the winner (field-backfilled from the vendor only where the customer was blank - owner-approved
precedence), the vendor record is merged into it (which re-points every doc/deal and tombstones it), the
winner is retyped ``both`` + ``is_self``, and its id is cached on ``company.settings.self_contact_id``.

Idempotent: re-running is a no-op. The retype event uses a deterministic idempotency key, and the merge is
only attempted while both live records exist - once the vendor record is tombstoned it is skipped.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

_log = logging.getLogger(__name__)

# Identity fields filled onto the winner from the vendor record only when the winner's value is blank.
# Customer record wins; vendor fills gaps (owner-approved precedence, Q1).
_BACKFILL_FIELDS = (
    "name", "company_name", "email", "phone", "tax_id", "website", "currency",
    "billing_address", "shipping_address", "payment_terms",
)


def _blank(v) -> bool:
    return v in (None, "", [], {})


async def migrate_self_contacts(session: AsyncSession, company_id, actor_id=None) -> dict:
    """Collapse one company's legacy customer+vendor self-contacts into a single both+is_self record.
    Emits events only; the caller commits. Returns a summary dict (status: noop | migrated)."""
    cust_key = f"reg:contact:customer:{company_id}"
    vend_key = f"reg:contact:vendor:{company_id}"
    rows = (await session.execute(
        select(LedgerEntry.entity_id, LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.event_type == "crm.contact.created",
            LedgerEntry.idempotency_key.in_([cust_key, vend_key]),
        )
    )).all()
    by_key = {k: eid for eid, k in rows}

    async def _live(cid):
        if not cid:
            return None
        row = await session.get(Projection, {"company_id": company_id, "entity_id": cid})
        if row is None or row.state.get("deleted") or row.state.get("merged_into"):
            return None
        return row

    cust = await _live(by_key.get(cust_key))
    vend = await _live(by_key.get(vend_key))
    winner = cust or vend
    if winner is None:
        return {"company_id": str(company_id), "status": "noop"}  # new seed, or both manually deleted

    # Merge only when BOTH live records exist; once the vendor is tombstoned this is skipped on re-run.
    source = vend if (winner is cust and vend is not None) else None

    # 1. Field backfill (vendor fills gaps) + retype to both+is_self, in one event. Deterministic key.
    fields: dict = {}
    if source is not None:
        for f in _BACKFILL_FIELDS:
            if _blank(winner.state.get(f)) and not _blank(source.state.get(f)):
                fields[f] = source.state.get(f)
    if winner.state.get("contact_type") != "both":
        fields["contact_type"] = "both"
    if winner.state.get("is_self") is not True:
        fields["is_self"] = True
    if fields:
        await emit_event(
            session, company_id=company_id, entity_id=winner.entity_id, entity_type="contact",
            event_type="crm.contact.updated",
            data={"fields_changed": {k: {"new": v} for k, v in fields.items()}},
            actor_id=actor_id, location_id=None, source="migration",
            idempotency_key=f"self-migrate:retype:{winner.entity_id}", metadata_={},
        )

    # 2. Merge the vendor record into the winner (re-points docs/deals + tombstones the vendor).
    docs_repointed = 0
    if source is not None:
        from celerp_contacts.routes import merge_contacts_service
        result = await merge_contacts_service(
            session, company_id, actor_id,
            target_contact_id=winner.entity_id, source_contact_ids=[source.entity_id],
        )
        docs_repointed = result.get("docs_updated", 0)

    # 3. Cache the canonical self-contact id for the Company Details page's direct lookup.
    company = await session.get(Company, company_id)
    if company is not None:
        company.settings = {**(company.settings or {}), "self_contact_id": winner.entity_id}

    return {
        "company_id": str(company_id), "status": "migrated", "winner": winner.entity_id,
        "merged_vendor": bool(source), "docs_repointed": docs_repointed,
    }


async def migrate_all_self_contacts(session: AsyncSession, actor_id=None) -> list[dict]:
    """Run migrate_self_contacts for every company, committing per company so one failure does not abort
    the batch. Idempotent end to end. Returns the per-company summaries."""
    company_ids = (await session.execute(select(Company.id))).scalars().all()
    results: list[dict] = []
    for cid in company_ids:
        try:
            res = await migrate_self_contacts(session, cid, actor_id)
            await session.commit()
        except Exception as exc:  # one bad company must not abort the batch
            await session.rollback()
            _log.warning("migrate_self_contacts failed for company %s: %s", cid, exc)
            res = {"company_id": str(cid), "status": "error", "error": str(exc)}
        results.append(res)
    return results
