# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Let every company of an older installation keep using every module it had.

Revision ID: t7i8j9k0l1m2
Revises: s6h7c8d9e0f1
Create Date: 2026-10-05

Before each company chose its own modules, every company used every module the
installation loaded, and companies.settings["enabled_modules"] was written by
the module toggle but never read. Read now, a list left over from then would
turn off every module it does not name. Removing it makes each company one that
has never chosen, which uses every module the installation loads: what it had.

Runs once per database. It is a data-only migration, so the version-change
reconcile replays it; the instance_meta marker makes every later run a no-op,
so a choice a company makes afterwards is never removed. Forward-only.
"""

from __future__ import annotations

from alembic import op

from celerp.migrations._data_reconcile import get_meta, set_meta
from celerp.migrations._json_compat import update_company_settings

revision = "t7i8j9k0l1m2"
down_revision = "s6h7c8d9e0f1"
branch_labels = None
depends_on = None

_DONE_KEY = "company_modules_chosen_per_company"
_SETTINGS_KEY = "enabled_modules"


def _drop_key(settings: dict, _row) -> bool:
    if isinstance(settings, dict) and _SETTINGS_KEY in settings:
        del settings[_SETTINGS_KEY]
        return True
    return False


def upgrade() -> None:
    conn = op.get_bind()
    if get_meta(conn, _DONE_KEY):
        return
    update_company_settings(conn, _drop_key)
    set_meta(conn, _DONE_KEY, revision)


def downgrade() -> None:
    pass
