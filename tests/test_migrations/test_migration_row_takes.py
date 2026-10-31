# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every revision's data step takes its tables before their rows.

A migration can run inside a serving process (System Recovery reconciles a restored schema),
so a data step that held a company's row while it waited for its next statement could meet a
request holding that company's lock and waiting for the same row. The lock checker counts every
such take; this walk runs every revision up, and this release's down and up again, over a
database that carries data, and requires that none of them made one. A second walk runs each
revision that writes a company's rows down and up over a company holding those rows, and fails
on any such revision function it did not see take one: the checker cannot judge a writer no
test ran.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from alembic.script import ScriptDirectory

from alembic_support import alembic_run, apply_migrations

from .conftest import throwaway_db, upgrade_to
from .test_alembic_graph import RELEASE_HEAD, _populate

from celerp.alembic_config import build_alembic_config

_SCRIPT = ScriptDirectory.from_config(build_alembic_config())
_REVISIONS = [r.revision for r in reversed(list(_SCRIPT.walk_revisions()))]


def _revision_of(writer: str) -> str:
    """The revision id a "path function" writer's file declares."""
    return _SCRIPT.get_revision(Path(writer.split(" ", 1)[0]).stem.split("_", 1)[0]).revision


def test_no_revision_takes_a_company_row_outside_a_table_lock():
    with throwaway_db("rowtakes") as (async_url, sync_url), \
            sys.modules["lock_rank"].ungated_takes(None, "celerp/migrations/") as found:
        upgrade_to(sync_url, RELEASE_HEAD)
        _populate(sync_url)
        apply_migrations(async_url)
        alembic_run(sync_url, "downgrade", RELEASE_HEAD)
        apply_migrations(async_url)
    assert sorted(found) == [], "\n".join(sorted(found))


def _company_with_chart_and_work_centers(async_url: str, sync_url: str) -> None:
    """The last release's company, upgraded, then running manufacturing with a chart from
    before the shrinkage write-off account, an account a Xero migration made a code for, and
    a connected store: the rows every revision's data step reads or changes."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from types import SimpleNamespace

    from celerp.models.connector_config import ConnectorConfig
    from celerp.models.migration import MigrationEntityMap, MigrationRun
    from celerp.services.migration_core_sink import deterministic_id
    from celerp_accounting.routes import THAI_CHART_OF_ACCOUNTS, _seeded_account

    upgrade_to(sync_url, RELEASE_HEAD)
    ids = _populate(sync_url)
    apply_migrations(async_url)
    eng = create_engine(sync_url)
    try:
        company = uuid.UUID(ids["company"])
        rows = [_seeded_account(company, entry) for entry in THAI_CHART_OF_ACCOUNTS if entry["code"] != "6970"]
        run_id = uuid.uuid4()
        code = "M" + deterministic_id(SimpleNamespace(run_id=run_id), "account", "xero-sales").hex[:8]
        # The accounting module's table comes from the boot's create_all, as it does live.
        type(rows[0]).__table__.create(eng, checkfirst=True)
        with Session(eng) as session, session.begin():
            session.execute(text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c"),
                            {"s": '{"manufacturing": {"hours_per_day": 8}}', "c": ids["company"]})
            session.add_all(rows)
            session.add(type(rows[0])(company_id=company, code=code, name="Sales (Xero)", account_type="revenue",
                                      parent_code="4000", is_active=True))
            session.add(MigrationRun(id=run_id.hex, company_id=company.hex, created_by_user_id=uuid.UUID(ids["user"]).hex,
                                     scan_claim_sha256="a" * 64, source_system="xero",
                                     source_artifact_sha256="b" * 64, adapter_version="1", cif_version="1",
                                     mode="import"))
            session.flush()
            session.add(MigrationEntityMap(migration_run_id=run_id.hex, source_type="account",
                                           source_external_id="xero-sales", target_entity_type="account",
                                           target_entity_id=code, status="created"))
            session.add(ConnectorConfig(company_id=ids["company"], connector="shopify"))
    finally:
        eng.dispose()


def test_every_revision_that_writes_a_company_row_runs_over_one_both_ways():
    """Every revision function that writes a company's rows runs here over a company that has
    them, down and up again from head, so the lock checker judges each one (a writer it never saw stays
    unproven) and none takes a row outside its table lock."""
    lock_rank = sys.modules["lock_rank"]
    writers = {w for found in lock_rank.static_writers().values() for w in found
               if w.startswith("celerp/migrations/")}
    newest_first = sorted({_revision_of(w) for w in writers}, key=_REVISIONS.index, reverse=True)
    with throwaway_db("rowwalk") as (async_url, sync_url), lock_rank.writers_taken("celerp/migrations/") as seen, \
            lock_rank.ungated_takes(None, "celerp/migrations/") as found:
        _company_with_chart_and_work_centers(async_url, sync_url)
        # Down to each revision's parent and up again, newest first: a revision runs over the
        # rows it acts on, as no table an older one reads is dropped on the way.
        for revision in newest_first:
            alembic_run(sync_url, "downgrade", _SCRIPT.get_revision(revision).down_revision)
            apply_migrations(async_url)
    assert sorted(found) == [], "\n".join(sorted(found))
    assert sorted(writers - seen) == [], "revision writers the walk never ran:\n" + "\n".join(sorted(writers - seen))
