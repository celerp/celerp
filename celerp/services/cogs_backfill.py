# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""One-time COGS backfill for finalized invoices missing their COGS journal entry.

Invoices finalized before COGS moved into the finalize JE only received their
COGS at full fulfillment, so any of them never (or only partially) fulfilled
carries recognized revenue with no matching cost. This backfill posts the
missing COGS / inventory pair once per database, dated to the invoice's own
finalize JE, and tells each affected company what happened via a bell
notification.

Runs at startup behind an instance_meta marker, exactly like the status-doc
backfill. A doc inside a locked accounting period, one whose lot is older stock
still waiting for the user to choose its inventory account, or one whose legacy
state the cost computation rejects is counted; the marker stays unset in that
case so the next boot retries the stragglers - the emit idempotency keys make
re-posting impossible for docs already handled. Each boot brings the company's
standing notice up to date, so it counts every invoice posted so far and only
what still waits. A doc whose line
quantity exceeds its bound lot is skipped terminally instead: the remainder was
drawn from sibling lots at costs unknowable now, so the owner posts that JE
manually from the notification.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import AccountRole, refusal
from celerp.migrations._data_reconcile import get_meta, set_meta
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.notifications import service as notification_service
from celerp.services import auto_je
from celerp.services.account_roles import LotOriginError, line_has_role, line_roles
from celerp.services.auto_je import compute_doc_cogs

log = logging.getLogger(__name__)

COGS_BACKFILL_KEY = "cogs_backfill"

_CATEGORY = "accounting"
_BACKFILL_SUFFIX = "cogs-backfill"


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


def _has_posted_cogs(settings: dict, je_states: list[dict]) -> bool | None:
    """True when any posted JE already debits a line posted for cost of goods sold,
    on whichever account the company used for it then - the doc's COGS exists. None
    when it cannot be told: an older debit line on an account the company never
    recorded for any role could be that cost."""
    unclassified = False
    for state in je_states:
        if state.get("status") != "posted":
            continue
        for entry in state.get("entries", []):
            if not float(entry.get("debit") or 0) > 0:
                continue
            if line_has_role(settings, entry, AccountRole.COGS):
                return True
            unclassified = unclassified or not line_roles(settings, entry)
    return None if unclassified else False


def _live_finalize_je(je_by_suffix: dict[str, dict]) -> dict | None:
    """The posted finalize-family JE state (fin, a re-finalize cycle, or an
    unvoid restore of one). The void sweep keeps at most one of them posted at
    any time, so the first posted match is the live one."""
    for suffix, state in je_by_suffix.items():
        if suffix == "fin" or suffix.startswith("fin:"):
            if state.get("status") == "posted":
                return state
    return None


async def legacy_cogs_refusal(session, company_id, doc_id: str, doc_state: dict) -> str | None:
    """Why a lot's cost correction cannot be posted to an invoice finalized before
    COGS moved into the finalize JE, or None when it can.

    Such an invoice books a lot's cost of sale in one JE (at fulfillment, or by
    this backfill). A correction adds the change in that cost, which stays exact
    when the invoice already books its COGS, so this backfill passes over it, or
    when this backfill has already run and would have booked the invoice at the
    lot's cost, as it does for every invoice whose lots it can cost exactly.
    """
    prefix = f"je:auto:{doc_id}:"
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix, autoescape=True),
    ))).scalars().all()
    je_by_suffix = {row.entity_id[len(prefix):]: row.state or {} for row in rows}
    if _live_finalize_je(je_by_suffix) is None:
        return ("but the invoice has no posted entry of its own, so its cost of goods sold cannot be "
                "adjusted automatically; correct it with a journal entry instead")
    company = await session.get(Company, company_id)
    posted_cogs = _has_posted_cogs((company.settings if company else None) or {}, list(je_by_suffix.values()))
    if posted_cogs:
        return None
    if posted_cogs is None:
        return ("whose entries hold a line on an account with no posting role, so whether its cost of "
                "goods sold was posted cannot be told; correct it with a journal entry instead")
    conn = await session.connection()
    if not await conn.run_sync(lambda c: get_meta(c, COGS_BACKFILL_KEY)):
        return ("whose cost of goods sold Celerp has not posted yet; it is posted the next time "
                "Celerp starts, after which the cost can be corrected")
    if (await compute_doc_cogs(session, company_id, doc_state)).ambiguous:
        return ("whose cost of goods sold was never posted because its lines cannot be matched to "
                "exact lots; correct it with a journal entry instead")
    return None


_NOTICE = "notice.cogs_backfill"
# The counts a notice tells only when there are any, in the order it tells them.
_OPTIONAL_PARTS = ("zero_cost", "deferred", "older_stock", "errored", "skipped")


def _notice_parts(c: dict) -> list[dict]:
    """The notice body's sentences, each a keyed message so the bell shows it in the
    reader's language: the invoices posted, then each count that is not zero."""
    from ui.i18n import t

    def part(key: str, **params) -> dict:
        return refusal(f"{_NOTICE}.{key}", t(f"{_NOTICE}.{key}", "en", **params), **params)

    posted = c["posted"]
    parts = [part("posted_one" if posted == 1 else "posted_many", count=posted, total=f"{c['total']:.2f}")]
    return parts + [part(key, count=c[key]) for key in _OPTIONAL_PARTS if c[key]]


def _count_posted(c: dict, cogs: float, ts) -> None:
    c["posted"] += 1
    c["total"] += cogs
    day = str(ts)[:10] if ts else None
    if day:
        if c["earliest"] is None or day < c["earliest"]:
            c["earliest"] = day
        if c["latest"] is None or day > c["latest"]:
            c["latest"] = day


async def _notify(session, company_id, c: dict) -> None:
    """One bell notice per company: a retrying boot brings the standing notice up to
    date (notify_standing) instead of stacking another. It is stored in English with the
    keys and params of its title and each part, which the bell shows in the reader's
    language."""
    from ui.i18n import t

    action_url = "/accounting?q=COGS%20backfill"
    if c["earliest"] and c["latest"]:
        action_url += f"&from={c['earliest']}&to={c['latest']}"
    parts = _notice_parts(c)
    await notification_service.notify_standing(
        session, company_id, _CATEGORY, t(f"{_NOTICE}.title", "en"), " ".join(p["message"] for p in parts),
        action_url=action_url,
        i18n={"title": f"{_NOTICE}.title", "body": f"{_NOTICE}.body", "params": {"parts": parts}})


async def run_cogs_backfill(session) -> dict:
    """Post the missing COGS JE for every affected finalized invoice.

    Affected: doc_type invoice, a posted finalize-family JE exists, and no
    posted JE anywhere on the doc debits cost of goods sold. The JE amount comes from
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

    settings_by_company = dict((await session.execute(select(Company.id, Company.settings))).all())
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

    def counts() -> dict:
        return {"posted": 0, "total": 0.0, "zero_cost": 0, "deferred": 0, "older_stock": 0,
                "errored": 0, "skipped": 0, "earliest": None, "latest": None}

    # What earlier boots posted, so a retry's notice counts every invoice posted so far.
    earlier: dict = {}
    for (company_id, _doc_id), je_by_suffix in doc_jes.items():
        backfill = je_by_suffix.get(_BACKFILL_SUFFIX)
        if backfill and backfill.get("status") == "posted":
            cogs = sum(float(e.get("debit") or 0) for e in backfill.get("entries", []))
            _count_posted(earlier.setdefault(company_id, counts()), cogs, backfill.get("ts"))

    per_company: dict = {}
    for doc in docs:
        state = doc.state or {}
        if state.get("doc_type") != "invoice":
            continue
        je_by_suffix = doc_jes.get((doc.company_id, doc.entity_id), {})
        if not je_by_suffix:
            continue
        fin_je = _live_finalize_je(je_by_suffix)
        if fin_je is None:
            continue
        posted_cogs = _has_posted_cogs(settings_by_company.get(doc.company_id) or {}, list(je_by_suffix.values()))
        if posted_cogs:
            continue

        c = per_company.setdefault(doc.company_id, counts())
        ts = fin_je.get("ts") or state.get("finalized_at") or state.get("issue_date")
        if posted_cogs is None:
            # Posting would risk a second COGS entry; the doc is reported and retried
            # once the books check has classified the older line.
            c["errored"] += 1
            log.warning("COGS backfill could not tell whether %s already has its COGS", doc.entity_id)
            continue
        try:
            async with session.begin_nested():
                cogs_result = await compute_doc_cogs(session, doc.company_id, state)
                cogs = cogs_result.total
                if not cogs_result.ambiguous and cogs > 0:
                    await auto_je.create_for_doc_cogs_backfill(
                        session,
                        company_id=doc.company_id,
                        user_id=None,
                        doc_id=doc.entity_id,
                        by_account=cogs_result.by_account,
                        ts=ts,
                    )
                    await auto_je.record_consignor_payables(session, doc.company_id, None, cogs_result.payables)
        except LotOriginError:
            # Older stock waiting for its inventory account: the user chooses it and the
            # next boot posts the doc.
            c["older_stock"] += 1
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
            if cogs_result.ambiguous:
                # The line drew beyond its bound lot, so the remainder's true lot
                # costs are unknowable now. Posting an extrapolated guess would put
                # a wrong number in the books; a skip is terminal and the owner
                # posts the JE manually from the notification.
                c["skipped"] += 1
                log.warning(
                    "COGS backfill skipped %s (%s): cost spans multiple lots, post manually",
                    doc.entity_id, state.get("doc_number") or "no number",
                )
            elif cogs > 0:
                _count_posted(c, cogs, ts)
            else:
                c["zero_cost"] += 1

    totals = {"posted": 0, "zero_cost": 0, "deferred": 0, "older_stock": 0, "errored": 0, "skipped": 0}
    for company_id, c in per_company.items():
        for key in totals:
            totals[key] += c[key]
        before = earlier.get(company_id)
        if before:
            c["posted"] += before["posted"]
            c["total"] += before["total"]
            days = [d for d in (c["earliest"], c["latest"], before["earliest"], before["latest"]) if d]
            c["earliest"], c["latest"] = (min(days), max(days)) if days else (None, None)
        if any(c[key] for key in totals):
            await _notify(session, company_id, c)

    pending = totals["deferred"] or totals["older_stock"] or totals["errored"]
    if not pending:
        conn = await session.connection()
        await conn.run_sync(lambda c: set_meta(c, COGS_BACKFILL_KEY, "done"))

    log.info(
        "COGS backfill: %d posted, %d zero cost, %d deferred, %d waiting on older stock, %d errored, "
        "%d skipped%s",
        totals["posted"], totals["zero_cost"], totals["deferred"], totals["older_stock"],
        totals["errored"], totals["skipped"],
        "" if not pending else " (marker left unset; next boot retries)",
    )
    return {"changed": True, **totals}
