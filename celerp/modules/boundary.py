# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The refusal for work that needs a module this process is not running.

Core code that reaches into a module (an online payment recorded on an invoice, a
connector sync writing items or documents) checks here first, so the work stops at
its boundary with a message naming the module, never an import error part way
through. A module turned off for one company but running for another still runs:
what stored records mean never depends on which company turned a module off."""
from __future__ import annotations

import functools

from fastapi import HTTPException

from celerp.accounting_roles import refusal
from celerp.modules.loader import is_running, module_label


class ModuleOff(HTTPException):
    """A 422 naming the module that is off, in the words of the module-off page.
    Outside a request it still reads as that one sentence, so a sync run or a
    delivery log shows the same message."""

    def __init__(self, pkg_name: str) -> None:
        self.module = pkg_name
        label = module_label(pkg_name)
        super().__init__(status_code=422, detail=refusal(
            "modules.off_for_company",
            f"The {label} module is turned off for your company.",
            module=label))

    def __str__(self) -> str:
        return self.detail["message"]


def require_running(*pkg_names: str) -> None:
    """Raise ModuleOff for the first of *pkg_names* this process is not running."""
    for name in pkg_names:
        if not is_running(name):
            raise ModuleOff(name)


def needs_modules(*pkg_names: str):
    """Mark an async function as work inside *pkg_names*: it refuses with ModuleOff,
    before doing anything, while any of them is not running."""
    def wrap(fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            require_running(*pkg_names)
            return await fn(*args, **kwargs)
        guarded.needs_modules = pkg_names
        return guarded
    return wrap
