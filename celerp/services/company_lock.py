# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Company-wide write serialization - one primitive for read-check-write sections."""

from __future__ import annotations

import hashlib

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

from celerp.models.company import Company
from celerp.models.projections import Projection

# The request's authorization (permissions.RequestAuthority), judged again the first time
# each transaction takes its company's lock; the refusal, once raised, blocks the commit.
AUTHORITY = "celerp_request_authority"
_REFUSED = "celerp_authority_refused"
# What the locks taken in this transaction proved, each with the (sub)transaction that
# took it: "locked" (lock_company held its row, company id), "loaded" (locked_company,
# company id), "deleting" (lock_company_for_deletion holds its row, company id), "judged"
# (the authority judged again, company id as text) and "modules" (the modules judged,
# (company id as text, names)). PostgreSQL releases a lock taken in a
# savepoint when that savepoint rolls back, so what it proved goes with it (_proofs_released).
_PROOFS = "celerp_lock_proofs"


def note(session, kind: str, key) -> None:
    """Record *key* under *kind* for as long as the work of the current transaction or
    savepoint lasts: gone when that savepoint rolls back or the transaction ends."""
    sync = getattr(session, "sync_session", session)
    taken_in = sync.get_nested_transaction() or sync.get_transaction()
    session.info.setdefault(_PROOFS, []).append((kind, key, taken_in))


def noted(session, kind: str) -> list:
    return [key for k, key, _ in session.info.get(_PROOFS, ()) if k == kind]


async def lock_company(session: AsyncSession, company_id) -> bool:
    """Serialize a read-check-write section with every other holder for the company.
    Returns whether the company row was there to lock: a caller with no request
    authority to judge again (background work) must stop on False, as the company is
    gone and nothing may be written for it.

    Two concurrent writers that each check a condition and then write would both
    pass the check. Taking a row lock on the company makes the second writer wait
    for the first to commit, so it reads the committed state instead. The lock is
    held until the caller's transaction commits or rolls back.

    The mode is FOR NO KEY UPDATE, not FOR UPDATE. Every ledger insert takes an
    implicit foreign-key KEY SHARE lock on its company row and holds it to commit,
    so a plain FOR UPDATE here would have to upgrade past that share lock: two
    transactions that have each already emitted an event for the company both hold
    KEY SHARE and then block on each other's row lock, which PostgreSQL breaks by
    aborting one with a deadlock (40P01). FOR NO KEY UPDATE does not conflict with
    KEY SHARE, so the upgrade never happens, while it still conflicts with another
    FOR NO KEY UPDATE.

    Canonical lock order: the module-state boundary when the caller holds it, then
    the company's module settings (judge_module_writes), then this lock, then any
    Projection row locks. Never acquire it after SELECT ... FOR UPDATE on a Projection.
    """
    await judge_module_writes(session, company_id)
    found = (await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update(key_share=True)
    )).first() is not None
    await _judge_authority_again(session, company_id)
    if found:
        note(session, "locked", company_id)
    return found


async def judge_module_writes(session: AsyncSession, company_id) -> None:
    """Work inside a module was admitted before it ran (modules.boundary.module_use), and
    a module may have been turned off since. Before its first write to the company in a
    transaction, the work holds the company's module settings (share_company_modules,
    which turning a module off waits for and holds through its commit) and judges the
    modules it relies on against them as committed now. A refusal, ModuleBusy when a
    turn-off holds the settings past the request lock timeout, is kept until the
    transaction ends, as an authority refusal is. Nothing outside module work."""
    from celerp.modules.boundary import (
        ModuleBusy, ModuleRefused, modules_in_use, refuse_writes, refused_past_lock_timeout, require_on,
    )
    from celerp.modules.registry import share_company_modules

    names = modules_in_use(company_id)
    if not names:
        return
    done = frozenset().union(*(n for c, n in noted(session, "modules") if c == str(company_id)))
    if names <= done:
        return
    try:
        if not done:
            with refused_past_lock_timeout(ModuleBusy(min(names))):
                await share_company_modules(session, company_id)
        note(session, "modules", (str(company_id), names - done))
        row = (await session.execute(select(Company.settings).where(Company.id == company_id))).first()
        require_on(None if row is None else dict(row[0] or {}), *sorted(names))
    except ModuleRefused as exc:
        session.info[_REFUSED] = exc
        refuse_writes(exc)
        raise


async def judge_without_company_lock(session: AsyncSession, company_id) -> None:
    """Judge the request again, as the company lock does, for the one write that must not
    wait for that lock: a migration cancel (migrations.request_cancel), saved while the
    runner holds the lock through a batch. The caller's membership is read under its row
    lock, so a role change in flight is waited for and a committed one is seen."""
    await _judge_authority_again(session, company_id)


async def _judge_authority_again(session: AsyncSession, company_id) -> None:
    """A request was authorized before its handler ran, but a role, membership or
    permission change commits under this same lock, so a write that waited here may wake
    to authority it no longer has. The first lock of each transaction re-reads the
    caller's membership, role and the company settings and judges the request again
    (permissions.RequestAuthority.judge_again); every later gate then reads the fresh
    values. A refusal is kept until the transaction ends, so a caller that catches it and
    carries on still cannot commit what it wrote before or after."""
    authority = session.info.get(AUTHORITY)
    if authority is None or str(authority.company_id) != str(company_id):
        return
    if str(company_id) in noted(session, "judged"):
        return
    note(session, "judged", str(company_id))
    try:
        await authority.judge_again(session)
    except Exception as exc:
        session.info[_REFUSED] = exc
        raise


async def lock_chart(session: AsyncSession, company_id) -> None:
    """Serialize changes to the shape of the company's chart of accounts.

    Adding an account, moving one, changing its type, switching it on or off, and
    pointing a posting role at one each read other accounts (a parent, the children)
    before writing. Taking this lock first makes those changes happen one at a time
    for a company, so each one checks the chart the previous one left. Postings
    never take it. A chart change is a company write, so this takes the company lock
    first (judging the request again), then this lock; account rows come after. Held
    until the transaction ends; SQLite writes one transaction at a time already.
    """
    await lock_company(session, company_id)
    if session.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(hashlib.sha256(b"chart:" + str(company_id).encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def hold_company(session: AsyncSession, company_id) -> bool:
    """Keep the company from being deleted until the caller's transaction ends; False when
    it is already gone.

    For writing what belongs to a company outside the database (stored files, uploads,
    backups). A company reset deletes the company FOR UPDATE, which waits for every holder,
    and runs its file cleanup after it commits: a write made while holding is removed by
    that cleanup, and one that starts after the reset finds the company gone. The mode is
    FOR KEY SHARE, so holders never wait for each other or for ordinary company writers."""
    held = await session.scalar(
        select(Company.id).where(Company.id == company_id).with_for_update(read=True, key_share=True))
    return held is not None


async def lock_company_for_deletion(session: AsyncSession, company_id) -> bool:
    """Take the company FOR UPDATE ahead of deleting it, waiting for every holder.
    False when the company is no longer there once the lock is free.

    Taken before any other lock on the company: a deleter that first held FOR NO KEY
    UPDATE and then asked for FOR UPDATE would deadlock with a holder (``hold_company``,
    or any ledger insert) that goes on to take ``lock_company`` for its own write."""
    found = (await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update())).first() is not None
    if found:
        note(session, "deleting", company_id)
    return found


async def lock_projections(session: AsyncSession, company_id, entity_ids) -> dict[str, Projection]:
    """Lock the company, then the given Projection rows in entity-id order.

    The one way to take row locks for a write: company first, then rows sorted by
    entity id, so two writers sharing rows always meet in the same order. A caller
    locking documents and items takes the documents in one call and the items in a
    later one. populate_existing replaces any stale copy already in the session.
    Ids with no row are absent from the result.
    """
    await lock_company(session, company_id)
    want = sorted({str(e) for e in entity_ids if e})
    if not want:
        return {}
    rows = (await session.execute(
        select(Projection)
        .where(Projection.company_id == company_id, Projection.entity_id.in_(want))
        .order_by(Projection.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
    return {r.entity_id: r for r in rows}


async def locked_company(session: AsyncSession, company_id) -> Company | None:
    """Take the company lock, then load the company as the previous holder committed it.

    For a read-modify-write of the company row itself (its settings in particular):
    reading after the lock means a concurrent writer's change is built on, never
    overwritten by a stale copy.

    Company settings are one JSON value, so this is the only way to change them:
    a flush that changes the settings of a company not loaded here is refused.
    Document numbering keeps its counters in the settings and follows the same rule.
    """
    await lock_company(session, company_id)
    company = await session.get(Company, company_id, populate_existing=True)
    if company is not None:
        note(session, "loaded", company.id)
    return company


def holds_company_lock(session, company_id) -> bool:
    """True when lock_company() found and locked the company in this transaction, judging
    the request's authority again, and PostgreSQL still holds the lock it took."""
    return str(company_id) in {str(c) for c in noted(session, "locked")}


def holds_locked_company(session, company_id) -> bool:
    """True when locked_company() loaded the company in this transaction and its lock still
    holds: the session's copy of the row is as the previous lock holder committed it. A copy
    loaded before a bare lock_company() may be stale."""
    return str(company_id) in {str(c) for c in noted(session, "loaded")}


@event.listens_for(Session, "before_flush")
def _settings_change_needs_the_lock(session: Session, flush_context, instances) -> None:
    for obj in session.dirty:
        if isinstance(obj, Company) and not holds_locked_company(session, obj.id) \
                and get_history(obj, "settings").has_changes():
            raise RuntimeError(
                f"Company {obj.id} settings changed without locked_company(); load the company "
                "with it before reading the settings to change"
            )


@event.listens_for(Session, "before_commit")
def _refused_authority_never_commits(session: Session) -> None:
    refused = session.info.get(_REFUSED)
    # Savepoint releases fire this too; only the outer commit makes anything durable.
    if refused is not None and not session.in_nested_transaction():
        raise refused


@event.listens_for(Session, "after_transaction_end")
def _lock_released(session: Session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_PROOFS, None)
        session.info.pop(_REFUSED, None)


@event.listens_for(Session, "after_soft_rollback")
def _proofs_released(session: Session, transaction) -> None:
    """A rolled-back savepoint released the locks taken in it and in the savepoints it
    contained, so the next lock judges again. A released savepoint's locks pass to its
    parent, as its proofs do. A refusal stays until the outer transaction ends."""
    boundary = transaction
    while boundary.parent is not None and not boundary.nested:
        boundary = boundary.parent
    proofs = session.info.get(_PROOFS)
    if not proofs:
        return

    def _inside(taken_in) -> bool:
        while taken_in is not None:
            if taken_in is boundary:
                return True
            taken_in = taken_in.parent
        return False

    proofs[:] = [p for p in proofs if not _inside(p[2])]
