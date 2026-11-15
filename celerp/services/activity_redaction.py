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
on the ``projection_handler`` contribution of a loaded module, or core's own for the events
no loaded module owns.
"""

from __future__ import annotations

from functools import cache

from sqlalchemy import (ColumnElement, Select, Text, and_, bindparam, case, cast, false, func, literal,
                        literal_column, or_, select, text, true)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from celerp.services.cost_visibility import COST_DERIVED_ITEM_KEYS, COST_ITEM_KEYS
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import COST_PRICE_LIST_NAMES, is_cost_price_type

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


# The events that are a cost as a whole, each with the only fields of its data a reader who
# cannot see costs is shown: every other field, an amount under any name, a field of an older
# shape or one added later, stays out. A price set is one unless it names a sell price.
_COST_EVENT_FIELDS = {
    "item.pricing.set": frozenset({"price_type"}),
    "item.cost_adjusted": frozenset({"manufacturing_order_id"}),
    "item.landed_cost.allocated": frozenset({"source_bill_id", "kind"}),
}
_PRICE_TYPED_EVENT = "item.pricing.set"


def cost_event_fields(event_type: str, data: dict) -> frozenset[str] | None:
    """The only fields shown of an event that is a cost as a whole, or None for any other event."""
    fields = _COST_EVENT_FIELDS.get(event_type)
    if event_type == _PRICE_TYPED_EVENT:
        price_type = data.get("price_type")
        if isinstance(price_type, str) and not is_cost_price_type(price_type):
            return None
    return fields


def _cost_price_type_sql(price_type: ColumnElement) -> ColumnElement[bool]:
    """is_cost_price_type in SQL."""
    suffix = "_price"
    stem = case((func.right(price_type, len(suffix)) == suffix, func.left(price_type, -len(suffix))),
                else_=price_type)
    return or_(price_type.in_(sorted(COST_ITEM_KEYS)), func.lower(stem).in_(sorted(COST_PRICE_LIST_NAMES)))


def _cost_key_sql(key: ColumnElement) -> ColumnElement[bool]:
    """is_cost_key in SQL."""
    prefix = "cost_"
    return or_(key.in_(sorted(COST_DATA_KEYS)), func.left(key, len(prefix)) == prefix, _cost_price_type_sql(key))


def _only(data: ColumnElement, fields: frozenset[str]) -> ColumnElement:
    """The top-level ``fields`` of a jsonb object, as a jsonb object."""
    each = func.jsonb_each(case((func.jsonb_typeof(data) == "object", data),
                                else_=cast(literal("{}"), JSONB))).table_valued("key", "value")
    picked = select(func.jsonb_object_agg(each.c.key, each.c.value)).where(each.c.key.in_(sorted(fields)))
    return func.coalesce(picked.scalar_subquery(), cast(literal("{}"), JSONB))


def _shown_top(data: ColumnElement, event_type: ColumnElement) -> ColumnElement:
    """cost_event_fields in SQL: the jsonb data of an event as a reader without costs is shown it at
    its top level (a cost event's own fields only, any other event whole)."""
    whens = []
    for kind, fields in _COST_EVENT_FIELDS.items():
        when = event_type == kind
        if kind == _PRICE_TYPED_EVENT:
            price_type = data.op("->")("price_type")
            when = and_(when, or_(func.jsonb_typeof(price_type).is_distinct_from("string"),
                                  _cost_price_type_sql(data.op("->>")("price_type"))))
        whens.append((when, _only(data, fields)))
    return case(*whens, else_=data)


def shown_data(settings: dict | None, role: str | None) -> ColumnElement:
    """An event's data as jsonb at its top level as this role is shown it (redact_event_costs for
    the top level; cost keys below it are each reader's own to leave out)."""
    from celerp.models.ledger import LedgerEntry

    data = cast(LedgerEntry.data, JSONB)
    return data if can_see_costs(settings, role) else _shown_top(data, LedgerEntry.event_type)


def _sql(expr: ColumnElement) -> str:
    """``expr`` as Postgres SQL with its (constant) values inline."""
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# Every key and scalar value of an event's data a role may search by: a walk from the data the role
# is shown at the top level that, for a role without costs, never enters a cost key, so nothing a
# serialized event leaves out is matched.
_DATA_MATCH = """EXISTS (
  WITH RECURSIVE walk(k, v) AS (
    SELECT ''::text, {root}
    UNION ALL
    SELECT c.k, c.v FROM walk CROSS JOIN LATERAL (
      SELECT e.key, e.value FROM jsonb_each(CASE WHEN jsonb_typeof(walk.v) = 'object'
                                                 THEN walk.v ELSE '{{}}'::jsonb END) AS e
      WHERE NOT ({hidden_key})
      UNION ALL
      SELECT '', a.value FROM jsonb_array_elements(CASE WHEN jsonb_typeof(walk.v) = 'array'
                                                        THEN walk.v ELSE '[]'::jsonb END) AS a
    ) AS c(k, v)
  )
  SELECT 1 FROM walk
  WHERE strpos(lower(walk.k), :needle) > 0
     OR (jsonb_typeof(walk.v) NOT IN ('object', 'array') AND strpos(lower(walk.v #>> '{{}}'), :needle) > 0)
)"""


@cache
def _data_match_sql(costs: bool) -> str:
    from celerp.models.ledger import LedgerEntry

    data = cast(LedgerEntry.data, JSONB)
    if costs:
        return _DATA_MATCH.format(root=_sql(data), hidden_key="false")
    return _DATA_MATCH.format(root=_sql(_shown_top(data, LedgerEntry.event_type)),
                              hidden_key=_sql(_cost_key_sql(literal_column("e.key", Text))))


def event_data_matches(needle: str, settings: dict | None, role: str | None) -> ColumnElement[bool]:
    """Whether an event's data holds ``needle`` (case-insensitive) in a key or value this role is
    shown of it (serialize_event). Every text search over event data uses this."""
    return text(_data_match_sql(can_see_costs(settings, role))).bindparams(
        bindparam("needle", needle.lower(), type_=Text))


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
    """A copy of ``data`` with every cost stripped; rows are kept (counts and pagination stay
    intact), only cost goes. An event that is a cost as a whole keeps only its own plain fields
    (cost_event_fields) and is marked ``cost_redacted``; any other event loses every cost key at
    every depth of its data (line items, attributes, old and new values alike).

    Read-time only: projection replay reads the raw ledger directly, so cost stays intact for
    state rebuilds; this keeps cost out of what a role without view_inventory_costs reads."""
    if not isinstance(data, dict):
        return data
    fields = cost_event_fields(event_type, data)
    if fields is None:
        return _without_costs(data)
    return {**{k: _without_costs(v) for k, v in data.items() if k in fields}, "cost_redacted": True}


def serialize_event(entry, settings: dict | None, role: str | None) -> dict:
    """A ledger entry as this role reads it: for a role without costs its data through
    redact_event_costs and its metadata with every cost key dropped at every depth. Every reader
    that returns events (the ledger list and a record's History, a single event, the dashboard
    activity and its search) serializes them here."""
    from celerp.services.ledger_display import entry_ts

    data = entry.data if isinstance(entry.data, dict) else {}
    metadata = entry.metadata_ if isinstance(entry.metadata_, dict) else {}
    if not can_see_costs(settings, role):
        data, metadata = redact_event_costs(entry.event_type, data), _without_costs(metadata)
    return {"id": entry.id, "entity_id": entry.entity_id, "entity_type": entry.entity_type,
            "event_type": entry.event_type, "ts": entry_ts(entry, settings), "data": data,
            "metadata": metadata}


def fields_for_role(fields, settings: dict | None, role: str | None) -> list[str]:
    """Changed-field names as this role may filter or choose by: no cost field for a role that
    cannot see costs."""
    return list(fields) if can_see_costs(settings, role) else [f for f in fields if not is_cost_key(f)]
