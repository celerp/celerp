# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The revision graph is one line from the last release to head, and every way a
database reaches head ends with the same schema.

Two branches can each add a revision with the same ID and the same parent. Alembic
only warns that the ID is present more than once, and a database stamped at that ID
cannot say which of the two changes it holds. The ID check reads the files
themselves, not alembic's graph, so it names both files.

The schema checks run the real `celerp migrate` path (`_apply_migrations`) on empty
databases, never `Base.metadata.create_all`: a fresh install, an upgrade of a
populated database built at the last release's head, a downgrade back to that head,
and a database built from a branch that used the colliding ID for a different change.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from celerp.alembic_config import build_alembic_config
from celerp.cli import _apply_migrations
from celerp.migrations.compatibility import NEWEST_CELERP_KEY, running_version

from alembic_support import alembic_run

from .conftest import head_rev, schema_of, throwaway_db, upgrade_to

VERSIONS = Path(build_alembic_config().get_main_option("script_location")) / "versions"

# The head of the last release (main).
RELEASE_HEAD = "o2d3e4f5a6b7"

# What this release adds on top of it, oldest first: session companies, then the
# payment revisions, then per-company modules for companies from 2.5, then the invoice
# an unmatched payment is recorded on, then cash flow sections, notice translations and
# generated account codes, then import reversibility, then per-user notice reads, then
# the index on the asset a projection belongs to, then password resets.
RELEASE_CHAIN = ["p3e4f5a6b7c8", "q4f5a6b7c8d9", "r5g6b7c8d9e0", "s6h7c8d9e0f1", "t7i8j9k0l1m2",
                 "u8j9k0l1m2n3", "c7f1a2b3d4e5", "d8e9f0a1b2c3", "e9f0a1b2c3d4", "v0m1n2o3p4q5",
                 "w1n2o3p4q5r6", "x2o3p4q5r6s7", "y3p4q5r6s7t8"]

# The head 2.6.0 shipped. Released history never changes, so what comes after it follows
# it: company peer grants, then removing per-user API keys, then the firm view's key last
# use and refused records, then the revisions two-way sync sent or declined, then the
# account links two-way sync keeps, then the attachments a Xero migration fetches, then the
# firm name a company key carries, then the password reset method check a restore keeps,
# then per-record connector directions, then connector execution leases and first-sync records.
SHIPPED_HEAD = "y3p4q5r6s7t8"
AFTER_SHIPPED = ["x2p3q4r5s6t7", "y3q4r5s6t7u8", "z4r5s6t7u8v9", "a5t6u7v8w9x0", "b6u7v8w9x0y1",
                 "c7v8w9x0y1z2", "d8w9x0y1z2a3", "e9x0y1z2a3b4", "f0y1z2a3b4c5", "g1z2a3b4c5d6",
                 "h2a3b4c5d6e7", "i3b4c5d6e7f8"]

# The newest revision a database can already carry from the module and payment work
# that reaches main before this release.
MODULE_WORK_HEAD = "u8j9k0l1m2n3"

# The newest revision main carries ahead of this release's own revisions.
MAIN_HEAD = "e9f0a1b2c3d4"


def _declared(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision") and node.value is not None:
                found[target.id] = ast.literal_eval(node.value)
    return found


def _revision_files() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if p.name != "__init__.py")


def test_every_revision_id_is_declared_by_exactly_one_file():
    owners: dict[str, list[str]] = {}
    for path in _revision_files():
        owners.setdefault(_declared(path)["revision"], []).append(path.name)
    assert {rev: files for rev, files in owners.items() if len(files) > 1} == {}


def test_every_revision_file_is_named_after_its_id():
    assert [p.name for p in _revision_files() if not p.name.startswith(f"{_declared(p)['revision']}_")] == []


def test_the_graph_has_one_head_and_no_merge_points():
    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_heads() == [head_rev()]
    assert [r.revision for r in script.walk_revisions() if isinstance(r.down_revision, (tuple, list))] == []


def test_this_release_runs_in_order_after_the_last_release():
    script = ScriptDirectory.from_config(build_alembic_config())
    walked = [r.revision for r in script.walk_revisions(base=RELEASE_HEAD, head="heads")]
    assert list(reversed(walked)) == [RELEASE_HEAD, *RELEASE_CHAIN, *AFTER_SHIPPED]


def test_the_revision_2_6_0_shipped_keeps_its_parent():
    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_revision(SHIPPED_HEAD).down_revision == "x2o3p4q5r6s7"


def test_a_database_from_2_6_0_runs_every_revision_after_it():
    """A 2.6.0 database is stamped at the head 2.6.0 shipped, so every revision after it
    still runs on upgrade: the per-user API key is dropped and keys issued before the firm
    name existed take the name their client was shown."""
    with throwaway_db("from_2_6_0") as (_, sync_url):
        upgrade_to(sync_url, SHIPPED_HEAD)
        eng = create_engine(sync_url)
        company_id, grant_id = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            with eng.begin() as conn:
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == SHIPPED_HEAD
                conn.execute(text(
                    "INSERT INTO users (id, email, name, auth_hash, is_active, created_at, api_key) "
                    "VALUES (:u, 'owner@example.com', 'Owner', 'hash', true, now(), 'old-key')"),
                    {"u": str(uuid.uuid4())})
                conn.execute(text(
                    "INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                    "VALUES (:id, 'Firm Copy', 'firm-copy', "
                    "CAST('{\"restored_backup\": {\"opened_from\": \"Lakeside\"}}' AS json), true, now())"),
                    {"id": company_id})
            # A key issued by a build between the peer grants and the firm name.
            upgrade_to(sync_url, AFTER_SHIPPED[AFTER_SHIPPED.index("f0y1z2a3b4c5") - 1])
            with eng.begin() as conn:
                conn.execute(text(
                    "INSERT INTO company_peer_grants (id, company_id, source_company_id, token_hash, "
                    "label, scopes, created_at, expires_at) VALUES (:id, :c, :s, :h, 'Key', "
                    "CAST('[]' AS json), now(), now() + interval '1 day')"),
                    {"id": grant_id, "c": company_id, "s": str(uuid.uuid4()), "h": "c" * 64})
            upgrade_to(sync_url, head_rev())
            with eng.connect() as conn:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                firm = conn.execute(text("SELECT firm FROM company_peer_grants WHERE id = :id"),
                                    {"id": grant_id}).scalar_one()
        finally:
            eng.dispose()
        user_columns = {column for table, column, *_ in schema_of(sync_url)["columns"] if table == "users"}
    assert version == head_rev() == AFTER_SHIPPED[-1]
    assert "api_key" not in user_columns
    assert firm == "Lakeside"


@pytest.mark.parametrize("table", ["company_peer_grants", "company_peer_identities"])
def test_peer_downgrade_preserves_persisted_records(table):
    with throwaway_db("peer_rollback") as (_, sync_url):
        upgrade_to(sync_url, head_rev())
        eng = create_engine(sync_url)
        company_id = str(uuid.uuid4())
        try:
            with eng.begin() as conn:
                conn.execute(text(
                    "INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                    "VALUES (:id, 'Peer', 'peer', '{}', true, now())"),
                    {"id": company_id})
                if table == "company_peer_grants":
                    conn.execute(text(
                        "INSERT INTO company_peer_grants "
                        "(id, company_id, source_company_id, token_hash, label, firm, scopes, "
                        "created_at, expires_at) VALUES "
                        "(:id, :company, :source, :digest, 'Firm', 'Firm', '[\"contacts\"]', "
                        "now(), now() + interval '1 day')"), {
                            "id": str(uuid.uuid4()), "company": company_id,
                            "source": str(uuid.uuid4()), "digest": "a" * 64,
                        })
                else:
                    conn.execute(text(
                        "INSERT INTO company_peer_identities "
                        "(id, company_id, source_company_id, entity_type, source_entity_id, "
                        "source_key_hash, target_entity_id, last_revision) "
                        "VALUES (:id, :company, :source, 'contact', 'old', :digest, 'contact:new', 1)"), {
                            "id": str(uuid.uuid4()), "company": company_id,
                            "source": str(uuid.uuid4()), "digest": "b" * 64,
                        })
        finally:
            eng.dispose()
        with pytest.raises(RuntimeError, match="Cannot downgrade while company integrations exist"):
            _downgrade_to(sync_url, SHIPPED_HEAD)
        eng = create_engine(sync_url)
        try:
            with eng.connect() as conn:
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head_rev()
        finally:
            eng.dispose()


def _without_instance_meta(schema: dict[str, set]) -> dict[str, set]:
    return {kind: {row for row in rows if not (isinstance(row, tuple) and row[0] == "instance_meta")}
            for kind, rows in schema.items()}


def _newest_celerp(sync_url: str) -> str | None:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return conn.execute(text("SELECT value FROM instance_meta WHERE key = :k"),
                                {"k": NEWEST_CELERP_KEY}).scalar()
    finally:
        eng.dispose()


def _downgrade_to(sync_url: str, revision: str) -> None:
    alembic_run(sync_url, "downgrade", revision)


def _populate(sync_url: str) -> dict[str, str]:
    """One company with a user, a signed-in session, an item and an import, as the
    last release stores them."""
    ids = {k: str(uuid.uuid4()) for k in ("company", "user", "batch")}
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email, name, auth_hash, is_active, created_at) "
                              "VALUES (:u, 'owner@example.test', 'Owner', 'x', true, now())"), {"u": ids["user"]})
            conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                              "VALUES (:c, 'Kept', 'kept', '{}', true, now())"), {"c": ids["company"]})
            conn.execute(text("INSERT INTO user_companies (id, user_id, company_id, role, is_active) "
                              "VALUES (gen_random_uuid(), :u, :c, 'owner', true)"),
                         {"u": ids["user"], "c": ids["company"]})
            conn.execute(text("INSERT INTO user_auth_state (user_id, nonce) VALUES (:u, 'before')"),
                         {"u": ids["user"]})
            conn.execute(text("INSERT INTO session_registry (jti, user_id, expiry, created_at) "
                              "VALUES ('old-session', :u, now() + interval '1 day', now())"), {"u": ids["user"]})
            conn.execute(text(
                "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, "
                "idempotency_key, ts) VALUES (:c, 'item:kept', 'item', 'item.created', "
                "'{\"sku\": \"KEPT\"}', 'test', 'kept-1', now())"), {"c": ids["company"]})
            conn.execute(text(
                "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                "VALUES (:c, 'item:kept', 'item', '{\"sku\": \"KEPT\"}', 1, now())"), {"c": ids["company"]})
            conn.execute(text(
                "INSERT INTO import_batches (id, company_id, entity_type, filename, row_count, entity_ids, "
                "idempotency_keys) VALUES (:b, :c, 'item', 'items.csv', 1, '[\"item:kept\"]', '[\"kept-1\"]')"),
                {"b": ids["batch"], "c": ids["company"]})
    finally:
        eng.dispose()
    return ids


def _business_data(sync_url: str, ids: dict[str, str]) -> dict[str, object]:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {
                "ledger": conn.execute(text("SELECT entity_id, data::text FROM ledger WHERE company_id = :c"),
                                       {"c": ids["company"]}).all(),
                "projections": conn.execute(text(
                    "SELECT entity_id, state::text FROM projections WHERE company_id = :c"),
                    {"c": ids["company"]}).all(),
                "batches": conn.execute(text("SELECT id::text, entity_ids::text FROM import_batches")).all(),
                "members": conn.execute(text("SELECT user_id::text, role FROM user_companies")).all(),
            }
    finally:
        eng.dispose()


def test_upgrade_downgrade_and_fresh_install_agree_on_the_schema():
    with throwaway_db("graph_up") as (up_async, up_sync), throwaway_db("graph_new") as (new_async, _new_sync):
        upgrade_to(up_sync, RELEASE_HEAD)
        at_release = schema_of(up_sync)
        ids = _populate(up_sync)
        kept = _business_data(up_sync, ids)

        _apply_migrations(up_async)
        upgraded = schema_of(up_sync)
        _apply_migrations(new_async)
        fresh = schema_of(_new_sync)

        assert upgraded["stamp"] == fresh["stamp"] == {head_rev()}
        assert upgraded == fresh
        assert _business_data(up_sync, ids) == kept
        eng = create_engine(up_sync)
        try:
            with eng.connect() as conn:
                # An import from before this release cannot be shown to be undoable.
                assert conn.execute(text("SELECT reversible FROM import_batches")).scalars().all() == [False]
                # Sessions that name no company are signed out, and so are the tokens behind them.
                assert conn.execute(text("SELECT count(*) FROM session_registry")).scalar_one() == 0
                assert conn.execute(text("SELECT nonce FROM user_auth_state")).scalar_one() != "before"
        finally:
            eng.dispose()

        _downgrade_to(up_sync, RELEASE_HEAD)
        # The record of the newest Celerp that opened the database is not part of any
        # revision, so going back a release keeps it: that copy is still refused.
        assert _without_instance_meta(schema_of(up_sync)) == at_release
        assert _newest_celerp(up_sync) == running_version()
        assert _business_data(up_sync, ids) == kept

        _apply_migrations(up_async)
        assert schema_of(up_sync) == fresh
        assert _business_data(up_sync, ids) == kept


@pytest.mark.parametrize("start", [MODULE_WORK_HEAD, MAIN_HEAD])
def test_a_database_at_an_earlier_head_reaches_head(start):
    with throwaway_db("graph_mod") as (mod_async, mod_sync), throwaway_db("graph_new") as (new_async, new_sync):
        upgrade_to(mod_sync, start)
        _apply_migrations(mod_async)
        _apply_migrations(new_async)

        assert schema_of(mod_sync)["stamp"] == {head_rev()}
        assert schema_of(mod_sync) == schema_of(new_sync)


def test_a_database_from_before_the_revisions_were_reordered_reaches_the_same_schema():
    """A database built while import reversibility still carried the ID that the
    unmatched refunds revision now has: stamped at that ID with only the import column
    present. Migrating it must add the refunds table rather than trust the stamp."""
    with throwaway_db("graph_old") as (old_async, old_sync), throwaway_db("graph_new") as (new_async, new_sync):
        upgrade_to(old_sync, "r5g6b7c8d9e0")
        eng = create_engine(old_sync)
        try:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE import_batches ADD COLUMN reversible BOOLEAN DEFAULT false NOT NULL"))
                conn.execute(text("UPDATE alembic_version SET version_num = 's6h7c8d9e0f1'"))
        finally:
            eng.dispose()

        _apply_migrations(old_async)
        _apply_migrations(new_async)

        assert schema_of(old_sync) == schema_of(new_sync)


# Tables this release adds that name a company without belonging to it: each records
# something about a company's online payments that has to outlive the company, so a
# company backup never carries them.
OUTLIVES_ITS_COMPANY = {"payment_closures", "payment_recoveries", "unmatched_payments", "unmatched_refunds"}

# Tables this release adds that belong to a person on this installation, never to a
# company, so no company backup carries them.
BELONGS_TO_A_PERSON = {"migration_templates", "migration_batch_files"}


def test_every_table_and_column_this_release_adds_is_placed_for_company_backups():
    from celerp.migrations._auto_stamp import extract_signatures
    from celerp.models.base import Base
    from celerp.services import company_backup as cb

    script = ScriptDirectory.from_config(build_alembic_config())
    added = {sig.table
             for rev in script.walk_revisions(base=RELEASE_HEAD, head="heads") if rev.revision != RELEASE_HEAD
             for sig in extract_signatures(Path(rev.path)) if sig.kind in ("create_table", "add_column")}
    tables = Base.metadata.tables

    assert added >= OUTLIVES_ITS_COMPANY | BELONGS_TO_A_PERSON
    for table in sorted(added):
        if "company_id" in tables[table].c:
            assert (table in cb.PORTABLE_TABLES) != (table in cb.EXCLUDED_TABLES), table
        else:
            # Without a company column a table is either one of those above or hangs
            # off a table that stays with the installation (a notice's read receipts,
            # a user's password resets).
            parents = {fk.column.table.name for fk in tables[table].foreign_keys}
            assert table not in cb.PORTABLE_TABLES, table
            assert table in OUTLIVES_ITS_COMPANY or parents & {*cb.EXCLUDED_TABLES, "users"}, table
