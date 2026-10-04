# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Slot registry for the Celerp module system.

Slots are named extension points in core UI and API that modules can fill.
Core checks each slot at render/startup time and injects module contributions.

Defined slots
-------------
These are every slot core or a bundled module reads; a slot name not listed here
is ignored at load time, and the loader logs it as unknown.

nav                Sidebar navigation entry
bulk_action        Action in the inventory bulk toolbar
send_to_targets    Document type offered by the inventory bulk "send to" action
catalog_channel    Compact external-channel state in the inventory catalog
item_action        Button in the item detail actions panel
pricing_action     Button on rows of an item's Pricing tab. Keys: label or label_key,
                   href_template, permission, show_on, presentation; any other key
                   is refused. href_template must be an app-local path (one leading
                   /, never //, no backslash, no control character) and may use
                   {entity_id}, {price_list} and {field_name} (filled URL-encoded);
                   any other brace is refused. Optional show_on lists row traits a
                   row must all carry (editable/readonly, sell/cost,
                   manual/derived); presentation is "page" (the default and only
                   value). Validated at load.
doc_detail_actions Element on a document's detail page, from a "render" callable
                   ("module.path:function", called with the document)
doc_detail_badges  Status badge on a document's detail page, from a "render" callable
category_schema    Default field definitions for a named category
projection_handler Maps event-type prefixes to a handler function
on_company_created Async callback(session, company_id) fired after a new company is persisted
on_modules_ready   Async callback(session) fired once after every module has loaded
doc_finalize_hook  Async callback fired when a document is finalized, before commit
on_doc_payment     Async callback fired after a payment is recorded on a document
inventory_in_production
                   {"handler": "module.path:function"} naming
                   `async def handler(*, session, company_id) -> Decimal`, called with
                   exactly those keyword arguments: stock value an older release issued
                   to work still open, which its books still carry on the inventory
                   accounts
item_lineage_guard {"handler": "module.path:function"} naming
                   `async def handler(*, session, entry, transition) -> None`, called
                   with exactly those keyword arguments on every live item event, after
                   it is applied and before its effects are booked; raising refuses the
                   event
search_provider    Contributes rows to the global search bar. Exactly one descriptor
                   dict per module: {"handler", "result_key", "permission"}. handler
                   names an in-module "module.path:function" resolved and validated
                   async at load, invoked as
                   `async def handler(session, company_id, role, q, limit) -> dict`
                   returning {result_key: [rows]}; result_key is "items" or "entries";
                   permission gates the source per company role. See the module loader
                   for the full descriptor contract.

Every slot entry is checked before any of its module's code runs
(celerp.modules.loader): an entry is a dict; every key its slot reads has the
type it is read as (nav "key" text, "group" text or None, "order" a number;
send_to_targets "doc_type" and catalog_channel "id" non-empty text, catalog_channel
"can_create" true or false; category_schema "category" non-empty text and
"fields" a list of field definitions; bulk_action "action_type" "htmx" or
"navigate"; "label" and "label_key" text); a "permission" (or catalog_channel
"write_permission"), when present, is a key from the permission registry; a
"requires_connector", when set, is a connector id; nav "href" / "settings_href"
and bulk_action "form_action" (required) are app-local paths; and every callable
an entry names is the module's own code, async exactly where core awaits it,
and takes exactly the keyword arguments core passes where the slot names them
(inventory_in_production, item_lineage_guard).

Whether a company uses a module is one rule, celerp.modules.registry.uses_module.
item_action, pricing_action, doc_detail_actions, doc_detail_badges, bulk_action,
send_to_targets and catalog_channel are shown only when the company uses the
contributing module, the role holds the entry's "permission", and the company is
connected to the entry's "requires_connector", if any (ui.module_slots); the
sidebar applies the same module and permission rules to nav. A lifecycle hook
fired for one company runs only for modules that company uses, and a module's
API routes and pages refuse a company that does not use it. Hiding is
presentation: the route an entry leads to must still check the permission itself.

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

# The slots listed above: the closed set a module may fill.
SLOT_NAMES = frozenset({
    "nav", "bulk_action", "send_to_targets", "catalog_channel", "item_action",
    "pricing_action", "doc_detail_actions", "doc_detail_badges", "category_schema",
    "projection_handler", "on_company_created", "on_modules_ready", "doc_finalize_hook",
    "on_doc_payment", "search_provider", "inventory_in_production", "item_lineage_guard",
})

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


def register(slot: str, contribution: dict) -> None:
    """Register a module contribution into a named slot.

    Called by the loader for each slot declared in PLUGIN_MANIFEST["slots"].
    """
    _slots.setdefault(slot, []).append(contribution)


def get(slot: str) -> list[dict]:
    """Return all contributions registered for a slot (empty list if none)."""
    return list(_slots.get(slot, []))


def unregister_module(module: str) -> None:
    """Remove every contribution the named module registered, in every slot."""
    for slot, entries in list(_slots.items()):
        _slots[slot] = [e for e in entries if e.get("_module") != module]


def clear() -> None:
    """Clear all registered slots. Used in tests only."""
    _slots.clear()


def all_slots() -> dict[str, list[dict]]:
    """Return a snapshot of all registered slots. Used in tests and diagnostics."""
    return {k: list(v) for k, v in _slots.items()}


async def _company_hooks(slot: str, kwargs: dict) -> list[dict]:
    """The slot's handlers that apply to this call: for a hook fired for one
    company (session and company_id given), only those of modules it uses."""
    contributions = [c for c in get(slot) if c.get("handler")]
    session, company_id = kwargs.get("session"), kwargs.get("company_id")
    if session is None or company_id is None or not contributions:
        return contributions
    import uuid
    from celerp.models.company import Company
    from celerp.modules.registry import uses_module
    company = await session.get(Company, uuid.UUID(str(company_id)))
    settings = company.settings if company is not None else None
    return [c for c in contributions if uses_module(settings, c.get("_module"))]


async def fire_lifecycle(slot: str, **kwargs) -> None:
    """Invoke all async callbacks registered under a lifecycle slot.

    Each contribution must have a "handler" key pointing to a dotted path
    "module.path:function_name". The function is called with **kwargs.
    A failing hook never blocks its siblings or boot: it is logged at ERROR
    (with traceback) and swallowed, so a recurrence surfaces as an alert
    instead of vanishing.
    """
    import logging

    _log = logging.getLogger(__name__)

    for contrib in await _company_hooks(slot, kwargs):
        handler_path = contrib["handler"]
        try:
            func = resolve_handler(handler_path)
            await func(**kwargs)
        except Exception as exc:
            _log.exception(
                "Lifecycle hook %s from %s failed: %s",
                slot, contrib.get("_module", "?"), exc,
            )


async def fire_lifecycle_strict(slot_name: str, **kwargs) -> None:
    """Like fire_lifecycle but propagates HTTPException from handlers.

    Use for slots where a handler must be able to block the action.
    """
    import logging
    from fastapi import HTTPException

    _log = logging.getLogger(__name__)

    for contrib in await _company_hooks(slot_name, kwargs):
        handler_path = contrib["handler"]
        try:
            func = resolve_handler(handler_path)
            await func(**kwargs)
        except HTTPException:
            raise
        except Exception as e:
            _log.warning("Slot handler %s raised: %s", handler_path, e)
