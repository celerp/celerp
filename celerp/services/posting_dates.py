# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The one rule for the date a correction to an existing entry posts on.

An entry in an open period is corrected in place, on its own date. An entry in a locked
period is never changed: its correction posts on the company's business date today, on
another open date the user picks, or, while a migration is staged, on that migration's
cutover date. It never posts into the locked period and never backdates to the day after
the lock. When no open date is possible the correction is refused with a keyed message
saying what to do.

``void_reversal`` applies the rule to a journal entry void at the event boundary, so
every void path (a document void, a manual void, a reconciliation unmatch, a repair) is
covered by the same rule with no change at its call site.
"""
from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import refusal
from celerp.services.business_time import business_date_of


async def _settings(session: AsyncSession, company_id) -> dict:
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    return dict((company.settings or {}) if company else {})


def _lock_date(settings: dict) -> date | None:
    try:
        return date.fromisoformat(str(settings["lock_date"]))
    except (KeyError, ValueError, TypeError):
        return None


def _day(recorded, timezone_name: str | None) -> date:
    try:
        return date.fromisoformat(business_date_of(recorded, timezone_name))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _cutover_day(session: AsyncSession, company_id) -> date | None:
    """The cutover date of the migration staged into this company, if any."""
    from celerp.models.migration import MigrationRun, MigrationStatus
    from celerp.services.migrations import is_company_migration_staged

    if not await is_company_migration_staged(session, company_id):
        return None
    runs = (await session.execute(select(MigrationRun).where(
        MigrationRun.company_id == company_id,
        MigrationRun.status.notin_([MigrationStatus.COMPLETED.value, MigrationStatus.CANCELLED.value]),
    ).order_by(MigrationRun.created_at.desc()))).scalars().all()
    for run in runs:
        cutover = (run.mapping_decisions or {}).get("cutover_date")
        if cutover:
            return date.fromisoformat(str(cutover)[:10])
    return None


def _chosen_day(chosen: str, original: date, lock: date | None) -> date:
    try:
        day = date.fromisoformat(str(chosen))
    except ValueError:
        day = None
    if day is None or day < original:
        raise HTTPException(status_code=422, detail=refusal(
            "accounting.reversal_date_invalid",
            "Pick a reversal date as YYYY-MM-DD, on or after the date of the entry it reverses "
            f"({original.isoformat()}).", entry_date=original.isoformat()))
    if lock is not None and day <= lock:
        raise HTTPException(status_code=422, detail=refusal(
            "accounting.reversal_date_locked",
            f"The period is locked through {lock.isoformat()}, so nothing can post on {day.isoformat()}. "
            f"Pick a reversal date after {lock.isoformat()}.", lock_date=lock.isoformat(), date=day.isoformat()))
    return day


async def correction_day(session: AsyncSession, company_id, original_day, *, chosen: str | None = None) -> str | None:
    """The date a correction to an entry dated ``original_day`` posts on.

    None when the entry's own date is open and the user picked no other date: the
    correction is made in place. Otherwise the user's chosen date (refused with a keyed
    message when unreadable, before the entry, or locked), else a staged migration's
    cutover date, else the company's business date today. Refused with
    accounting.reversal_no_open_date when that date is itself locked.
    """
    settings = await _settings(session, company_id)
    tz = settings.get("timezone")
    lock = _lock_date(settings)
    original = _day(original_day, tz)
    if chosen:
        return _chosen_day(chosen, original, lock).isoformat()
    if lock is None or original > lock:
        return None
    day = await _cutover_day(session, company_id) or _day(None, tz)
    if day <= lock:
        raise HTTPException(status_code=422, detail=refusal(
            "accounting.reversal_no_open_date",
            f"The period is locked through {lock.isoformat()}, which includes today, so there is no "
            "open date to post the reversal on. Move the lock date back in Settings > Accounting, "
            "then try again.", lock_date=lock.isoformat()))
    return day.isoformat()


async def _require_accounts(session: AsyncSession, company_id, entity_id: str) -> None:
    """A reversal posts through the original entry's own accounts, active or not; it is
    refused only when one of them is no longer in the chart."""
    from celerp.models.projections import Projection
    from celerp.services.journal_accounts import lock_accounts

    entry = await session.get(Projection, (company_id, entity_id))
    codes = {str(line.get("account")) for line in ((entry.state or {}).get("entries") or []) if line.get("account")} \
        if entry is not None else set()
    if not codes:
        return
    chart = await lock_accounts(session, company_id, codes)
    if chart is None:
        return
    missing = sorted(codes - set(chart))
    if missing:
        raise HTTPException(status_code=422, detail=refusal(
            "accounting.reversal_account_missing",
            f"The entry posted to account {missing[0]}, which is no longer in the chart of accounts, "
            f"so its reversal cannot post. Add account {missing[0]} back under Settings > Accounting, "
            "then try again.", account=missing[0]))


async def void_reversal(session: AsyncSession, company_id, entity_id: str, data: dict) -> None:
    """Date an acc.journal_entry.voided event in place.

    ``data["ts"]`` is the voided entry's own date and ``data["reversed_on"]`` an optional
    date the user picked. When the void is not in place, ``reversed_on`` is set to the date
    its reversal posts on and the original's accounts are checked; the entry stays in its
    own period and the reversal lands on ``reversed_on``.
    """
    day = await correction_day(session, company_id, data.get("ts"), chosen=data.get("reversed_on"))
    if day is None:
        data.pop("reversed_on", None)
        return
    data["reversed_on"] = day
    await _require_accounts(session, company_id, entity_id)
