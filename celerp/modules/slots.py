# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Slot registry for the Celerp module system.

Slots are named extension points in core UI and API that modules can fill.
Core checks each slot at render/startup time and injects module contributions.

Defined slots
-------------
These are every slot core or a bundled module reads; a slot name not listed here
is ignored at load time.

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
search_provider    Contributes rows to the global search bar. Exactly one descriptor
                   dict per module: {"handler", "result_key", "permission"}. handler
                   names an in-module "module.path:function" resolved and validated
                   async at load, invoked as
                   `async def handler(session, company_id, role, q, limit) -> dict`
                   returning {result_key: [rows]}; result_key is "items" or "entries";
                   permission gates the source per company role. See the module loader
                   for the full descriptor contract.

SLOT_ACCESS below says which of these third-party modules may fill (public) and
which are filled only by Celerp's own first-party modules (internal). The loader
enforces it, and the module template's linter mirrors it.

Every slot entry is checked when its module loads (celerp.modules.loader): an
entry is a dict; a "permission" (or catalog_channel "write_permission"), when
present, is a key from the permission registry; nav "href" / "settings_href" and
bulk_action "form_action" (required) are app-local paths; and every callable an
entry names resolves to the module's own code, async exactly where core awaits it.

item_action, pricing_action, doc_detail_actions, doc_detail_badges, bulk_action,
send_to_targets and catalog_channel are shown only when the company has the
contributing module switched on and the role holds the entry's "permission"
(ui.module_slots); the sidebar applies the same permission rule to nav. Hiding is
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

# Who may fill each slot. "public": any module, the third-party extension surface.
# "internal": first-party modules only - these run inside core's own transactions
# (finalize, payment), rebuild core projections, or feed core-only flows (send-to
# document types, connector catalog channels). The module template's SLOT_NAMES
# mirrors this table.
SLOT_ACCESS: dict[str, str] = {
    "nav": "public",
    "bulk_action": "public",
    "item_action": "public",
    "pricing_action": "public",
    "doc_detail_actions": "public",
    "doc_detail_badges": "public",
    "search_provider": "public",
    "category_schema": "public",
    "on_company_created": "public",
    "on_modules_ready": "public",
    "projection_handler": "internal",
    "doc_finalize_hook": "internal",
    "on_doc_payment": "internal",
    "send_to_targets": "internal",
    "catalog_channel": "internal",
}

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


def clear() -> None:
    """Clear all registered slots. Used in tests only."""
    _slots.clear()


def all_slots() -> dict[str, list[dict]]:
    """Return a snapshot of all registered slots. Used in tests and diagnostics."""
    return {k: list(v) for k, v in _slots.items()}


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

    for contrib in get(slot):
        handler_path = contrib.get("handler")
        if not handler_path:
            continue
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
