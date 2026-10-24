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

- admission (``module_use``) judges the company's settings under the module-state
  boundary in shared mode, and counts the work as running before letting go of it;
- turning a module off takes that boundary, then waits for the company's running
  work to finish (``settle_module_use``), so nothing admitted before it, a request
  to a store included, happens after it;
- every write the work makes judges the settings again, holding them as they are
  (company_lock.judge_module_writes, from events.engine.emit_event and
  company_lock.lock_company), so a change committed elsewhere is never written past,
  and the work ends refused even when it caught the refusal.

Lock order: the module-state boundary, then the company's module settings, then the
company, then its rows. Admission ends before the work starts, so the boundary is
never held across a request to another system."""
from __future__ import annotations

import asyncio
import functools
import inspect
from collections import Counter
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Callable, Mapping

from fastapi import HTTPException

from celerp.accounting_roles import refusal
from celerp.db import get_session_ctx
from celerp.modules.loader import is_running, module_label
from celerp.modules.registry import share_module_state, uses_module

# The companies whose module use the running work was admitted for, each with the
# modules it relies on; every write the work makes judges them again (judge_writes).
_IN_USE: ContextVar[dict[str, frozenset[str]]] = ContextVar("celerp_module_use", default={})
# The refusals the running work's writes met (refuse_writes), shared by nested work: a
# caller that catches one and carries on still ends refused, never as if it had written.
_REFUSALS: ContextVar[list[Exception] | None] = ContextVar("celerp_module_refusals", default=None)
# Admitted work running in this process, per company (module_use, settle_module_use).
_RUNNING: Counter[str] = Counter()
# How long turning a module off waits for the company's running work to finish.
SETTLE_SECONDS = 30.0


class ModuleOff(HTTPException):
    """A 403 naming the module. Outside a request it still reads as that one sentence,
    so a sync run or a delivery log shows the same message."""

    def __init__(self, pkg_name: str, *, starting: bool = False) -> None:
        self.module = pkg_name
        label = module_label(pkg_name)
        detail = (refusal("modules.starts_after_restart",
                          f"The {label} module starts after Celerp restarts.", module=label)
                  if starting else
                  refusal("modules.off_for_company",
                          f"The {label} module is turned off for your company.", module=label))
        super().__init__(status_code=403, detail=detail)

    def __str__(self) -> str:
        return self.detail["message"]


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
    nothing about who may use it. Work already admitted for the company is judged
    without waiting for the module-state boundary, which a change turning a module off
    holds while it waits for that same work."""
    cid = str(company_id or "")
    if not cid:
        raise ModuleOff(pkg_names[0])
    outer = _IN_USE.get()
    counted = False
    token = _IN_USE.set(outer)
    refusals = _REFUSALS.get()
    refusals_token = _REFUSALS.set([]) if refusals is None else None
    try:
        if cid in outer:
            require_on(await company_settings(cid), *pkg_names)
        else:
            async with get_session_ctx() as session:
                await share_module_state(session)
                require_on(await company_settings(cid), *pkg_names)
                _RUNNING[cid] += 1
                counted = True
        judge_writes(cid, *pkg_names)
        yield
        if refused := _REFUSALS.get():
            raise refused[0]
    finally:
        _IN_USE.reset(token)
        if refusals_token is not None:
            _REFUSALS.reset(refusals_token)
        if counted:
            _RUNNING[cid] -= 1
            if not _RUNNING[cid]:
                del _RUNNING[cid]


async def settle_module_use(company_id, pkg_name: str) -> None:
    """For a caller about to turn *pkg_name* off for *company_id*, holding the module-state
    boundary (registry.hold_module_state): wait for the company's work admitted before it
    to finish. 409 when some is still running after SETTLE_SECONDS."""
    cid = str(company_id)
    deadline = asyncio.get_running_loop().time() + SETTLE_SECONDS
    while _RUNNING.get(cid):
        if asyncio.get_running_loop().time() >= deadline:
            label = module_label(pkg_name)
            raise HTTPException(status_code=409, detail=refusal(
                "modules.turn_off_busy",
                f"The {label} module is still finishing work for your company, such as a store sync or a payment. "
                "Try again in a minute.",
                module=label))
        await asyncio.sleep(0.05)


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
