# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Column setup per user

Revision ID: e5c2b8d1f4a7
Revises: c4d1e7a2f9b3
Create Date: 2026-11-25

companies.settings["column_prefs"] held one list of visible item columns per category for the
whole company, while each browser kept its own copy that overrode it. It now holds each user's
column setup for every table, keyed by user id. A list left from the company-wide form names a
category, never a user, so it is removed and those tables open with their default columns.
Running it again finds no list left. Forward-only data step.
"""

from __future__ import annotations

from alembic import op

from celerp.migrations._json_compat import update_company_settings

revision = "e5c2b8d1f4a7"
down_revision = "c4d1e7a2f9b3"
branch_labels = None
depends_on = None


def _drop_company_wide(settings: dict, _row) -> bool:
    prefs = settings.get("column_prefs") if isinstance(settings, dict) else None
    if not isinstance(prefs, dict):
        return False
    kept = {key: value for key, value in prefs.items() if isinstance(value, dict)}
    if kept == prefs:
        return False
    settings["column_prefs"] = kept
    return True


def upgrade() -> None:
    update_company_settings(op.get_bind(), _drop_company_wide)


def downgrade() -> None:
    pass
