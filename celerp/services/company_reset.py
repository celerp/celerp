# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Reset one company: remove it and everything it owns in one transaction, keeping every
login and every other company.

What the company owns is read from the live schema, never from a hand list: a table with
a ``company_id`` column, and any table reaching one through foreign keys. A table that is
neither and is not named below as belonging to the installation stops the reset before
anything is written, so a table added later can never be skipped or wiped by mistake.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.auth import SessionRegistry
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig
from celerp.models.migration import MigrationCleanupTask, MigrationRun
from celerp.services.auth import first_company_link
from celerp.services.company_backup import _fk_order, _ident, _schema

# Tables that belong to the installation rather than any one company, and why.
INSTALL_WIDE = {
    "users": "logins outlive any one company",
    "user_auth_state": "each login's session generation",
    "session_registry": "each login's issued sessions",
    "supporter_badges": "each login's supporter badge",
    "system_runtime_state": "the installation's own runtime state",
    "alembic_version": "the database schema version",
    "instance_meta": "the installation's upgrade markers, created at runtime",
}

NAME_MISMATCH = "The name you typed does not match this company's name. Nothing was deleted."
FAILED = "The company could not be reset. Nothing was deleted."


class ResetRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class _Owned:
    """A table holding the company's rows and the condition selecting them."""
    table: str
    where: str


async def owned_tables(session: AsyncSession) -> list[_Owned]:
    """Every table holding rows of a company, children before the tables they reference,
    ending with ``companies``. Refuses when a table cannot be placed."""
    schema = await _schema(session)
    where = {"companies": "id = :c"}
    for name, table in schema.items():
        if "company_id" in table.columns:
            where[name] = "company_id = :c"
    # A child reaches the company through foreign keys into tables already placed.
    while True:
        found = {}
        for name, table in schema.items():
            if name in where or name in INSTALL_WIDE:
                continue
            refs = [(cols, target, tcols) for cols, target, tcols in table.fks if target in where]
            if refs:
                found[name] = " OR ".join(
                    f"({', '.join(map(_ident, cols))}) IN "
                    f"(SELECT {', '.join(map(_ident, tcols))} FROM {_ident(target)} WHERE {where[target]})"
                    for cols, target, tcols in refs)
        if not found:
            break
        where.update(found)
    unplaced = sorted(t for t in schema if t not in where and t not in INSTALL_WIDE)
    if unplaced:
        raise ResetRefused(409, f"This company cannot be reset safely: {', '.join(unplaced)} could not be "
                                "identified as company data. Nothing was deleted.")
    crossing = sorted(t for t in INSTALL_WIDE if t in schema and t in where)
    crossing += sorted(t for t in INSTALL_WIDE if t in schema
                       and any(target in where for _, target, _ in schema[t].fks))
    if crossing:
        raise ResetRefused(409, f"This company cannot be reset safely: {', '.join(crossing)} holds both "
                                "installation and company data. Nothing was deleted.")
    order, unordered = _fk_order(list(where), schema)
    cycles = sorted(unordered - set(order))
    if cycles:
        raise ResetRefused(409, f"This company cannot be reset safely: {', '.join(cycles)} reference each "
                                "other. Nothing was deleted.")
    return [_Owned(t, where[t]) for t in reversed(order)]


async def reset(session: AsyncSession, company: Company, typed_name: str) -> uuid.UUID:
    """Delete *company* and every row it owns in the session's transaction, and record its
    files for deletion after the commit. The caller holds the company lock and connector
    maintenance lock, commits, then runs the returned cleanup task. Nothing is written
    unless every check passes; a database failure part way leaves the transaction to roll back."""
    if typed_name != company.name:
        raise ResetRefused(422, NAME_MISMATCH)
    tables = await owned_tables(session)
    cid = str(company.id)
    connected = sorted(set((await session.scalars(
        select(ConnectorConfig.connector).where(ConnectorConfig.company_id == cid))).all()))
    if connected:
        raise ResetRefused(409, f"Disconnect {', '.join(connected)} before resetting this company. "
                                "Nothing was deleted.")
    run_ids = [str(r) for r in (await session.scalars(
        select(MigrationRun.id).where(MigrationRun.company_id == company.id))).all()]
    members = set((await session.scalars(
        select(UserCompany.user_id).where(UserCompany.company_id == company.id))).all())
    try:
        for owned in tables:
            await session.execute(text(f"DELETE FROM {_ident(owned.table)} WHERE {owned.where}"), {"c": cid})
        # A login left with no company is signed out everywhere, so it no longer holds
        # the single direct sign-in place.
        left = [u for u in members if await first_company_link(session, u) is None]
        if left:
            await session.execute(delete(SessionRegistry).where(SessionRegistry.user_id.in_(left)))
        task = MigrationCleanupTask(company_id=company.id, run_ids=run_ids)
        session.add(task)
        await session.flush()
    except SQLAlchemyError as exc:
        raise ResetRefused(500, FAILED) from exc
    return task.id
