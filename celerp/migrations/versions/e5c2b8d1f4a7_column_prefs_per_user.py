# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Column setup per user

Revision ID: e5c2b8d1f4a7
Revises: c4d1e7a2f9b3
Create Date: 2026-11-25

companies.settings["column_prefs"] held one list of visible item columns per category for the
whole company. It now holds each user's column setup for every table, keyed by user id, and the
item list has one setup for all categories. Each company-wide list is copied into the item list
setup of every user of the company who has not saved one, then removed: the all-items list
gives the column order and hides every column it left out, and a category's list decides that
category's own columns and adds them to the order. Running it again finds no list left.

Downgrade changes nothing. The per-user setups cannot be merged back into one company-wide
list, so they stay where the older version does not read them, and it opens the item list with
its default columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.migrations._json_compat import update_company_settings
from celerp.services.field_schema import effective_field_schema

revision = "e5c2b8d1f4a7"
down_revision = "c4d1e7a2f9b3"
branch_labels = None
depends_on = None

ALL_ITEMS = "__all__"  # the list the older version kept for the item list with no category chosen


def _item_list_layout(settings: dict, lists: dict) -> dict:
    """The one item list setup that the company-wide ``lists`` (category -> visible keys) give."""
    def keys(fields) -> list[str]:
        return [f["key"] for f in fields or [] if isinstance(f, dict) and isinstance(f.get("key"), str)]

    def named(value) -> list[str]:
        return [key for key in value if isinstance(key, str)]

    shared = keys(effective_field_schema(settings))
    everything = named(lists.get(ALL_ITEMS) or [])
    order = list(dict.fromkeys(everything))
    vis = {key: key in everything for key in shared} if everything else {}
    category_fields = settings.get("category_schemas")
    for category in sorted(c for c in lists if c != ALL_ITEMS):
        shown = named(lists[category])
        own = keys((category_fields if isinstance(category_fields, dict) else {}).get(category))
        vis.update({key: key in shown for key in own if key not in shared})
        order.extend(key for key in shown if key not in order)
    return {"order": order, "vis": {**vis, **{key: vis.get(key, True) for key in order}}}


def _copy_to_each_user(members: dict[str, list[str]]):
    def mutate(settings: dict, row) -> bool:
        prefs = settings.get("column_prefs") if isinstance(settings, dict) else None
        if not isinstance(prefs, dict):
            return False
        lists = {key: value for key, value in prefs.items() if isinstance(value, list)}
        if not lists:
            return False
        per_user = {key: value for key, value in prefs.items() if isinstance(value, dict)}
        layout = _item_list_layout(settings, lists)
        if layout["order"]:
            for user in members.get(str(row["id"]), ()):
                per_user[user] = {"inventory": layout, **per_user.get(user, {})}
        settings["column_prefs"] = per_user
        return True

    return mutate


def upgrade() -> None:
    conn = op.get_bind()
    members: dict[str, list[str]] = {}
    for company, user in conn.execute(sa.text("SELECT company_id, user_id FROM user_companies ORDER BY company_id, user_id")):
        members.setdefault(str(company), []).append(str(user))
    update_company_settings(conn, _copy_to_each_user(members), extra_cols=("id",))


def downgrade() -> None:
    pass
