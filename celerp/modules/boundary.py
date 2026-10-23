# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The one refusal for work in a module a company cannot use right now.

A module's routes, an online payment recorded on an invoice, a connector sync writing
items or documents, connecting a store and choosing a deposit account all check here,
so the work stops at its boundary with a message naming the module, never an import
error part way through. Two states are refused, each in its own words:

- the company turned the module off;
- the company uses it, but this process has not started it yet (a business type or a
  module was just turned on and Celerp has not restarted)."""
from __future__ import annotations

import functools
from typing import Any, Callable

from fastapi import HTTPException

from celerp.accounting_roles import refusal
from celerp.db import get_session_ctx
from celerp.modules.loader import is_running, module_label
from celerp.modules.registry import uses_module


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


def needs_modules(*pkg_names: str, company: Callable[..., Any] = context_company):
    """Mark an async function as work inside *pkg_names* for the company *company*
    reads from its arguments: it refuses with ModuleOff, before doing anything, while
    that company cannot use any of them (``require_on``), and when it names no company,
    since whether a module runs in this process says nothing about who may use it."""
    def wrap(fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            company_id = company(*args, **kwargs)
            if not company_id:
                raise ModuleOff(pkg_names[0])
            require_on(await company_settings(company_id), *pkg_names)
            return await fn(*args, **kwargs)
        guarded.needs_modules = pkg_names
        return guarded
    return wrap
