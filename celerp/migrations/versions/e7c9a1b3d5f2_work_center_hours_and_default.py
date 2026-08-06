# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""work center hours_per_day + is_default; backfill a Default center

Revision ID: e7c9a1b3d5f2
Revises: d5e6f7a8b9c0
Create Date: 2026-08-06

Moves the company-wide manufacturing hours_per_day setting onto work centers.
Each manufacturing-active company gets one dedicated "Default" work center that
carries its old hours value (the same x-or-8.0 mapping the module used), a
company-wide in-app notice that the setting moved, and the now-migrated
settings key is stripped. One transaction: on any failure the whole revision
rolls back and the pre-migration schema and data survive intact. Forward-only.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e7c9a1b3d5f2"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

_NOTICE_TITLE = "Hours per day moved to work centers"
_NOTICE_BODY = (
    "Your company Hours per day setting now lives on the new Default work center under "
    "Settings > Manufacturing > Work centers. Nothing changed in your To-Make estimates."
)


def upgrade() -> None:
    conn = op.get_bind()

    # 1. New columns + the one-default-per-company invariant. The develop-to-release
    #    reconcile replays this revision's upgrade() against a database whose schema
    #    was built by create_all (celerp/migrations/_data_reconcile.py:95), so every
    #    statement here has to converge on a second run rather than raise.
    conn.execute(sa.text("ALTER TABLE work_centers ADD COLUMN IF NOT EXISTS hours_per_day DOUBLE PRECISION"))
    conn.execute(sa.text(
        "ALTER TABLE work_centers ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false"
    ))
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_center_one_default "
        "ON work_centers (company_id) WHERE is_default"
    ))

    # 2. One dedicated Default center per manufacturing-active company, carrying
    #    its old hours value. The CASE mirrors the module's `x or 8.0`: a stored
    #    0, blank, or absent value maps to 8.0 (a plain COALESCE would keep 0).
    #    settings is a JSON column, so the ? membership test needs a jsonb cast;
    #    the -> / ->> extraction works on json directly.
    conn.execute(sa.text("""
        INSERT INTO work_centers (id, company_id, name, hours_per_day, is_default, created_at)
        SELECT gen_random_uuid(), c.id, 'Default',
               CASE WHEN COALESCE(NULLIF(c.settings->'manufacturing'->>'hours_per_day', '')::float, 0) = 0
                    THEN 8.0
                    ELSE (c.settings->'manufacturing'->>'hours_per_day')::float END,
               true, NOW()
        FROM companies c
        WHERE (c.settings::jsonb ? 'manufacturing'
               OR EXISTS (SELECT 1 FROM work_centers wc WHERE wc.company_id = c.id))
          AND NOT EXISTS (SELECT 1 FROM work_centers d WHERE d.company_id = c.id AND d.is_default)
    """))

    # 3. One company-wide notice per company that just received a Default center
    #    (user_id NULL = company-wide). Guarded so a re-run never doubles it.
    #    The notifications table is created when the application boots, not by the
    #    migration chain, so on a database migrated before its first boot it is
    #    absent. Such a database has never run the app and therefore holds no
    #    company whose value could have moved, so there is nothing to notify.
    if conn.execute(sa.text("SELECT to_regclass('public.notifications')")).scalar() is not None:
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

    # 4. Strip the now-migrated key (jsonb #- needs the cast, then back to json).
    conn.execute(sa.text(
        "UPDATE companies SET settings = (settings::jsonb #- '{manufacturing,hours_per_day}')::json "
        "WHERE settings::jsonb #> '{manufacturing,hours_per_day}' IS NOT NULL"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_work_center_one_default"))
    conn.execute(sa.text("ALTER TABLE work_centers DROP COLUMN IF EXISTS is_default"))
    conn.execute(sa.text("ALTER TABLE work_centers DROP COLUMN IF EXISTS hours_per_day"))
