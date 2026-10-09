# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The document batch import, shared by the batch route and the migration sink.

Nothing here commits: the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.importers.results import ImportOutcome, OutcomeStatus, failure_reason
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.journal_accounts import require_line_destinations
from celerp_docs.routes import (
    DocImportRecord,
    DocPatch,
    _assert_doc_import_permissions,
    _assert_import_number_free,
    _doc_import_fields_changed,
    _get_doc,
    _import_auto_je,
    _import_metadata,
    _lock_imported_contact,
    _require_doc_rate_http,
    imported_opening_snapshot,
    write_doc_patch,
)

DOC_CREATED = "doc.created"


async def import_doc_records(
    session: AsyncSession,
    company_id: uuid.UUID,
    user,
    role: str,
    settings: dict,
    records: Sequence[DocImportRecord],
    *,
    upsert: bool = False,
    post_ledger: bool = True,
) -> ImportOutcome:
    """Create imported documents once per per-company idempotency key and entity.

    With `upsert`, a replayed key refreshes the document's editable fields through
    the normal document patch. Lifecycle permissions are checked for every record
    before the first write. Without `post_ledger`, an issued document is written
    without its normal accounting entry, for a caller that posts the entry the
    source books carry instead.
    """
    outcome = ImportOutcome()
    keys = [r.idempotency_key for r in records]
    existing_keys = set((await session.execute(
        select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key.in_(keys),
        )
    )).scalars().all())

    create_entity_ids = [r.entity_id for r in records if r.event_type == DOC_CREATED]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    company = await session.get(Company, company_id)
    base_currency = company.settings.get("currency", "USD") if company else "USD"
    # Fail authorization before the first row writes, so a mixed-status import cannot
    # partially apply before discovering that the caller lacks a lifecycle permission.
    for rec in records:
        if rec.event_type == DOC_CREATED:
            _assert_doc_import_permissions(settings, role, rec.data)
    for rec in records:
        if rec.event_type != DOC_CREATED:
            outcome.add(rec.entity_id, "rejected", f"{rec.entity_id}: event type {rec.event_type!r} is not import-safe")
            continue

        # A row names an existing document either by the key an earlier import gave it or,
        # for one made in the app, by its id; with upsert on, either is updated.
        if rec.idempotency_key in existing_keys:
            replay = await find_event_by_idempotency(session, company_id, rec.idempotency_key)
            if replay is None or replay.event_type != DOC_CREATED or replay.entity_id != rec.entity_id:
                outcome.add(rec.entity_id, "rejected", f"{rec.entity_id}: idempotency key belongs to another operation")
                continue
            if not upsert:
                outcome.add(rec.entity_id, "skipped")
                continue
        elif rec.entity_id in existing_entities:
            if not upsert:
                outcome.add(rec.entity_id, "skipped")
                continue
        upserting = rec.idempotency_key in existing_keys or rec.entity_id in existing_entities
        # A row is one unit: the document and its accounting entry are written together
        # in one savepoint, so a row refused part way leaves nothing behind and the rows
        # around it are unaffected.
        try:
            async with session.begin_nested():
                if upserting:
                    status = await _upsert_doc(session, company_id, user, role, settings, rec)
                else:
                    status = await _create_doc(
                        session, company_id, user, rec, base_currency, post_ledger=post_ledger,
                    )
        except Exception as exc:
            outcome.add(rec.entity_id, "failed", f"{rec.entity_id}: {failure_reason(exc)}")
            continue
        existing_keys.add(rec.idempotency_key)
        existing_entities.add(rec.entity_id)
        outcome.add(rec.entity_id, status)
    return outcome


async def _upsert_doc(session, company_id, user, role, settings, rec: DocImportRecord) -> OutcomeStatus:
    """Refresh an imported document's editable fields through the normal document edit."""
    row = await _get_doc(session, company_id, rec.entity_id)
    fields_changed = _doc_import_fields_changed(row.state, rec.data)
    if not fields_changed:
        return "skipped"
    canonical_patch = json.dumps(fields_changed, sort_keys=True, separators=(",", ":"), default=str)
    upsert_idem = (
        f"{rec.idempotency_key}:upsert:"
        f"{hashlib.sha256(canonical_patch.encode()).hexdigest()}"
    )
    result = await write_doc_patch(
        session, company_id, role, settings, user, rec.entity_id,
        DocPatch(fields_changed=fields_changed, idempotency_key=upsert_idem),
    )
    return "skipped" if result.get("event_id") is None else "updated"


async def _create_doc(
    session, company_id, user, rec: DocImportRecord, base_currency: str, *, post_ledger: bool,
) -> OutcomeStatus:
    """Write one imported document and, when it is issued, its accounting entry."""
    await _lock_imported_contact(session, company_id, "doc", rec.data)
    await _assert_import_number_free(session, company_id, "doc", rec.data)
    if auto_je.imported_issue_kind(rec.data) is not None:
        _require_doc_rate_http(rec.data, base_currency)
    data = rec.data
    if post_ledger:
        if auto_je.imported_issue_kind(data) == "bill":
            await require_line_destinations(session, company_id, data.get("line_items"))
        data = await imported_opening_snapshot(session, company_id, data)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=rec.entity_id,
        entity_type="doc",
        event_type=DOC_CREATED,
        data=data,
        actor_id=user.id,
        location_id=None,
        source=rec.source,
        idempotency_key=rec.idempotency_key,
        metadata_=_import_metadata(rec.source_ts, data, post_ledger=post_ledger),
    )
    if getattr(entry, "was_deduped", False):
        return "skipped"
    if post_ledger:
        await _import_auto_je(
            session, company_id, user.id, entry.entity_id, data,
            base_currency=base_currency,
        )
    return "created"
