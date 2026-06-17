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
from celerp.models.company import Company, Location
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

_log = logging.getLogger(__name__)


def _blank_str(v) -> bool:
    return not (str(v).strip() if v is not None else "")


async def backfill_self_contact_identity(session: AsyncSession, company_id) -> bool:
    """Sync the company's identity onto its self-contact where missing: the scalar fields tax_id / phone
    / email from company settings, and a billing address from the default Location. Company setup stored
    these on company settings + the Head Office Location, but the Company Details page and the document
    letterhead read the self-contact - so without this they show blank there. Idempotent: each piece is
    skipped when the self-contact already has it. Returns True if anything was added."""
    company = await session.get(Company, company_id)
    sid = (company.settings or {}).get("self_contact_id") if company else None
    if not sid:
        return False
    row = await session.get(Projection, {"company_id": company_id, "entity_id": sid})
    if row is None:
        return False
    state = row.state or {}
    settings = company.settings or {}
    changed = False

    # 1. Scalar identity (tax_id / phone / email): copy from settings where the self-contact is blank.
    fields = {f: settings.get(f) for f in ("tax_id", "phone", "email")
              if _blank_str(state.get(f)) and not _blank_str(settings.get(f))}
    if fields:
        await emit_event(
            session, company_id=company_id, entity_id=sid, entity_type="contact",
            event_type="crm.contact.updated",
            data={"fields_changed": {k: {"new": v} for k, v in fields.items()}},
            actor_id=None, location_id=None, source="migration",
            idempotency_key=f"self-migrate:identity:{sid}", metadata_={},
        )
        changed = True

    # 2. Billing address from the default Location, if the self-contact has none.
    if not (state.get("addresses") or []):
        loc = (await session.execute(
            select(Location).where(Location.company_id == company_id, Location.is_default.is_(True))
        )).scalars().first() or (await session.execute(
            select(Location).where(Location.company_id == company_id)
        )).scalars().first()
        addr = loc.address if loc else None
        text = ((addr.get("text") if isinstance(addr, dict) else addr if isinstance(addr, str) else "") or "").strip()
        if text:
            await emit_event(
                session, company_id=company_id, entity_id=sid, entity_type="contact",
                event_type="crm.contact.address_added",
                data={"address_id": f"address:self-{company_id}", "address_type": "billing",
                      "line1": text, "is_default": True},
                actor_id=None, location_id=None, source="migration",
                idempotency_key=f"self-migrate:address:{sid}", metadata_={},
            )
            changed = True
    return changed

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
    the batch. Idempotent and cheap on re-run: companies whose self_contact_id is already cached (new
    seed or previously migrated) are skipped without touching the ledger. Returns per-company summaries."""
    companies = (await session.execute(select(Company))).scalars().all()
    results: list[dict] = []
    for company in companies:
        if (company.settings or {}).get("self_contact_id"):
            continue  # already seeded under the new model, or already migrated - nothing to do
        try:
            res = await migrate_self_contacts(session, company.id, actor_id)
            await session.commit()
        except Exception as exc:  # one bad company must not abort the batch
            await session.rollback()
            _log.warning("migrate_self_contacts failed for company %s: %s", company.id, exc)
            res = {"company_id": str(company.id), "status": "error", "error": str(exc)}
        results.append(res)
    return results


async def backfill_self_contacts_hook(*, session: AsyncSession) -> None:
    """on_modules_ready lifecycle hook (fires on every startup, so it runs automatically after an
    upgrade): (1) collapse any legacy two-self-contact company into one both+is_self record, and (2) copy
    each company's default-Location address onto its self-contact if missing. Idempotent throughout."""
    await migrate_all_self_contacts(session)
    for cid in (await session.execute(select(Company.id))).scalars().all():
        try:
            if await backfill_self_contact_identity(session, cid):
                await session.commit()
        except Exception as exc:  # one bad company must not abort the batch
            await session.rollback()
            _log.warning("self-contact identity backfill failed for company %s: %s", cid, exc)
