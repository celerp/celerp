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

import json
import re

import sqlalchemy as sa
from alembic import op

from celerp.migrations._json_compat import update_company_settings

revision = "f8a9b0c1d2e3"
down_revision = "e7c9a1b3d5f2"
branch_labels = None
depends_on = None

_NOTICE_TITLE = "Hours per day moved to work centers"
_NOTICE_BODY = (
    "Your company Hours per day setting now lives on the new Default work center under "
    "Settings > Manufacturing > Work centers. Nothing changed in your To-Make estimates."
)
_HOURS_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _legacy_hours_per_day(value) -> float:
    """Mirror the retired SQL CASE exactly: positive decimal text, else 8.0."""
    text = str(value) if value is not None else ""
    if not _HOURS_RE.fullmatch(text):
        return 8.0
    parsed = float(text)
    return parsed if parsed != 0 else 8.0


def upgrade() -> None:
    conn = op.get_bind()

    # Never ask PostgreSQL to interpret companies.settings here. SQL_ASCII
    # databases can contain valid JSON with escaped Unicode that server-side
    # JSON operators cannot safely decode. Relational EXISTS checks stay in SQL;
    # settings membership/value handling stays in Python.
    rows = conn.execute(sa.text("""
        SELECT c.id, c.settings,
               EXISTS (SELECT 1 FROM work_centers wc WHERE wc.company_id = c.id) AS has_work_center,
               EXISTS (SELECT 1 FROM work_centers d
                       WHERE d.company_id = c.id AND d.is_default) AS has_default
        FROM companies c
    """)).mappings().all()
    for row in rows:
        settings = row["settings"]
        if not isinstance(settings, dict):
            settings = json.loads(settings)
        manufacturing_active = isinstance(settings, dict) and "manufacturing" in settings
        if row["has_default"] or not (manufacturing_active or row["has_work_center"]):
            continue
        manufacturing = settings.get("manufacturing") if isinstance(settings, dict) else None
        old_hours = (
            manufacturing.get("hours_per_day")
            if isinstance(manufacturing, dict)
            else None
        )
        conn.execute(
            sa.text("""
                INSERT INTO work_centers
                    (id, company_id, name, hours_per_day, is_default, created_at)
                VALUES (gen_random_uuid(), :cid, 'Default', :hours, true, NOW())
            """),
            {"cid": row["id"], "hours": _legacy_hours_per_day(old_hours)},
        )

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

    def _strip_hours(settings: dict, _row) -> bool:
        manufacturing = settings.get("manufacturing")
        if not isinstance(manufacturing, dict) or "hours_per_day" not in manufacturing:
            return False
        manufacturing.pop("hours_per_day")
        return True

    update_company_settings(conn, _strip_hours)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM work_centers WHERE is_default AND name = 'Default'"))
