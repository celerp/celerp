# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The password reset method check is re-created in a form Postgres prints back the same
way after a backup is restored, so a recovered database matches the one it came from."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, exc, text

from .conftest import throwaway_db, upgrade_to

REVISION = "g1z2a3b4c5d6"
CHECK = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_password_resets_method'"


def test_the_method_check_survives_a_restore_unchanged():
    with throwaway_db("resetcheck") as (_, sync_url):
        upgrade_to(sync_url, REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                upgraded = conn.execute(text(CHECK)).scalar_one()
                conn.execute(text("ALTER TABLE password_resets DROP CONSTRAINT ck_password_resets_method"))
                conn.execute(text(f"ALTER TABLE password_resets ADD CONSTRAINT ck_password_resets_method {upgraded}"))
                restored = conn.execute(text(CHECK)).scalar_one()
            with pytest.raises(exc.IntegrityError), engine.begin() as conn:
                conn.execute(text("INSERT INTO users (id, email, name, auth_hash, is_active, created_at) "
                                  "VALUES (gen_random_uuid(), 'a@example.com', 'User', 'hash', true, now())"))
                conn.execute(text("INSERT INTO password_resets (id, user_id, method, created_at) "
                                  "SELECT gen_random_uuid(), id, 'link', now() FROM users"))
        finally:
            engine.dispose()
    assert restored == upgraded
