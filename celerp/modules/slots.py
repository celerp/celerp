# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Slot registry for the Celerp module system.

Slots are named extension points in core UI and API that modules can fill.
Core checks each slot at render/startup time and injects module contributions.

Defined slots
-------------
nav                Sidebar navigation entry
settings_tab       Tab in the /settings page
bulk_action        Action in the inventory bulk toolbar
catalog_channel    Compact external-channel state in the inventory catalog
item_action        Button in the item detail actions panel
doc_action         Button in the document detail actions panel
dashboard_widget   Widget on the dashboard page
import_adapter     Source option in the CSV import page
category_schema    Default field definitions for a named category
projection_handler Maps event-type prefixes to a handler function
on_company_created Async callback(session, company_id) fired after a new company is persisted
inventory_in_production
                   {"handler": "module.path:function"} naming
                   `async def handler(*, session, company_id) -> Decimal`, called with
                   exactly those keyword arguments: stock value an older release issued
                   to work still open, which its books still carry on the inventory
                   accounts (lot_origin.in_production). First-party modules only.
item_lineage_guard {"handler": "module.path:function"} naming
                   `async def handler(*, session, entry, transition) -> None`, called
                   with exactly those keyword arguments on every live item event, after
                   it is applied and before its effects are booked; raising refuses the
                   event (celerp.events.engine._item_applied)
search_provider    Contributes rows to the global search bar. Exactly one descriptor
                   dict per module: {"handler", "result_key", "permission"}. handler
                   names an in-module "module.path:function" resolved and validated
                   async at load, invoked as
                   `async def handler(session, company_id, role, q, limit) -> dict`
                   returning {result_key: [rows]}; result_key is "items" or "entries";
                   permission gates the source per company role. See the module loader
                   for the full descriptor contract.

Usage in core UI
----------------
    from celerp.modules.slots import get as get_slot

    for action in get_slot("bulk_action"):
        # action is a dict from PLUGIN_MANIFEST["slots"]["bulk_action"]
        # plus "_module": module_name injected by the loader
        ...
"""
from __future__ import annotations

from typing import Callable

_slots: dict[str, list[dict]] = {}


def resolve_handler(dotted: str) -> Callable:
    """Resolve a "module.path:function" string to the callable it names.

    Raises ImportError if the module cannot be imported and AttributeError if
    the module has no such attribute. Callers wrap this in their own try/except
    to apply their failure policy (swallow, propagate, log-and-skip); the shared
    resolver never decides that policy, it only does the import and lookup.
    """
    import importlib

    module_path, func_name = dotted.rsplit(":", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, func_name)


# Slots whose handler core calls on every item write or upgrade: a contribution without a
# "module.path:function" handler would fail every such call, so it is refused here.
_HANDLER_SLOTS = frozenset({"inventory_in_production", "item_lineage_guard"})
# Slots whose answer the books are judged by: taken from first-party modules only, as the
# chart of accounts is (the loader sets "_first_party" from the module's content identity).
_FIRST_PARTY_SLOTS = frozenset({"inventory_in_production"})


def check(slot: str, contribution: dict) -> None:
    """Raise ValueError, naming the reason, when ``contribution`` cannot fill ``slot``."""
    if slot in _HANDLER_SLOTS:
        handler = contribution.get("handler")
        if not isinstance(handler, str) or ":" not in handler:
            raise ValueError(f"Slot {slot!r} needs a \"handler\" naming \"module.path:function\".")
    if slot in _FIRST_PARTY_SLOTS and contribution.get("_first_party") is not True:
        raise ValueError(f"Slot {slot!r} is filled by Celerp's own modules only.")


def register(slot: str, contribution: dict) -> None:
    """Register a module contribution into a named slot.

    Called by the loader for each slot declared in PLUGIN_MANIFEST["slots"]. A
    contribution that cannot fill the slot is refused (``check``).
    """
    check(slot, contribution)
    _slots.setdefault(slot, []).append(contribution)


def get(slot: str) -> list[dict]:
    """Return all contributions registered for a slot (empty list if none)."""
    return list(_slots.get(slot, []))


def clear() -> None:
    """Clear all registered slots. Used in tests only."""
    _slots.clear()


def all_slots() -> dict[str, list[dict]]:
    """Return a snapshot of all registered slots. Used in tests and diagnostics."""
    return {k: list(v) for k, v in _slots.items()}


async def fire_lifecycle(slot: str, **kwargs) -> list[tuple[str, str]]:
    """Invoke all async callbacks registered under a lifecycle slot.

    Each contribution must have a "handler" key pointing to a dotted path
    "module.path:function_name". The function is called with **kwargs.
    Given a ``session``, each hook runs in its own savepoint, so a hook that
    fails rolls back only its own changes. A failing hook never blocks its
    siblings or boot: it is logged at ERROR (with traceback) and swallowed, so a
    recurrence surfaces as an alert instead of vanishing. Returns each failed hook's
    module with the error it raised.
    """
    import contextlib
    import logging

    _log = logging.getLogger(__name__)
    session = kwargs.get("session")
    failed: list[tuple[str, str]] = []

    for contrib in get(slot):
        handler_path = contrib.get("handler")
        if not handler_path:
            continue
        try:
            func = resolve_handler(handler_path)
            async with session.begin_nested() if session is not None else contextlib.nullcontext():
                await func(**kwargs)
        except Exception as exc:
            failed.append((contrib.get("_module", "?"), f"{type(exc).__name__}: {exc}"))
            _log.exception(
                "Lifecycle hook %s from %s failed: %s",
                slot, contrib.get("_module", "?"), exc,
            )
    return failed


async def fire_lifecycle_strict(slot_name: str, **kwargs) -> None:
    """Like fire_lifecycle but propagates HTTPException from handlers.

    Use for slots where a handler must be able to block the action.
    """
    import logging
    from fastapi import HTTPException

    _log = logging.getLogger(__name__)

    for contrib in get(slot_name):
        handler_path = contrib.get("handler")
        if not handler_path:
            continue
        try:
            func = resolve_handler(handler_path)
            await func(**kwargs)
        except HTTPException:
            raise
        except Exception as e:
            _log.warning("Slot handler %s raised: %s", handler_path, e)
