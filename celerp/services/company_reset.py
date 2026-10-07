# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Reset one company: remove it and everything it owns in one transaction, keeping every
login and every other company.

What the company owns is read from the database catalog (celerp.db_catalog), never from a
hand list, so a switched-off module's tables are included: a table with a ``company_id``
column, and any table reaching one through foreign keys. A table that reaches neither a
company nor a table named below as belonging to the installation stops the reset before
anything is written, so a table added later can never be skipped or wiped by mistake.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp import db_catalog
from celerp.accounting_roles import refusal
from celerp.db import sqlstate

from celerp.models.accounting import UserCompany
from celerp.models.ai import AIBatchJob
from celerp.models.auth import SessionRegistry
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig
from celerp.models.migration import MigrationCleanupTask, MigrationRun
from celerp.services import payments
from celerp.services.auth import first_usable_company_link
from celerp.services.company_lock import lock_company_for_deletion
from ui.i18n import t

# Tables that belong to the installation rather than any one company, and why.
INSTALL_WIDE = {
    "users": "logins outlive any one company",
    "user_auth_state": "each login's session generation",
    "supporter_badges": "each login's supporter badge",
    "system_runtime_state": "the installation's own runtime state",
    "alembic_version": "the database schema version",
    "instance_meta": "the installation's upgrade markers, created at runtime",
    "payment_closures": "requests to close a company's online payments, which outlive the company",
    "payment_recoveries": "the installation's System Recovery restores, as Celerp Cloud must learn of them",
    "unmatched_payments": "online payments received for a company or invoice that no longer exists",
    "unmatched_refunds": "refunds of online payments kept until their payment is on its invoice",
}

AI_BATCH_ACTIVE = ("Wait for the assistant to finish reading files before resetting this company. "
                   "Nothing was deleted.")
FAILED = "The company could not be reset. Nothing was deleted."
# Another transaction in the way: the reset rolls back and is refused as busy.
_BUSY = ("40P01", "40001", "55P03")  # deadlock, serialization failure, lock wait timed out
PAYMENTS_NOT_CLOSED = {
    "disconnected": (503, "Reconnect Celerp Cloud so this company's online invoice payments can be "
                          "closed, then reset it. Nothing was deleted."),
    "payment_settling": (409, "A payment on one of this company's invoices is still being processed. "
                              "Try again once it has finished. Nothing was deleted."),
    "reconnect_required": (409, f"{t('pay.settings_revoked', 'en')} Nothing was deleted."),
    "payment_unrecorded": (409, "A payment on one of this company's invoices has not reached Celerp yet. "
                                "Try again once it shows on the invoice. Nothing was deleted."),
    "update_required": (409, "A refund of an online payment on one of this company's invoices can only be "
                             "recorded by a newer version of Celerp. Update Celerp, then try again. "
                             "Nothing was deleted."),
    "unconfirmed": (503, "Celerp could not confirm with Celerp Cloud that this company's online invoice "
                         "payments are closed. Try again in a moment. Nothing was deleted."),
}


class ResetRefused(Exception):
    def __init__(self, status: int, detail: str | dict, closure: uuid.UUID | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.closure = closure


@dataclass(frozen=True)
class Reset:
    """What a reset leaves for its caller once the transaction ends: the cleanup task
    to run, and the payment closure to settle (``payments.settle_company_closure``)."""
    task_id: uuid.UUID
    closure: uuid.UUID | None


def _busy() -> ResetRefused:
    return ResetRefused(409, refusal(
        "company_reset.busy",
        "This company could not be reset because other changes were being saved at the "
        "same time. Nothing was deleted. Try again."))


def _changed_outside(kind: str, table: str) -> ResetRefused:
    """The refusal for a table changed outside Celerp (``db_catalog.changed_outside``)."""
    if kind == "outside_reference":
        return ResetRefused(409, refusal(
            "company_reset.outside_reference",
            f"This company cannot be reset because the table {table}, which was added outside "
            "Celerp (by an installed module or a direct database change), refers to Celerp's "
            "records. Nothing was deleted. Ask whoever installed that module or changed the "
            "database to remove that reference.", table=table))
    return ResetRefused(409, refusal(
        "company_reset.partition_key",
        f"This company cannot be reset because the table {table} was changed outside Celerp "
        "(by an installed module or a direct database change) in a way the reset cannot safely "
        "handle. Nothing was deleted. Ask whoever installed that module or changed the database "
        "to fix it.", table=table))


def _placed(schema: dict) -> None:
    """Refuse a table that holds neither company nor installation data, and an
    installation table that would also hold company data."""
    company = db_catalog.company_tables(schema)
    install = {name for name in INSTALL_WIDE if name in schema}
    # A table hanging off the installation's tables (a login's own rows) is the installation's.
    installation = db_catalog.reach(schema, install, lambda name, fk: True)
    unplaced = sorted(name for name in schema if name not in company and name not in installation)
    if unplaced:
        raise ResetRefused(409, f"This company cannot be reset safely: {', '.join(unplaced)} could not be "
                                "identified as company data. Nothing was deleted.")
    crossing = sorted(install & company)
    if crossing:
        verb = "holds" if len(crossing) == 1 else "hold"
        raise ResetRefused(409, f"This company cannot be reset safely: {', '.join(crossing)} {verb} both "
                                "installation and company data. Nothing was deleted.")


def _company_rows(schema: dict) -> dict[str, str]:
    """Every table holding rows of the company bound as ``:c``, each after the tables it
    references, with the condition that picks them: its company column, or else a
    foreign key to rows already picked (a conversation's messages, a run's entity maps).
    A key that clears on delete picks nothing: Postgres clears it. Tables that refer to
    each other in a loop have no such order, nor a condition built from one another, so
    the reset is refused naming them."""
    ident = db_catalog.ident
    owned = db_catalog.company_tables(schema, held=True)
    order, unordered = db_catalog.fk_order(sorted(owned), schema)
    if looped := ", ".join(sorted(unordered - set(order))):
        raise ResetRefused(409, refusal(
            "company_reset.reference_cycle",
            f"The tables {looped} refer to each other in a loop, so this company cannot be "
            "reset. Nothing was deleted.", tables=looped))
    where: dict[str, str] = {"companies": "id = CAST(:c AS uuid)"}

    def rows(name: str) -> str:
        if name not in where:
            table = schema[name]
            if "company_id" in table.columns:  # a few connector tables keep it as text
                where[name] = f"company_id = CAST(CAST(:c AS text) AS {ident(table.columns['company_id'].udt)})"
            else:
                where[name] = " OR ".join(
                    f"({', '.join(map(ident, fk.cols))}) IN (SELECT {', '.join(map(ident, fk.tcols))} "
                    f"FROM {ident(fk.target)} WHERE {rows(fk.target)})"
                    for fk in table.fks if fk.target in owned and fk.target != name and not fk.clears)
        return where[name]

    return {name: rows(name) for name in order}


def _lock_writers(schema: dict) -> list[str]:
    """The locks on every table, held until the reset commits. Nothing written to a company
    table between the checks and the deletes can then be deleted with the company, and no
    table or key can be added that the reset does not know about: such a write waits, and
    fails on the row that is gone. An installation table keeps taking writes, since its rows
    outlive the company (a payment closure is recorded from its own connection during the
    reset), but no key can be added to it. So do sessions: issuing one holds the company FOR
    KEY SHARE, which waits for the reset, and a sign-out during the reset only deletes them.
    Concurrent resets take it in the same order, one after the other."""
    writable = sorted(set(schema) & (INSTALL_WIDE.keys() | {SessionRegistry.__tablename__}))
    company = sorted(set(schema) - set(writable))
    return [f"LOCK TABLE {', '.join(map(db_catalog.ident, names))} IN {mode} MODE"
            for names, mode in ((company, "SHARE ROW EXCLUSIVE"), (writable, "ROW EXCLUSIVE")) if names]


def _held_elsewhere(schema: dict) -> str:
    """A query naming a table whose rows the reset of the company bound as ``:c`` would
    delete, change or trip over though they are not only that company's: a row outside
    the company naming one of its rows, or a row of the company also naming another
    company's, by a key of any kind. Nothing when there is none."""
    ident = db_catalog.ident
    rows = _company_rows(schema)
    company = db_catalog.company_tables(schema)

    def naming(fk, mine: bool, seen: frozenset[str] = frozenset()) -> str:
        """The rows whose ``fk`` names a row of the company (``mine``) or of another."""
        if fk.target in rows:
            where = f"({rows[fk.target]})" + ("" if mine else " IS NOT TRUE")
        else:  # reached only through keys that clear: another company's when it names one
            where = "FALSE" if fk.target in seen else others(fk.target, seen | {fk.target}) or "FALSE"
        return f"({', '.join(map(ident, fk.cols))}) IN (SELECT {', '.join(map(ident, fk.tcols))} " \
               f"FROM {ident(fk.target)} WHERE {where})"

    def others(name: str, seen: frozenset[str]) -> str:
        """The rows of ``name``, a table with no company column, that are another company's
        too: those naming one of its rows."""
        return " OR ".join(naming(fk, False, seen) for fk in schema[name].fks if fk.target in company)

    checks = []
    for name in sorted(company):
        table = schema[name]
        theirs = "" if "company_id" in table.columns else others(name, frozenset())
        for fk in table.fks:
            if fk.target not in rows:
                continue
            if name in rows:
                # A named row tied to the company by a key of its own straight to companies,
                # as a user is by a home company, is shared by the two companies, so its
                # table, which holds that key, is the one named.
                shared = fk.target != "companies" and "company_id" not in schema[fk.target].columns and any(
                    k.target == "companies" for k in schema[fk.target].fks)
                checks.append((name, f"({rows[name]}) IS NOT TRUE AND {naming(fk, mine=True)}",
                               fk.target if shared else name))
            elif theirs:
                checks.append((name, f"{naming(fk, mine=True)} AND ({theirs})", name))
        if name in rows and theirs:
            checks.append((name, f"({rows[name]}) AND ({theirs})", name))
    return " UNION ALL ".join(
        f"(SELECT '{named.replace(chr(39), chr(39) * 2)}' WHERE EXISTS "
        f"(SELECT 1 FROM {ident(name)} WHERE {where}))" for name, where, named in checks) + " LIMIT 1"


def _company_deletes(schema: dict) -> list[str]:
    """The deletes that remove the company bound as ``:c``, each table before any it
    references."""
    return [f"DELETE FROM {db_catalog.ident(name)} WHERE {where}"
            for name, where in reversed(_company_rows(schema).items())]


async def _company_schema(session: AsyncSession, cid: str) -> dict:
    """The catalog's keys, read and locked for the reset of the company bound as ``cid``
    once every table is placed and no row the reset would reach belongs to another
    company. Raises ResetRefused otherwise, before anything is written."""
    try:
        await db_catalog.pin(session)
    except db_catalog.TableElsewhere as exc:
        raise _changed_outside("partition_key", exc.table) from None
    schema = await db_catalog.read(session)
    # A table this connection cannot read and delete from can neither be locked below
    # nor have the company's rows picked out of it.
    if tables := await db_catalog.hidden(session, schema):
        raise _changed_outside("partition_key", await db_catalog.label(session, tables[0]))
    _placed(schema)
    _company_rows(schema)
    # Another transaction writing these tables can hold them for longer than a request
    # may wait, or lock in the opposite order so Postgres aborts one of the two. Either
    # way the rollback leaves everything as it was and the owner is asked to try again.
    for lock in _lock_writers(schema):
        await session.execute(text(lock))
    if await db_catalog.read(session) != schema:  # a table or key added before the lock
        raise _busy()
    if changed := await db_catalog.changed_outside(session, schema):
        raise _changed_outside(*changed)
    keys = db_catalog.own_keys(schema)
    if held := await session.scalar(text(_held_elsewhere(keys)), {"c": cid}):
        raise ResetRefused(409, refusal(
            "company_reset.held_elsewhere",
            f"This company cannot be reset because records in {held} that belong to another "
            "company refer to its data. Nothing was deleted.", table=held))
    return keys


async def reset(session: AsyncSession, company: Company, typed_name: str) -> Reset:
    """Delete *company* and every row it owns in the session's transaction, and record its
    files for deletion after the commit. The caller holds the connector maintenance lock,
    took ``lock_company_for_deletion`` before any other company lock, and ends the
    transaction; then, whether it committed or rolled back, settles the payment closure
    (also when this raises ResetRefused carrying one) and, after a commit, runs the
    cleanup task. Nothing is written unless every check passes, and a company connected
    to Celerp Cloud has its online payments closed there first; a database failure part
    way leaves the transaction to roll back."""
    if typed_name != company.name:
        raise ResetRefused(422, refusal(
            "company_reset.name_mismatch",
            "The name you typed does not match this company's name. Nothing was deleted."))
    # A session being issued, or a file being stored, holds the company FOR KEY SHARE until
    # it is saved: the reset waits for it and then removes it with the company, and a later
    # one waits for the reset and finds the company gone.
    await lock_company_for_deletion(session, company.id)
    cid = str(company.id)
    try:
        keys = await _company_schema(session, cid)
    except DBAPIError as exc:
        if sqlstate(exc) not in _BUSY:
            raise
        raise _busy() from exc
    connected = sorted(set((await session.scalars(
        select(ConnectorConfig.connector).where(ConnectorConfig.company_id == cid))).all()))
    if connected:
        raise ResetRefused(409, f"Disconnect {', '.join(connected)} before resetting this company. "
                                "Nothing was deleted.")
    # A connector set up before companies had their own is adopted by the only company left
    # at the next startup, so a reset must never be what leaves one company standing beside it.
    # An unfinished import's company does not count: discarding it removes it again.
    from celerp.config import ensure_instance_id
    unassigned = sorted(set((await session.scalars(
        select(ConnectorConfig.connector).where(ConnectorConfig.company_id == ensure_instance_id()))).all()))
    if unassigned and len((await session.scalars(
            select(Company.id).where(Company.id != company.id, Company.is_migration_staged.is_(False))
            .limit(2))).all()) <= 1:
        raise ResetRefused(409, f"{', '.join(unassigned)} was set up before companies had their own "
                                "connectors and belongs to no company yet. Connect it in the company it "
                                "belongs to before resetting this one. Nothing was deleted.")
    # A batch is created under the company's key lock, so none can start once this check passed.
    reading = await session.scalar(select(AIBatchJob.id).where(
        AIBatchJob.company_id == company.id, AIBatchJob.status.in_(("pending", "running"))).limit(1))
    if reading is not None:
        raise ResetRefused(409, AI_BATCH_ACTIVE)
    run_ids = [str(r) for r in (await session.scalars(
        select(MigrationRun.id).where(MigrationRun.company_id == company.id))).all()]
    members = set((await session.scalars(
        select(UserCompany.user_id).where(UserCompany.company_id == company.id))).all())
    # Last, so a refusal above never closes the payments of a company that stays. They
    # are reopened when the deletion below does not commit.
    try:
        closure = await payments.prepare_company_closure(company.id)
    except payments.PaymentsNotClosed as exc:
        raise ResetRefused(*PAYMENTS_NOT_CLOSED[exc.reason]) from None
    try:
        for statement in _company_deletes(keys):
            await session.execute(text(statement), {"c": cid})
        await payments.unrecord_company(session, company.id)
        # A login left with no company is signed out everywhere, so it no longer holds
        # the single direct sign-in place.
        left = [u for u in members if await first_usable_company_link(session, u) is None]
        if left:
            await session.execute(delete(SessionRegistry).where(SessionRegistry.user_id.in_(left)))
        task = MigrationCleanupTask(company_id=company.id, run_ids=run_ids)
        session.add(task)
        await session.flush()
    except SQLAlchemyError as exc:
        if isinstance(exc, DBAPIError) and sqlstate(exc) in _BUSY:
            raise ResetRefused(409, _busy().detail, closure) from exc
        raise ResetRefused(500, FAILED, closure) from exc
    return Reset(task.id, closure)
