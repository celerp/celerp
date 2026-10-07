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
                   href_template, permission, show_on, presentation,
                   requires_connector; any other key is refused. href_template must be an app-local path (one leading
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
projection_handler Maps event-type prefixes to a handler function. Each event type has
                   one handler: no two prefixes may overlap (one equal to or starting
                   with another), within a module, across modules, or with a prefix
                   core handles itself (KERNEL_PROJECTION_PREFIXES)
on_company_created Async callback(session, company_id) fired after a new company is persisted
on_modules_ready   Async callback(session) fired once after every module has loaded
doc_finalize_hook  Async callback fired when a document is finalized, before commit
on_doc_payment     Async callback fired after a payment is recorded on a document
inventory_in_production
                   {"handler": "module.path:function"} naming
                   `async def handler(*, session, company_id) -> Decimal`, called with
                   exactly those keyword arguments: stock value an older release issued
                   to work still open, which its books still carry on the inventory
                   accounts (lot_origin.in_production). Filled by Celerp's own modules only
                   (FIRST_PARTY_SLOTS)
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

# Slots only Celerp's own (first-party) modules may fill; admission refuses any
# other module that fills one.
FIRST_PARTY_SLOTS = frozenset({"inventory_in_production"})

# projection_handler prefixes core handles itself: system events (registered at
# startup, celerp.main) and the core-folded connectors' declared prefixes.
KERNEL_PROJECTION_PREFIXES = frozenset({"sys.", "mp.", "shop.sync."})


def projection_prefixes_overlap(a: str, b: str) -> bool:
    """Whether two projection_handler prefixes could both match one event type.
    The projection engine applies the first match, so one would hide the other."""
    return a.startswith(b) or b.startswith(a)


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


def check(slot: str, contribution: dict) -> None:
    """Raise ValueError, naming the reason, when ``contribution`` cannot fill ``slot``."""
    if slot in _HANDLER_SLOTS:
        handler = contribution.get("handler")
        if not isinstance(handler, str) or ":" not in handler:
            raise ValueError(f"Slot {slot!r} needs a \"handler\" naming \"module.path:function\".")
    if slot in FIRST_PARTY_SLOTS and contribution.get("_first_party") is not True:
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

    for contrib in await _company_hooks(slot, kwargs):
        try:
            async with (session.begin_nested() if session is not None
                        else contextlib.nullcontext()):
                await resolve_handler(contrib["handler"])(**kwargs)
        except Exception as exc:
            failed.append((contrib.get("_module", "?"), f"{type(exc).__name__}: {exc}"))
            _log.exception(
                "Lifecycle hook %s from %s failed: %s",
                slot, contrib.get("_module", "?"), exc,
            )
    return failed
