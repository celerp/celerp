# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Read-only inventory search building blocks and the global-search provider.

The inventory list route and the cross-app global-search bar both need the same
core pipeline: load every item projection for a company, flatten it to the
schema-driven shape, strip fields the requesting role may not see, and match a
free-text query against the per-category searchable field sets. That pipeline
lives here as small composable phases so it exists exactly once.

The list route (celerp_inventory.routes.list_items) has list-only concerns the
global bar does not - status modes other than "all", contact holdings scope,
sold pricing, attribute facets, attr.* column filters, sku/barcode filters, the
low_stock semantic filter, user column sort, and value totals. Those stay in the
route and interleave between these phases; the phases below are the shared subset
both callers compose, so none of the load/flatten/visibility/q-match code is
duplicated.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp.services.cost_visibility import apply_field_visibility
from celerp.services.field_schema import get_effective_field_schema
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import get_price_config
from celerp.services.units import get_company_units

from .routes import (
    _DEFAULT_NUMERIC_FIELDS,
    _DEFAULT_TEXT_FIELDS,
    DERIVED_FIELD_DEPS,
    flatten_item,
    query_match_reasons,
    searchable_field_sets,
)


async def load_item_rows(
    session: AsyncSession, company_id
) -> list[Projection]:
    """Load every item projection for the company, once.

    This is the single dominant inventory read. Both the list route and the
    global-search provider load through here and then flatten the same rows, so
    a request never reads the full item projection set twice (which also kept
    the raw rows and the flattened result from drifting across two READ
    COMMITTED snapshots).
    """
    return (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()


async def flatten_item_rows(
    session: AsyncSession, company_id, rows: list[Projection]
) -> list[dict]:
    """Flatten already-loaded item projections to the schema-driven shape, with
    location names, price config, and unit map applied.

    Takes the rows from `load_item_rows` rather than reloading them, so the
    caller keeps the raw projections available (holdings/sold scopes need them)
    while the flatten runs exactly once over the same snapshot. This is the
    pre-visibility shared front of the pipeline: no status, contact, or query
    filtering, so both callers start from an identical flattened set.
    """
    from celerp.models.company import Location

    loc_rows = (
        await session.execute(
            select(Location).where(Location.company_id == company_id)
        )
    ).scalars().all()
    loc_map = {str(r.id): r.name for r in loc_rows}

    price_config = await get_price_config(session, company_id)
    units = await get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    return [
        flatten_item(
            r.state, r.entity_id,
            location_id=str(r.location_id) if r.location_id else None,
            location_name=loc_map.get(str(r.location_id)) if r.location_id else None,
            created_at=r.created_at,
            updated_at=r.updated_at,
            price_config=price_config,
            unit_map=unit_map,
        )
        for r in rows
    ]


async def strip_field_visibility(
    session: AsyncSession, company_id, role: str, result: list[dict]
) -> tuple[list[dict], dict[str, tuple[frozenset[str], frozenset[str]]]]:
    """Strip every item down to the fields the requesting role may see, per the
    item's own category schema, and return the searchable numeric/text field sets
    keyed by item id (used by the q-match phase).

    Cost keys are gated by view_inventory_costs; a draft's cost survives for a
    role that can author drafts (edit_inventory). Returns (stripped_result,
    item_field_sets). The per-category schema is resolved once per distinct
    category and cached.
    """
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    settings = (company.settings if company else {}) or {}
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    can_author_drafts = role_has_permission(settings, role, "edit_inventory")

    schema_cache: dict[str | None, tuple[list[dict], frozenset[str], frozenset[str]]] = {}

    async def _category_ctx(cat: str | None):
        if cat not in schema_cache:
            fs = await get_effective_field_schema(session, company_id, category=cat)
            num, txt = searchable_field_sets(fs)
            schema_cache[cat] = (fs, num, txt)
        return schema_cache[cat]

    item_field_sets: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    stripped: list[dict] = []
    for r in result:
        cat = r.get("category")
        fs, num, txt = await _category_ctx(cat)
        item_field_sets[r.get("id")] = (num, txt)
        # Fill the inventory_type default on the ALLOWED side of the visibility
        # boundary, mirroring the projection-time default so a legacy projection
        # missing the key still carries the real-or-default value for a role that
        # may see it. A restricted role has the key removed by the strip below.
        r.setdefault("inventory_type", "stocked")
        stripped.append(apply_field_visibility(
            [r], role, fs, can_see_costs,
            can_author_drafts=can_author_drafts,
            derived_field_deps=DERIVED_FIELD_DEPS,
        )[0])
    return stripped, item_field_sets


def apply_query_match(
    result: list[dict], q: str,
    item_field_sets: dict[str, tuple[frozenset[str], frozenset[str]]],
) -> list[dict]:
    """Keep only items matching the search grammar and attach each item's q_match.

    Grammar: comma = OR groups, & = AND terms, lo-hi = numeric range, bare number
    = numeric-exact OR text, else text substring (query_match_reasons). Each item
    is matched against its own category's numeric/text field sets, so a
    number-typed category field resolves and a text-typed one is not coerced.
    Reasons are computed over the visibility-stripped dict, so every cited field
    is one the role may see. Mutates the surviving dicts to add r["q_match"].
    """
    q_reasons: dict = {}
    matched: list[dict] = []
    for r in result:
        num, txt = item_field_sets.get(
            r.get("id"), (_DEFAULT_NUMERIC_FIELDS, _DEFAULT_TEXT_FIELDS)
        )
        reasons = query_match_reasons(r, q, num, txt)
        if reasons is not None:
            q_reasons[r.get("id")] = reasons
            matched.append(r)
    for r in matched:
        r["q_match"] = [
            {"field": f, "match": m} for f, m in q_reasons.get(r.get("id"), [])
        ]
    return matched


def default_order(result: list[dict]) -> None:
    """Order items most-recently-updated first, then name asc, then entity_id asc
    for stability. Two-pass: a stable name+id sub-sort, then a descending updated_at
    primary sort. Sorts in place.
    """
    result.sort(
        key=lambda item: (
            str(item.get("name") or "").lower(),
            str(item.get("entity_id") or ""),
        )
    )
    result.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def apply_item_order(
    result: list[dict], *,
    inventory_method: str | None,
    sort: str | None,
    direction: str,
    status: str | None,
) -> None:
    """Order the flattened item set exactly as the list route does, single-sourced
    so the global-search bar and the list stay in lockstep. Sorts in place.

    FEFO (when the company's inventory_method is "fefo"): available items float to
    the top by expires_at ascending, no-expiry last. An explicit user column sort
    wins for every status other than the available set; for the available set FEFO
    overrides it. With no user sort and no FEFO, the default order applies
    (most-recently-updated first, then name asc, then entity_id asc).
    """
    is_fefo = inventory_method == "fefo"
    if is_fefo:
        def _fefo_key(item: dict):
            # Items without expiry float to the bottom; expired items sort first.
            return item.get("expires_at") or "9999-99-99"
        result.sort(key=_fefo_key)

    if sort and (not is_fefo or status not in (None, "available", "")):
        reverse = direction.lower() != "asc"

        def _sort_key(item: dict):
            v = item.get(sort)
            if v is None:
                # Nulls always last regardless of direction
                return (1, "")
            if isinstance(v, (int, float)):
                return (0, v)
            # ISO date/datetime strings sort correctly as strings
            return (0, str(v).lower())

        result.sort(key=_sort_key, reverse=reverse)
    elif not sort and not is_fefo:
        default_order(result)


async def global_search(session, company_id, role, q, limit) -> dict:
    """Global-search bar contribution for inventory items (read-only).

    Reproduces the inventory list route's search behavior for the shared bar:
    status="all" (no status filtering), the same flattened item shape, the same
    role-dependent field visibility, the same q grammar with q_match attachment,
    and the same ordering. Ordering honors the company's inventory_method so a
    FEFO company's global-search hits lead with the same soonest-expiring items
    the list shows; a non-FEFO company falls to the shared default order. It takes
    no request, no attribute filters, no facets, no holdings or sold scope, and no
    column sort - just the shared pipeline, capped at ``limit`` items.
    """
    from celerp.models.company import Company

    rows = await load_item_rows(session, company_id)
    result = await flatten_item_rows(session, company_id, rows)
    # status="all" - no status filtering, matching the list route's "all" mode.
    result, item_field_sets = await strip_field_visibility(
        session, company_id, role, result
    )
    if q:
        result = apply_query_match(result, q, item_field_sets)
    company = await session.get(Company, company_id)
    inventory_method = (company.settings or {}).get("inventory_method") if company else None
    apply_item_order(
        result,
        inventory_method=inventory_method,
        sort=None,
        direction="desc",
        status="all",
    )
    return {"items": result[:limit]}
