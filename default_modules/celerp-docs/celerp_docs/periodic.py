# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The periodic dispatcher: unattended operations other modules register.

Each ``periodic_operation`` slot entry names ``async def handler(*, session, company_id,
today, budget) -> OperationReport`` and the module it belongs to. Every hour the loop
takes each active company and, for each operation whose module the company uses, calls
the handler in its own transaction under a transaction advisory try-lock keyed on the
operation and company: a pass that finds it held skips that operation this hour. A
failing operation is rolled back and logged; the other operations and companies carry
on. A company staged for a migration is skipped. Nothing is kept in memory between
passes: each operation finds its work in its own records.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.modules import slots
from celerp.modules.registry import uses_module
from celerp.services.business_time import company_today
from celerp.services.migrations import is_company_migration_staged

log = logging.getLogger(__name__)

SLOT = "periodic_operation"
BUDGET = 10  # documents (or runs) one operation may make for one company in one pass
_INTERVAL_SECONDS = 3600


@dataclass
class OperationReport:
    """What one operation did for one company: ``done`` units, and the steps to take once
    its transaction has committed (each called with the session factory)."""
    done: int = 0
    after_commit: list[Callable[..., Awaitable[None]]] = field(default_factory=list)


def _lock_key(operation: str, company_id) -> int:
    return int.from_bytes(hashlib.sha256(f"periodic:{operation}:{company_id}".encode()).digest()[:8],
                          "big", signed=True)


async def _try_lock(session: AsyncSession, operation: str, company_id) -> bool:
    """Hold the operation for this company until the transaction ends; False when another
    pass holds it. SQLite writes one transaction at a time already."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                     {"k": _lock_key(operation, company_id)}))


async def _run_operation(factory, company_id, entry: dict, today: date | None) -> OperationReport | None:
    async with factory() as session:
        try:
            company = await session.get(Company, company_id)
            if (company is None or not uses_module(company.settings, entry["module"])
                    or await is_company_migration_staged(session, company_id)):
                await session.rollback()
                return None
            if not await _try_lock(session, entry["handler"], company_id):
                await session.rollback()
                return None
            day = today or await company_today(session, company_id)
            report = await slots.resolve_handler(entry["handler"])(
                session=session, company_id=company_id, today=day, budget=BUDGET)
            await session.commit()
            return report
        except Exception:
            await session.rollback()
            log.exception("Periodic operation %s failed for company %s", entry["handler"], company_id)
            return None


async def run_company(factory, company_id, *, today: date | None = None) -> None:
    """One pass of every registered operation for one company."""
    for entry in slots.get(SLOT):
        report = await _run_operation(factory, company_id, entry, today)
        for step in (report.after_commit if report else []):
            try:
                await step(factory)
            except Exception:
                log.exception("Periodic operation %s: a step after commit failed for company %s",
                              entry["handler"], company_id)


async def tick(factory, *, today: date | None = None) -> None:
    """One pass over every active company."""
    async with factory() as session:
        companies = (await session.execute(
            select(Company.id).where(Company.is_active.is_(True)).order_by(Company.id))).scalars().all()
    for company_id in companies:
        await run_company(factory, company_id, today=today)


async def loop() -> None:
    """Background loop (the ``background_loop`` slot): a pass every hour."""
    from celerp.db import SessionLocal

    while True:
        try:
            await tick(SessionLocal)
        except Exception:
            log.exception("Periodic dispatcher pass failed")
        await asyncio.sleep(_INTERVAL_SECONDS)
