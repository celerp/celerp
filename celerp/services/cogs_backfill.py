# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""One-time COGS backfill for finalized invoices missing their COGS journal entry.

Invoices finalized before COGS moved into the finalize JE only received their
COGS at full fulfillment, so any of them never (or only partially) fulfilled
carries recognized revenue with no matching cost. This backfill posts the
missing Dr 5100 / Cr 1130-P pair once per database, dated to the invoice's own
finalize JE, and tells each affected company what happened via a bell
notification.

Runs at startup behind an instance_meta marker, exactly like the status-doc
backfill. A doc inside a locked accounting period (or one whose legacy state
the cost computation rejects) is skipped and counted; the marker stays unset in
that case so the next boot retries the stragglers - the emit idempotency keys
make re-posting impossible for docs already handled.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select

from celerp.migrations._data_reconcile import get_meta, set_meta
from celerp.models.notification import Notification
from celerp.models.projections import Projection
from celerp.notifications import service as notification_service
from celerp.services import auto_je
from celerp.services.auto_je import compute_doc_cogs, finalize_family_suffixes

log = logging.getLogger(__name__)

COGS_BACKFILL_KEY = "cogs_backfill"

_CATEGORY = "accounting"
_TITLE = "Cost of goods posted for past invoices"


def _je_doc_id(je_id: str, doc_ids: set[str]) -> str | None:
    """Recover the owning doc id from an auto-JE entity id.

    Auto-JE ids are je:auto:{doc_id}:{suffix} where both the doc id and the
    suffix can contain colons, so the split is resolved against the company's
    actual doc-id set: the longest candidate that is a real doc wins.
    """
    if not je_id.startswith("je:auto:"):
        return None
    rest = je_id[len("je:auto:"):]
    for parts in (1, 2):
        candidate = rest.rsplit(":", parts)[0]
        if candidate in doc_ids:
            return candidate
    return None


def _has_posted_cogs(je_states: list[dict]) -> bool:
    """True when any posted JE already debits 5100 - the doc's COGS exists."""
    for state in je_states:
        if state.get("status") != "posted":
            continue
        for entry in state.get("entries", []):
            if entry.get("account") == "5100" and float(entry.get("debit") or 0) > 0:
                return True
    return False


def _live_finalize_je(je_by_suffix: dict[str, dict], revert_count: int) -> dict | None:
    """The posted finalize-family JE state, latest cycle (or unvoid) winning."""
    live = None
    for suffix in finalize_family_suffixes(revert_count):
        state = je_by_suffix.get(suffix)
        if state is not None and state.get("status") == "posted":
            live = state
    return live


def _notify_body(c: dict) -> str:
    posted = c["posted"]
    parts = [f"{posted} invoice{'s' if posted != 1 else ''}, total {c['total']:.2f}."]
    if c["zero_cost"]:
        parts.append(f"{c['zero_cost']} zero cost, nothing to post.")
    if c["deferred"]:
        parts.append(f"{c['deferred']} deferred: accounting period locked.")
    if c["errored"]:
        parts.append(f"{c['errored']} could not be computed.")
    return " ".join(parts)


async def _notify(session, company_id, c: dict) -> None:
    """One bell notice per company, deduped on the unread stable title so a
    retrying boot never stacks duplicates."""
    existing = (await session.execute(
        select(Notification.id)
        .where(
            Notification.company_id == company_id,
            Notification.category == _CATEGORY,
            Notification.title == _TITLE,
            Notification.read == False,  # noqa: E712
        )
        .limit(1)
    )).scalar()
    if existing:
        return
    action_url = "/accounting?q=COGS%20backfill"
    if c["earliest"] and c["latest"]:
        action_url += f"&from={c['earliest']}&to={c['latest']}"
    await notification_service.create(
        session,
        company_id,
        _CATEGORY,
        _TITLE,
        _notify_body(c),
        user_id=None,
        action_url=action_url,
        priority="high",
    )


async def run_cogs_backfill(session) -> dict:
    """Post the missing COGS JE for every affected finalized invoice.

    Affected: doc_type invoice, a posted finalize-family JE exists, and no
    posted JE anywhere on the doc debits 5100. The JE amount comes from
    compute_doc_cogs over current projections; zero-cost docs post nothing and
    are only counted. Returns aggregate counts; the caller commits.
    """
    conn = await session.connection()
    already = await conn.run_sync(lambda c: get_meta(c, COGS_BACKFILL_KEY))
    if already:
        return {"changed": False}

    docs = (await session.execute(
        select(Projection).where(Projection.entity_type == "doc")
    )).scalars().all()
    jes = (await session.execute(
        select(Projection).where(Projection.entity_type == "journal_entry")
    )).scalars().all()

    doc_ids_by_company: dict = {}
    for doc in docs:
        doc_ids_by_company.setdefault(doc.company_id, set()).add(doc.entity_id)

    # (company_id, doc_id) -> {suffix: je_state}
    doc_jes: dict = {}
    for je in jes:
        doc_ids = doc_ids_by_company.get(je.company_id)
        if not doc_ids:
            continue
        doc_id = _je_doc_id(je.entity_id, doc_ids)
        if doc_id is None:
            continue
        suffix = je.entity_id[len(f"je:auto:{doc_id}:"):]
        doc_jes.setdefault((je.company_id, doc_id), {})[suffix] = je.state or {}

    per_company: dict = {}
    for doc in docs:
        state = doc.state or {}
        if state.get("doc_type") != "invoice":
            continue
        je_by_suffix = doc_jes.get((doc.company_id, doc.entity_id), {})
        if not je_by_suffix:
            continue
        revert_count = int(state.get("revert_count") or 0)
        fin_je = _live_finalize_je(je_by_suffix, revert_count)
        if fin_je is None:
            continue
        if _has_posted_cogs(list(je_by_suffix.values())):
            continue

        c = per_company.setdefault(doc.company_id, {
            "posted": 0, "total": 0.0, "zero_cost": 0,
            "deferred": 0, "errored": 0, "earliest": None, "latest": None,
        })
        ts = fin_je.get("ts") or state.get("finalized_at") or state.get("issue_date")
        try:
            async with session.begin_nested():
                cogs = (await compute_doc_cogs(session, doc.company_id, state)).total
                if cogs > 0:
                    await auto_je.create_for_doc_cogs_backfill(
                        session,
                        company_id=doc.company_id,
                        user_id=None,
                        doc_id=doc.entity_id,
                        cogs=cogs,
                        ts=ts,
                    )
        except HTTPException as exc:
            if exc.status_code == 503:
                # Backup in progress: run-level, nothing lands, next boot retries.
                raise
            if exc.status_code == 422 and "locked" in str(exc.detail).lower():
                c["deferred"] += 1
            else:
                c["errored"] += 1
                log.warning("COGS backfill could not post for %s: %s",
                            doc.entity_id, exc.detail)
        except Exception:
            c["errored"] += 1
            log.exception("COGS backfill could not compute %s", doc.entity_id)
        else:
            if cogs > 0:
                c["posted"] += 1
                c["total"] += cogs
                day = str(ts)[:10] if ts else None
                if day:
                    if c["earliest"] is None or day < c["earliest"]:
                        c["earliest"] = day
                    if c["latest"] is None or day > c["latest"]:
                        c["latest"] = day
            else:
                c["zero_cost"] += 1

    totals = {"posted": 0, "zero_cost": 0, "deferred": 0, "errored": 0}
    for company_id, c in per_company.items():
        for key in totals:
            totals[key] += c[key]
        if any((c["posted"], c["zero_cost"], c["deferred"], c["errored"])):
            await _notify(session, company_id, c)

    pending = totals["deferred"] or totals["errored"]
    if not pending:
        conn = await session.connection()
        await conn.run_sync(lambda c: set_meta(c, COGS_BACKFILL_KEY, "done"))

    log.info(
        "COGS backfill: %d posted, %d zero cost, %d deferred, %d errored%s",
        totals["posted"], totals["zero_cost"], totals["deferred"], totals["errored"],
        "" if not pending else " (marker left unset; next boot retries)",
    )
    return {"changed": True, **totals}
