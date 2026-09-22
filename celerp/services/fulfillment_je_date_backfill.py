# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""One-time repair for legacy fulfillment COGS journal-entry dates."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.migrations._data_reconcile import get_meta, set_meta
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.business_time import business_date_at

log = logging.getLogger(__name__)

FULFILLMENT_JE_DATE_BACKFILL_KEY = "fulfillment_je_date_backfill_v1"

_FULFILLMENT_JE_RE = re.compile(r"^je:auto:(.+):(fulfill(?:-[1-9]\d*)?)$")
_DATE_REPAIR_EVENT = "sys.journal_entry.date_repaired"


def _legacy_fulfillment_identity(je_id: str) -> tuple[str, int, str] | None:
    """Return (doc_id, cycle, cycle_tag) only for the historical JE id family."""
    match = _FULFILLMENT_JE_RE.fullmatch(je_id)
    if match is None:
        return None
    tag = match.group(2)
    cycle = 0 if tag == "fulfill" else int(tag.removeprefix("fulfill-"))
    return match.group(1), cycle, tag


def _exact_legacy_fulfillment_cogs(entries) -> bool:
    """Exact two-line shape emitted by the historical fulfillment COGS path."""
    if not isinstance(entries, list) or len(entries) != 2:
        return False
    debit, credit = entries
    if not isinstance(debit, dict) or not isinstance(credit, dict):
        return False
    if set(debit) != {"account", "debit", "credit"}:
        return False
    if set(credit) != {"account", "debit", "credit"}:
        return False
    try:
        amount = float(debit.get("debit") or 0)
        return (
            debit.get("account") == "5100"
            and amount > 0
            and float(debit.get("credit") or 0) == 0
            and credit.get("account") == "1130-P"
            and float(credit.get("debit") or 0) == 0
            and float(credit.get("credit") or 0) == amount
        )
    except (TypeError, ValueError):
        return False


def _legacy_fulfillment_create(event: LedgerEntry) -> tuple[str, int, str] | None:
    """Prove a create event came from the exact pre-date fulfillment emitter."""
    identity = _legacy_fulfillment_identity(event.entity_id)
    if identity is None:
        return None
    doc_id, _, cycle_tag = identity
    data = event.data or {}
    expected_meta = {"trigger": "doc.fulfilled", "doc_id": doc_id}
    expected_key = f"je:auto:{doc_id}:{cycle_tag}"
    if not (
        event.entity_type == "journal_entry"
        and event.event_type == "acc.journal_entry.created"
        and event.source == "auto_je"
        and event.location_id is None
        and event.idempotency_key == f"{expected_key}:create"
        and (event.metadata_ or {}) == expected_meta
        and set(data) == {"memo", "entries"}
        and data.get("memo") == f"Auto JE for {doc_id} fulfilled (COGS)"
        and _exact_legacy_fulfillment_cogs(data.get("entries"))
    ):
        return None
    return identity


def _aware_fulfillment_instant(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        instant = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return instant


def _fulfillment_cycles(events: list[LedgerEntry]) -> tuple[dict[int, list[LedgerEntry]], dict[int, int]]:
    """Fold partial/full fulfillment events and reversals into lifecycle cycles."""
    cycle = 0
    fulfilled: dict[int, list[LedgerEntry]] = {}
    cycle_end: dict[int, int] = {}
    for event in sorted(events, key=lambda e: e.id):
        if event.event_type in {"doc.partially_fulfilled", "doc.fulfilled"}:
            fulfilled.setdefault(cycle, []).append(event)
        elif event.event_type == "doc.fulfillment_reversed":
            cycle_end.setdefault(cycle, event.id)
            cycle += 1
    return fulfilled, cycle_end


async def run_fulfillment_je_date_backfill(session) -> dict:
    """Repair only proven legacy fulfillment COGS JEs with a missing date.

    Immutable JE history proves eligibility. The matching partial/full
    fulfillment event supplies the actual fulfillment instant, and the company's configured IANA
    timezone supplies its business day. Projection state is only a final
    consistency gate and destination; it is never the source of eligibility.
    """
    conn = await session.connection()
    already = await conn.run_sync(
        lambda c: get_meta(c, FULFILLMENT_JE_DATE_BACKFILL_KEY)
    )
    if already:
        return {"changed": False}

    je_events = (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.entity_type == "journal_entry",
                LedgerEntry.entity_id.like("je:auto:%:fulfill%"),
            )
        )
    ).scalars().all()

    # Build complete immutable histories and identify only exact pre-date create
    # events in one pass. Multiple creates on one key remain visible so the
    # complete-history proof below rejects corrupt ambiguity.
    history_by_key: dict[tuple, list[LedgerEntry]] = {}
    candidates: dict[tuple, list[tuple[LedgerEntry, str, int, str]]] = {}
    for event in je_events:
        key = (event.company_id, event.entity_id)
        history_by_key.setdefault(key, []).append(event)
        if event.event_type != "acc.journal_entry.created" or event.source != "auto_je":
            continue
        identity = _legacy_fulfillment_create(event)
        if identity is None:
            continue
        doc_id, cycle, cycle_tag = identity
        candidates.setdefault(key, []).append((event, doc_id, cycle, cycle_tag))

    if not candidates:
        await conn.run_sync(
            lambda c: set_meta(c, FULFILLMENT_JE_DATE_BACKFILL_KEY, "done")
        )
        log.info("Fulfillment JE date backfill: 0 repaired, 0 deferred, 0 skipped")
        return {"changed": True, "repaired": 0, "deferred": 0, "skipped": 0}

    company_ids = {key[0] for key in candidates}

    doc_keys = {
        (key[0], candidate[0][1])
        for key, candidate in candidates.items()
        if candidate
    }
    doc_company_ids = {key[0] for key in doc_keys}
    doc_events = (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.entity_type == "doc",
                LedgerEntry.company_id.in_(doc_company_ids),
                LedgerEntry.event_type.in_(
                    (
                        "doc.partially_fulfilled",
                        "doc.fulfilled",
                        "doc.fulfillment_reversed",
                    )
                ),
            )
        )
    ).scalars().all()
    doc_events_by_key: dict[tuple, list[LedgerEntry]] = {}
    for event in doc_events:
        key = (event.company_id, event.entity_id)
        if key in doc_keys:
            doc_events_by_key.setdefault(key, []).append(event)
    fulfillment_cycles = {
        key: _fulfillment_cycles(events)
        for key, events in doc_events_by_key.items()
    }

    companies = (
        await session.execute(select(Company).where(Company.id.in_(company_ids)))
    ).scalars().all()
    company_by_id = {company.id: company for company in companies}

    projections = (
        await session.execute(
            select(Projection).where(
                Projection.entity_type == "journal_entry",
                Projection.entity_id.like("je:auto:%:fulfill%"),
            )
        )
    ).scalars().all()
    projection_by_key = {
        (row.company_id, row.entity_id): row
        for row in projections
        if (row.company_id, row.entity_id) in candidates
    }

    repaired = deferred = skipped = 0
    for key, create_candidates in candidates.items():
        # Immutable ambiguity or later JE history (void, prior repair, etc.) is
        # terminally outside this repair. Do not retry it forever.
        history = sorted(history_by_key.get(key, []), key=lambda e: e.id)
        if len(create_candidates) != 1 or len(history) != 2:
            skipped += 1
            continue

        create_event, doc_id, cycle, cycle_tag = create_candidates[0]
        if history[0].id != create_event.id:
            skipped += 1
            continue
        post_event = history[1]
        expected_meta = {"trigger": "doc.fulfilled", "doc_id": doc_id}
        expected_key = f"je:auto:{doc_id}:{cycle_tag}"
        if not (
            post_event.event_type == "acc.journal_entry.posted"
            and post_event.source == "auto_je"
            and post_event.actor_id == create_event.actor_id
            and post_event.location_id is None
            and post_event.idempotency_key == f"{expected_key}:posted"
            and (post_event.metadata_ or {}) == expected_meta
            and (post_event.data or {}) == {}
            and create_event.id < post_event.id
        ):
            skipped += 1
            continue

        fulfilled_by_cycle, cycle_end = fulfillment_cycles.get(
            (key[0], doc_id), ({}, {})
        )
        # The emitter runs immediately after the document's partial/full
        # fulfillment event. Earlier partials can legitimately exist in the same
        # cycle, so the exact source is the latest same-actor lifecycle event
        # preceding this JE, provided no reversal ended the cycle first.
        preceding = [
            event
            for event in fulfilled_by_cycle.get(cycle, [])
            if event.id < create_event.id and event.actor_id == create_event.actor_id
        ]
        if not preceding:
            skipped += 1
            continue
        fulfillment_event = max(preceding, key=lambda event: event.id)
        end_id = cycle_end.get(cycle)
        if not (
            fulfillment_event.id < create_event.id < post_event.id
            and (end_id is None or create_event.id < end_id)
        ):
            skipped += 1
            continue
        fulfillment_instant = _aware_fulfillment_instant(
            (fulfillment_event.data or {}).get("fulfilled_at")
        )
        if fulfillment_instant is None:
            skipped += 1
            continue

        # Timezone and projection state can legitimately be fixed later, so
        # these are deferred rather than terminally skipped.
        company = company_by_id.get(key[0])
        if company is None:
            deferred += 1
            continue
        timezone_name = (company.settings or {}).get("timezone")
        try:
            effective_date = business_date_at(fulfillment_instant, timezone_name)
        except ValueError:
            deferred += 1
            continue

        row = projection_by_key.get(key)
        state = (row.state or {}) if row is not None else {}
        if not (
            row is not None
            and row.entity_type == "journal_entry"
            and row.version == post_event.id
            and state.get("memo") == (create_event.data or {}).get("memo")
            and state.get("entries") == (create_event.data or {}).get("entries")
        ):
            deferred += 1
            continue
        existing_date = str(state.get("ts") or "")[:10]
        if existing_date and existing_date != effective_date:
            deferred += 1
            continue

        try:
            async with session.begin_nested():
                await emit_event(
                    session,
                    company_id=key[0],
                    entity_id=key[1],
                    entity_type="journal_entry",
                    event_type=_DATE_REPAIR_EVENT,
                    data={"ts": effective_date},
                    actor_id=None,
                    location_id=None,
                    source="data_repair",
                    idempotency_key=f"repair:fulfillment-je-date:{create_event.id}",
                    metadata_={},
                )
        except HTTPException as exc:
            if exc.status_code == 503:
                raise
            if exc.status_code == 422 and "locked" in str(exc.detail).lower():
                deferred += 1
                continue
            raise
        repaired += 1

    if not deferred:
        conn = await session.connection()
        await conn.run_sync(
            lambda c: set_meta(c, FULFILLMENT_JE_DATE_BACKFILL_KEY, "done")
        )

    log.info(
        "Fulfillment JE date backfill: %d repaired, %d deferred, %d skipped%s",
        repaired,
        deferred,
        skipped,
        "" if not deferred else " (marker left unset; next boot retries)",
    )
    return {
        "changed": True,
        "repaired": repaired,
        "deferred": deferred,
        "skipped": skipped,
    }
