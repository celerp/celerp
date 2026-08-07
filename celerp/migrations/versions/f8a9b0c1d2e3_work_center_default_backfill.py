# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""seed a Default work center and move the hours_per_day setting onto it

Revision ID: f8a9b0c1d2e3
Revises: e7c9a1b3d5f2
Create Date: 2026-08-06

The data half of the work-center default split. Signature-less (raw SQL), so
the develop-to-release reconcile replays it on every create_all/restore
database once the schema migration has added the columns. Seeds one Default
center per manufacturing-active company carrying the old company hours value
(the module's x-or-8.0 mapping), posts one company-wide notice that the setting
moved, and strips the now-migrated settings key. Idempotent: every step guards
against a prior application, because it replays on each version-change
reconcile and, on a restore boot, can be both alembic-applied and
reconcile-replayed in one pass. Forward-only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "e7c9a1b3d5f2"
branch_labels = None
depends_on = None

_NOTICE_TITLE = "Hours per day moved to work centers"
_NOTICE_BODY = (
    "Your company Hours per day setting now lives on the new Default work center under "
    "Settings > Manufacturing > Work centers. Nothing changed in your To-Make estimates."
)


def upgrade() -> None:
    conn = op.get_bind()

    # One dedicated Default center per manufacturing-active company, carrying its
    # old hours value. The nested CASE mirrors the module's `x or 8.0`: a stored
    # 0, blank, absent, or non-numeric value maps to 8.0. The regex guard runs
    # BEFORE the ::float cast because Postgres does not guarantee AND
    # short-circuit, so a flat `regex AND value::float` would still cast 'abc'
    # and abort the whole insert. settings is a JSON column, so the ? membership
    # test needs a jsonb cast; the -> / ->> extraction works on json directly.
    conn.execute(sa.text("""
        INSERT INTO work_centers (id, company_id, name, hours_per_day, is_default, created_at)
        SELECT gen_random_uuid(), c.id, 'Default',
               CASE WHEN (c.settings->'manufacturing'->>'hours_per_day') ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN CASE WHEN (c.settings->'manufacturing'->>'hours_per_day')::float <> 0
                              THEN (c.settings->'manufacturing'->>'hours_per_day')::float
                              ELSE 8.0 END
                    ELSE 8.0 END,
               true, NOW()
        FROM companies c
        WHERE (c.settings::jsonb ? 'manufacturing'
               OR EXISTS (SELECT 1 FROM work_centers wc WHERE wc.company_id = c.id))
          AND NOT EXISTS (SELECT 1 FROM work_centers d WHERE d.company_id = c.id AND d.is_default)
    """))

    # One company-wide notice per company that just received a Default center
    # (user_id NULL = company-wide). Guarded so a re-run never doubles it. The
    # notifications table is created when the application boots, not by the
    # migration chain, so on a database migrated before its first boot it is
    # absent. Such a database has never run the app and therefore holds no
    # company whose value could have moved, so there is nothing to notify. The
    # regclass check is unqualified so it resolves against the same search path
    # the INSERT targets (public in production), not a hardcoded schema.
    if conn.execute(sa.text("SELECT to_regclass('notifications')")).scalar() is not None:
        conn.execute(sa.text("""
            INSERT INTO notifications
                (id, company_id, user_id, category, title, body, action_url, priority, read, created_at)
            SELECT gen_random_uuid(), w.company_id, NULL, 'manufacturing', :title, :body,
                   '/settings/manufacturing', 'high', false, NOW()
            FROM work_centers w
            WHERE w.is_default
              AND w.name = 'Default'
              AND NOT EXISTS (SELECT 1 FROM notifications n
                              WHERE n.company_id = w.company_id AND n.title = :title)
        """), {"title": _NOTICE_TITLE, "body": _NOTICE_BODY})

    # Strip the now-migrated key (jsonb #- needs the cast, then back to json).
    conn.execute(sa.text(
        "UPDATE companies SET settings = (settings::jsonb #- '{manufacturing,hours_per_day}')::json "
        "WHERE settings::jsonb #> '{manufacturing,hours_per_day}' IS NOT NULL"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM work_centers WHERE is_default AND name = 'Default'"))
