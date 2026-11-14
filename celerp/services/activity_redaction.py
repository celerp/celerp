# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Role-gated cost redaction for activity/history payloads.

Cost (``cost_price`` / ``cost_total`` and any cost price list) is visible only to manager+
everywhere else in the app (item detail, list, dashboard cost basis). The ledger/activity feeds must honour
the same rule, enforced **server-side** so raw cost never ships to an under-threshold
role. This is the single source of truth for that redaction: every event leaves the server
through :func:`serialize_event`, which redacts each of its JSON payloads (data and metadata).

Every ledger reader (the event list, a single event, the dashboard activity and History)
shows an event only to a role holding the permission it is read with: the one its module names
on its ``projection_handler`` contribution, enabled or not, or core's own for the events no
module owns.
"""

from __future__ import annotations

from functools import cache

from sqlalchemy import ARRAY, JSON, ColumnElement, Select, Text, bindparam, case, false, func, text, true
from sqlalchemy import inspect as sa_inspect

from celerp.events.schemas import ItemPricingSet
from celerp.services.cost_visibility import COST_DERIVED_ITEM_KEYS, COST_ITEM_KEYS
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import COST_PRICE_LIST_NAMES, is_cost_price_type

# Every amount an item.pricing.set carries (all its fields but price_type), so an amount
# field added to the event later is redacted with the rest.
_PRICING_AMOUNT_KEYS = frozenset(ItemPricingSet.model_fields) - {"price_type"}

# The one cost-key registry for event data: item cost and its companions (the keys the item
# API strips), COGS on fulfillment, manufacturing unit cost and transform cost totals. A key
# starting with ``cost_`` or naming a cost price list is a cost key too. Sell prices
# (retail_price etc.) are deliberately excluded - they are visible to all roles, matching
# item-level visibility.
COST_DATA_KEYS = COST_ITEM_KEYS | COST_DERIVED_ITEM_KEYS | frozenset(
    {"total_cogs", "cogs", "unit_cost", "parent_cost_total", "child_cost_total"})


def is_cost_key(key: str) -> bool:
    """Whether a key of event data, at any depth, holds a cost."""
    return key in COST_DATA_KEYS or key.startswith("cost_") or is_cost_price_type(key)

# The events no module owns, each read with the permission of the records it changes. The
# longest prefix an event type starts with decides.
_CORE_EVENT_PERMISSIONS = {
    "sys.older_stock.": "view_inventory",
    "sys.": "manage_company_settings",
    "payment_batch.": "view_payments",
    "line_action.": "view_documents",
    "sub.": "view_subscriptions",
    "bom.": "manage_manufacturing",
}
# An event that neither an installed module nor core names (a removed module's) is read by
# those who manage the company.
_UNOWNED_EVENT_PERMISSION = "manage_company_settings"


def _event_permissions() -> list[tuple[str, str]]:
    """(prefix, permission) for every known event prefix, longest first."""
    from celerp.modules.loader import event_read_permissions

    found = {**_CORE_EVENT_PERMISSIONS, **event_read_permissions()}
    return sorted(found.items(), key=lambda kv: -len(kv[0]))


def can_read_event(event_type: str, settings: dict | None, role: str | None) -> bool:
    """Whether this role may read an event of ``event_type``."""
    permission = next((perm for prefix, perm in _event_permissions() if event_type.startswith(prefix)),
                      _UNOWNED_EVENT_PERMISSION)
    return role_has_permission(settings, role or "", permission)


def readable_events(stmt: Select, settings: dict | None, role: str | None) -> Select:
    """``stmt`` over ledger entries narrowed to the events this role can read."""
    from celerp.models.ledger import LedgerEntry

    held = lambda perm: role_has_permission(settings, role or "", perm)  # noqa: E731
    rules = [(LedgerEntry.event_type.startswith(prefix, autoescape=True), true() if held(perm) else false())
             for prefix, perm in _event_permissions()]
    return stmt.where(case(*rules, else_=true() if held(_UNOWNED_EVENT_PERMISSION) else false()))


# Every key and scalar value of an event's data a role without costs may search by: a walk of
# the data that never enters a cost key, so nothing at or below one is matched. The key test is
# is_cost_key in SQL, over the same registry.
_VISIBLE_DATA_MATCH = """EXISTS (
  WITH RECURSIVE walk(k, v) AS (
    SELECT ''::text, CAST({data} AS jsonb)
    UNION ALL
    SELECT c.k, c.v FROM walk CROSS JOIN LATERAL (
      SELECT e.key, e.value FROM jsonb_each(CASE WHEN jsonb_typeof(walk.v) = 'object'
                                                 THEN walk.v ELSE '{{}}'::jsonb END) AS e
      WHERE NOT (e.key = ANY(:cost_keys) OR e.key LIKE 'cost\\_%'
                 OR lower(e.key) = ANY(:cost_lists)
                 OR (e.key LIKE '%\\_price' AND lower(substr(e.key, 1, length(e.key) - 6)) = ANY(:cost_lists)))
      UNION ALL
      SELECT '', a.value FROM jsonb_array_elements(CASE WHEN jsonb_typeof(walk.v) = 'array'
                                                        THEN walk.v ELSE '[]'::jsonb END) AS a
    ) AS c(k, v)
  )
  SELECT 1 FROM walk
  WHERE strpos(lower(walk.k), :needle) > 0
     OR (jsonb_typeof(walk.v) NOT IN ('object', 'array') AND strpos(lower(walk.v #>> '{{}}'), :needle) > 0)
)"""


def event_data_matches(needle: str, settings: dict | None, role: str | None) -> ColumnElement[bool]:
    """Whether an event's data holds ``needle`` (case-insensitive) where this role can see it.
    A role that sees costs matches the whole data; any other role matches only keys and values
    outside every cost key, at any depth. Every text search over event data uses this."""
    from celerp.models.ledger import LedgerEntry

    ql = needle.lower()
    if can_see_costs(settings, role):
        return func.lower(LedgerEntry.data.cast(Text)).contains(ql, autoescape=True)
    data = f"{LedgerEntry.__tablename__}.{LedgerEntry.data.key}"
    return text(_VISIBLE_DATA_MATCH.format(data=data)).bindparams(
        bindparam("cost_keys", sorted(COST_DATA_KEYS), type_=ARRAY(Text)),
        bindparam("cost_lists", sorted(COST_PRICE_LIST_NAMES), type_=ARRAY(Text)),
        bindparam("needle", ql, type_=Text),
    )


def can_see_costs(settings: dict | None, role: str | None) -> bool:
    """True when the role holds view_inventory_costs for this company."""
    return role_has_permission(settings, role or "", "view_inventory_costs")


def _without_costs(value):
    """``value`` with every cost key dropped from every dict at every depth, through lists."""
    if isinstance(value, dict):
        return {k: _without_costs(v) for k, v in value.items() if not is_cost_key(k)}
    if isinstance(value, list):
        return [_without_costs(v) for v in value]
    return value


def redact_event_costs(event_type: str, data: dict) -> dict:
    """A copy of ``data`` with every cost amount stripped; rows are kept (counts and pagination
    stay intact), only cost numbers go. An ``item.pricing.set`` for a cost price type loses every
    amount and is marked ``cost_redacted``; any other event loses every cost key at every depth
    of its data (line items, attributes, old and new values alike).

    Read-time only: projection replay reads the raw ledger directly, so cost stays intact for
    state rebuilds; this keeps cost out of what a role without view_inventory_costs reads."""
    if not isinstance(data, dict):
        return data
    if event_type == "item.pricing.set" and is_cost_price_type(str(data.get("price_type") or "")):
        out = {k: v for k, v in data.items() if k not in _PRICING_AMOUNT_KEYS}
        out["cost_redacted"] = True
        return out
    return _without_costs(data)


def event_data_for_role(event_type: str, data, settings: dict | None, role: str | None):
    """One JSON payload of an event as this role reads it: whole for a role that sees costs, else
    redacted."""
    return data if can_see_costs(settings, role) else redact_event_costs(event_type, data)


@cache
def _payload_columns() -> tuple[tuple[str, str], ...]:
    """(attribute, column name) of every JSON column of a ledger entry: its data and its metadata,
    and any JSON column added later."""
    from celerp.models.ledger import LedgerEntry

    return tuple((attr.key, attr.columns[0].name) for attr in sa_inspect(LedgerEntry).column_attrs
                 if isinstance(attr.columns[0].type, JSON))


def serialize_event(entry, settings: dict | None, role: str | None) -> dict:
    """A ledger entry as this role reads it, every JSON payload (data, metadata) through the cost
    redaction. Every reader that returns events (the ledger list and a record's History, a single
    event, the dashboard activity and its search) serializes them here."""
    from celerp.services.ledger_display import entry_ts

    payloads = {}
    for attr, name in _payload_columns():
        value = getattr(entry, attr)
        payloads[name] = event_data_for_role(entry.event_type, value if isinstance(value, dict) else {},
                                             settings, role)
    return {"id": entry.id, "entity_id": entry.entity_id, "entity_type": entry.entity_type,
            "event_type": entry.event_type, "ts": entry_ts(entry, settings), **payloads}


def fields_for_role(fields, settings: dict | None, role: str | None) -> list[str]:
    """Changed-field names as this role may filter or choose by: no cost field for a role that
    cannot see costs."""
    return list(fields) if can_see_costs(settings, role) else [f for f in fields if not is_cost_key(f)]
