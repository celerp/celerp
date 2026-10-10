# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Existing charts learn which codes a migration made up. The proof is the migration's
own record: the source account it mapped, whose id the code was made from. A code that
only looks generated, typed or imported by someone, keeps showing."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from .conftest import run_migration_ops

MODULE = "e9f0a1b2c3d4_account_code_generated"


def _runs_and_maps(acc_db) -> None:
    with acc_db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE migration_runs (id UUID PRIMARY KEY, company_id UUID NOT NULL)"))
        conn.execute(text(
            "CREATE TABLE migration_entity_maps (id UUID PRIMARY KEY, migration_run_id UUID NOT NULL, "
            "source_type TEXT NOT NULL, source_external_id TEXT NOT NULL, "
            "target_entity_type TEXT NOT NULL, target_entity_id TEXT NOT NULL)"))


def _mapped(acc_db, run: uuid.UUID, cid: str, external_id: str, code: str) -> None:
    with acc_db.engine.begin() as conn:
        conn.execute(text("INSERT INTO migration_runs VALUES (:r, :c) ON CONFLICT DO NOTHING"), {"r": run, "c": cid})
        conn.execute(text("INSERT INTO migration_entity_maps VALUES (gen_random_uuid(), :r, 'account', :e, "
                          "'account', :code)"), {"r": run, "e": external_id, "code": code})


def _generated(acc_db, cid: str) -> dict[str, bool]:
    with acc_db.engine.connect() as conn:
        return dict(conn.execute(text("SELECT code, code_generated FROM accounts WHERE company_id = :c"),
                                 {"c": cid}).all())


def test_only_codes_a_migration_made_from_its_source_record_are_marked(acc_db):
    _runs_and_maps(acc_db)
    run, cid, other = uuid.uuid4(), str(uuid.uuid4()), str(uuid.uuid4())
    made = "M" + uuid.uuid5(run, "account:charges").hex[:8]
    clash = "M" + uuid.uuid5(run, "account:fees").hex[:8] + "-1"
    for code in (made, clash, "Mdeadbeef", "Mdeadbee0", "120"):
        acc_db.add_account(cid, code)
    acc_db.add_account(other, made)
    _mapped(acc_db, run, cid, "charges", made)
    _mapped(acc_db, run, cid, "fees", clash)
    _mapped(acc_db, run, cid, "petty", "Mdeadbee0")  # the source's own code
    _mapped(acc_db, run, cid, "ar", "120")

    run_migration_ops(acc_db.engine, MODULE)
    run_migration_ops(acc_db.engine, MODULE)

    assert _generated(acc_db, cid) == {made: True, clash: True, "Mdeadbeef": False, "Mdeadbee0": False,
                                       "120": False}
    assert _generated(acc_db, other) == {made: False}


def test_without_migration_records_every_code_is_shown(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "M0a1b2c3d")
    run_migration_ops(acc_db.engine, MODULE)
    assert _generated(acc_db, cid) == {"M0a1b2c3d": False}


def test_a_fresh_install_without_the_accounts_table_is_a_no_op(mig_db):
    run_migration_ops(mig_db.engine, MODULE)
