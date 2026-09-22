# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import json

from sqlalchemy import create_engine, text

from .conftest import run_migration_ops, wc_mkcompany

MODULE = "b3c4d5e6f7a8_expand_role_permissions_to_role_grants"


def _settings(engine, cid: str) -> dict:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT settings FROM companies WHERE id = :id"), {"id": cid}).scalar_one()
    return value if isinstance(value, dict) else json.loads(value)


def test_sql_ascii_upgrade_and_downgrade_preserve_unrelated_unicode(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE companies (id UUID PRIMARY KEY, name TEXT, settings JSON NOT NULL)"
            ))
            cid = wc_mkcompany(conn, {
                "role_permissions": {"view_payments": "operator"},
                "note": "Müller",
            })

        run_migration_ops(engine, MODULE)
        settings = _settings(engine, cid)
        assert settings["note"] == "Müller"
        assert set(settings["role_grants"]["view_payments"]) == {
            "operator", "manager", "admin", "owner"
        }
        assert "role_permissions" not in settings

        run_migration_ops(engine, MODULE, "downgrade")
        settings = _settings(engine, cid)
        assert settings["note"] == "Müller"
        assert settings["role_permissions"]["view_payments"] == "operator"
        assert "role_grants" not in settings
    finally:
        engine.dispose()
