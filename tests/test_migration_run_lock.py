# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A migration run's row is locked within its company, after the company's lock: the lock
checker's census finds no run row taken outside the company lock by the canonical run lock
every start, finish, discard and resume takes."""
from __future__ import annotations

import sys
import uuid

_RUN = {"row:migration_runs", "key:migration_runs"}


def _ungated() -> set[str]:
    return sys.modules["lock_rank"].RECORDER.ungated_sites(_RUN, "celerp/services/migrations.py")


async def test_the_run_lock_takes_the_run_row_under_its_company(session, auth, ids):
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations

    run = MigrationRun(company_id=ids["company_id"], created_by_user_id=ids["user_id"],
                       scan_claim_sha256=uuid.uuid4().hex * 2, source_system="fake_source",
                       source_artifact_sha256="0" * 64, adapter_version="1", cif_version="1",
                       mode="additional")
    session.add(run)
    await session.commit()
    before = _ungated()

    await migrations._lock_run(session, run)
    await session.commit()
    assert _ungated() - before == set()
