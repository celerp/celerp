# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The one refusal for work in a module a company cannot use right now.

A module's routes, an online payment recorded on an invoice, a connector sync writing
items or documents, connecting a store and choosing a deposit account all check here,
so the work stops at its boundary with a message naming the module, never an import
error part way through. Two states are refused, each in its own words:

- the company turned the module off;
- the company uses it, but this process has not started it yet (a business type or a
  module was just turned on and Celerp has not restarted).

The decision holds for the whole of the work, never only at its start:

- admission (``module_use``) judges the company's settings under the gate of each
  module it needs for that company and the module-state boundary, both in shared
  mode, and counts the work as running in each of those modules before letting go;
- turning a module off takes that module's gate for the company, then waits for the
  company's running work in that module to finish (``settle_module_use``), so nothing
  admitted before it, a request to a store included, happens after it. Other modules
  and other companies are never held up by the wait, and while it holds the
  module-state boundary it waits on the company's own locks only briefly
  (take_company_locks), so no other company's work is refused for it;
- every write the work makes judges the settings again, holding them as they are
  (company_lock.judge_module_writes, from events.engine.emit_event and
  company_lock.lock_company), so a change committed elsewhere is never written past,
  and the work ends refused even when it caught the refusal.

Work that meets one of these locks held for longer than the request lock timeout is
refused with ModuleBusy, a 409 that says to try again, never a server error.

Lock order: the company's module gates, then the module-state boundary, then the
company's module settings, then the company, then its rows. Admission ends before the
work starts, so no lock is held across a request to another system."""
from __future__ import annotations

import asyncio
import functools
import inspect
from collections import Counter
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Mapping

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from celerp.accounting_roles import refusal
from celerp.capacity import MODULE_SETTLE_SECONDS
from celerp.db import get_session_ctx, sqlstate
from celerp.modules.loader import is_running, module_label
from celerp.modules.registry import share_module_gate, share_module_state, uses_module

# The companies whose module use the running work was admitted for, each with the
# modules it relies on; every write the work makes judges them again (judge_writes).
_IN_USE: ContextVar[dict[str, frozenset[str]]] = ContextVar("celerp_module_use", default={})
# The refusals the running work's writes met (refuse_writes), shared by nested work: a
# caller that catches one and carries on still ends refused, never as if it had written.
_REFUSALS: ContextVar[list[Exception] | None] = ContextVar("celerp_module_refusals", default=None)
# Admitted work running in this process, per company and module (module_use,
# settle_module_use).
_RUNNING: Counter[tuple[str, str]] = Counter()
# The Postgres SQLSTATE of a lock wait that gave up at the request lock timeout.
LOCK_TIMEOUT = "55P03"
# How long a change to modules waits, in all, on the locks of the companies it changes
# while it holds the module-state boundary (take_company_locks): well inside the request
# lock timeout every admission on the installation waits for that boundary with.
_WAIT_UNDER_BOUNDARY_MS = 1000


class ModuleRefused(HTTPException):
    """Work in a module that did not go ahead, naming the module. Outside a request it
    still reads as that one sentence, so a sync run or a delivery log shows the same
    message. A caller that records a refusal as final catches this, not a bare
    HTTPException, so a busy refusal is retried and never recorded as final."""

    def __init__(self, pkg_name: str, status_code: int, detail: dict, headers: dict | None = None) -> None:
        self.module = pkg_name
        super().__init__(status_code=status_code, detail=detail, headers=headers)

    def __str__(self) -> str:
        return self.detail["message"]


class ModuleOff(ModuleRefused):
    """A 403: the company cannot use the module."""

    def __init__(self, pkg_name: str, *, starting: bool = False) -> None:
        label = module_label(pkg_name)
        detail = (refusal("modules.starts_after_restart",
                          f"The {label} module starts after Celerp restarts.", module=label)
                  if starting else
                  refusal("modules.off_for_company",
                          f"The {label} module is turned off for your company.", module=label))
        super().__init__(pkg_name, 403, detail)


class ModuleBusy(ModuleRefused):
    """A 409 with Retry-After: the work met a change to modules in progress (a lock held
    past the request lock timeout), so it did not go ahead, and can be tried again."""

    def __init__(self, pkg_name: str) -> None:
        label = module_label(pkg_name)
        super().__init__(pkg_name, 409, refusal(
            "modules.use_busy",
            f"Modules are being changed right now, so this {label} work did not go ahead. Try again in a minute.",
            module=label), headers={"Retry-After": str(int(MODULE_SETTLE_SECONDS))})


class CompanyBusy(HTTPException):
    """A 409: a change to modules met the company's other work still holding the company's
    locks past the brief wait (take_company_locks), so it did not go ahead, and can be
    tried again. Which work holds them is not known here (it may be in another process),
    so it names none."""

    def __init__(self) -> None:
        super().__init__(status_code=409, detail=refusal(
            "modules.company_busy",
            "Other work for your company is still being saved, so this change did not go ahead. "
            "Try again in a minute."))


async def take_company_locks(session, *locks: Callable[[], Awaitable[Any]]) -> list[Any]:
    """Take *locks* (each a call taking one company lock in *session*) in turn while
    *session* holds the module-state boundary, waiting on them for _WAIT_UNDER_BOUNDARY_MS
    in all; their results. Every admission on the installation waits for that boundary, so
    no other company's work is held up past that wait, which is well inside the request
    lock timeout. Past it raises CompanyBusy and leaves the transaction to be rolled back;
    otherwise the session is back on its own lock timeout. The one way a change to modules
    takes company locks, in the lock order: the boundary first. Off Postgres (no lock
    waits) the locks are just taken."""
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return [await lock() for lock in locks]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_UNDER_BOUNDARY_MS / 1000
    taken = []
    with refused_past_lock_timeout(CompanyBusy()):
        for lock in locks:
            left = int((deadline - loop.time()) * 1000)
            if left < 1:
                raise CompanyBusy()
            await session.execute(text(f"SET LOCAL lock_timeout = {left}"))
            taken.append(await lock())
    await session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return taken


@contextmanager
def refused_past_lock_timeout(refused: HTTPException):
    """Raise *refused* in place of a lock wait in the block that gave up at the request
    lock timeout; any other database error goes on as it is."""
    try:
        yield
    except DBAPIError as exc:
        if sqlstate(exc) != LOCK_TIMEOUT:
            raise
        raise refused from None


def require_company_uses(settings: dict | None, *pkg_names: str) -> None:
    """Raise ModuleOff for the first of *pkg_names* the company turned off. *settings*
    None means no company to judge (it no longer exists), which turns nothing off."""
    for name in pkg_names:
        if not uses_module(settings, name):
            raise ModuleOff(name)


def require_on(settings: dict | None, *pkg_names: str) -> None:
    """Raise ModuleOff for the first of *pkg_names* the company turned off, or that
    this process has not started."""
    require_company_uses(settings, *pkg_names)
    for name in pkg_names:
        if not is_running(name):
            raise ModuleOff(name, starting=True)


async def company_settings(company_id) -> dict | None:
    """The stored settings of *company_id*, None when there is no such company."""
    import uuid

    from celerp.models.company import Company
    try:
        cid = uuid.UUID(str(company_id))
    except ValueError:
        return None
    async with get_session_ctx() as session:
        company = await session.get(Company, cid)
        return dict(company.settings or {}) if company is not None else None


def context_company(*args: Any, **kwargs: Any) -> str | None:
    """The company a connector method works for: its ConnectorContext's."""
    from celerp.connectors.base import ConnectorContext
    for value in (*args, *kwargs.values()):
        if isinstance(value, ConnectorContext):
            return value.company_id
    return None


def judge_writes(company_id, *pkg_names: str) -> None:
    """Have every write the running work makes for *company_id* judge *pkg_names* again
    (company_lock.judge_module_writes). For a company the work reaches after admission (a payment
    recorded on another company's invoice); it ends with the work."""
    cid = str(company_id)
    held = _IN_USE.get()
    _IN_USE.set({**held, cid: held.get(cid, frozenset()) | frozenset(pkg_names)})


def refuse_writes(exc: Exception) -> None:
    """Note that a write of the running work was refused for a module its company no
    longer uses: the work ends with this refusal (``module_use``)."""
    refusals = _REFUSALS.get()
    if refusals is not None:
        refusals.append(exc)


def modules_in_use(company_id) -> frozenset[str]:
    """The modules the running work relies on for *company_id*, empty outside such work."""
    return _IN_USE.get().get(str(company_id), frozenset())


@asynccontextmanager
async def module_use(company_id, *pkg_names: str):
    """Run the block as work inside *pkg_names* for *company_id*: refused with ModuleOff,
    before anything is done, while that company cannot use any of them (``require_on``)
    or when no company is named, since whether a module runs in this process says
    nothing about who may use it; refused with ModuleBusy while one of them is being
    turned off for the company, or modules are being changed, past the request lock
    timeout. The modules work already admitted for the company relies on are judged
    without waiting, since a change turning one of them off waits for that same work;
    the module-state boundary is likewise taken only by the company's outermost work."""
    cid = str(company_id or "")
    if not cid:
        raise ModuleOff(pkg_names[0])
    outer = _IN_USE.get()
    fresh = sorted(set(pkg_names) - outer.get(cid, frozenset()))
    counted: list[tuple[str, str]] = []
    token = _IN_USE.set(outer)
    refusals = _REFUSALS.get()
    refusals_token = _REFUSALS.set([]) if refusals is None else None
    try:
        if fresh:
            async with get_session_ctx() as session:
                for name in fresh:
                    with refused_past_lock_timeout(ModuleBusy(name)):
                        await share_module_gate(session, cid, name)
                if cid not in outer:
                    with refused_past_lock_timeout(ModuleBusy(fresh[0])):
                        await share_module_state(session)
                require_on(await company_settings(cid), *pkg_names)
                for name in fresh:
                    _RUNNING[cid, name] += 1
                    counted.append((cid, name))
        else:
            require_on(await company_settings(cid), *pkg_names)
        judge_writes(cid, *pkg_names)
        yield
        if refused := _REFUSALS.get():
            raise refused[0]
    finally:
        _IN_USE.reset(token)
        if refusals_token is not None:
            _REFUSALS.reset(refusals_token)
        for key in counted:
            _RUNNING[key] -= 1
            if not _RUNNING[key]:
                del _RUNNING[key]


async def settle_module_use(company_id, pkg_name: str) -> None:
    """For a caller about to turn *pkg_name* off for *company_id*, holding its gate
    (registry.hold_module_gate): wait for the company's work in *pkg_name* admitted before
    it to finish. 409 when some is still running after MODULE_SETTLE_SECONDS."""
    key = (str(company_id), pkg_name)
    deadline = asyncio.get_running_loop().time() + MODULE_SETTLE_SECONDS
    while _RUNNING.get(key):
        if asyncio.get_running_loop().time() >= deadline:
            raise turn_off_busy(pkg_name)
        await asyncio.sleep(0.05)


def turn_off_busy(pkg_name: str) -> HTTPException:
    """The 409 for turning *pkg_name* off while the company's work in it is still running."""
    label = module_label(pkg_name)
    return HTTPException(status_code=409, detail=refusal(
        "modules.turn_off_busy",
        f"The {label} module is still finishing work for your company, such as a store sync or a payment. "
        "Try again in a minute.",
        module=label))


def needs_modules(*pkg_names: str, company: Callable[..., Any] = context_company):
    """Mark an async function as work inside *pkg_names* (``module_use``) for the company
    *company* reads from its arguments."""
    def wrap(fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            async with module_use(company(*args, **kwargs), *pkg_names):
                return await fn(*args, **kwargs)
        guarded.needs_modules = pkg_names
        return guarded
    return wrap


def needs_modules_for(param: str, needed: Mapping[Any, tuple[str, ...]]):
    """Mark an async function as work inside the modules *needed* names for the value of
    its argument *param* (``module_use``, for the company in its ``company_id``
    argument). A value *needed* does not name needs no module."""
    def wrap(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            named = signature.bind_partial(*args, **kwargs).arguments
            names = needed.get(named[param], ())
            async with module_use(named["company_id"], *names) if names else nullcontext():
                return await fn(*args, **kwargs)
        return guarded
    return wrap
