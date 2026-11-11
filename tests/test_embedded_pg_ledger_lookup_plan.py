# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The GL Impact lookup of a record's journal entries uses the cost-scope index on the bundled
PostgreSQL, whose cluster takes the machine's locale (libc provider): there a plain btree cannot
serve the entry-id prefix match, so without the index the lookup reads every journal entry of
the company. The query is the one ``record_entry_ids`` sends, prepared with the company, the
event type and the record id as bound parameters, executed past the custom-plan window, and its
plan must use the index through the entry-id prefix.

Self-contained: the embedded job runs this file with --noconftest."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql

from celerp import embedded_pg
from celerp.db_url import sync_url

pytestmark = [
    pytest.mark.embedded_pg,
    pytest.mark.skipif(not embedded_pg.is_available(), reason="celerp-postgres wheel not available here"),
    pytest.mark.skipif(os.name == "nt", reason="needs the en_US.UTF-8 libc locale"),
]

INDEX = "ix_ledger_je_cost_scope"
COMPANY = uuid.UUID("00000000-0000-0000-0000-0000000000c5")


@pytest.fixture()
def cluster(tmp_path, monkeypatch):
    for name in ("LC_ALL", "LANG"):
        monkeypatch.setenv(name, "en_US.UTF-8")
    cfg = tmp_path / "celerp"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg / "config.toml"))
    uri = embedded_pg.ensure_cluster(cfg)
    monkeypatch.setenv("DATABASE_URL", uri)
    yield uri
    embedded_pg.wipe(cfg)


def _upgrade_to_head(uri: str) -> None:
    from alembic import command

    from celerp.alembic_config import build_alembic_config
    from celerp.db import migration_key

    engine = create_engine(sync_url(uri), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn, migration_key(conn):
            command.upgrade(build_alembic_config(), "head")
    finally:
        engine.dispose()


def _lookup_statement(record_id: str):
    """The select record_entry_ids sends for ``record_id``, captured from the function itself."""
    from celerp.services.ledger_sources import record_entry_ids

    captured = []

    class _Session:
        def get_bind(self):
            return type("Bind", (), {"dialect": postgresql.dialect()})()

        async def execute(self, statement):
            captured.append(statement)
            return type("Rows", (), {"all": lambda self: []})()

    asyncio.run(record_entry_ids(_Session(), COMPANY, record_id))
    return captured[0]


def _prepared(statement) -> tuple[str, list]:
    """``statement`` as PREPARE text with $n parameters, and the order its values go in."""
    compiled = statement.compile(dialect=postgresql.psycopg2.dialect())
    order: list[str] = []

    def number(match):
        order.append(match.group(1))
        return f"${len(order)}"

    sql = re.sub(r"%\((\w+)\)s", number, compiled.string).replace("%%", "%")
    types = ["uuid" if isinstance(compiled.params[n], uuid.UUID) else
             "jsonb" if isinstance(compiled.params[n], dict) else "text" for n in order]
    return f"PREPARE lookup ({', '.join(types)}) AS {sql}", [compiled.params[n] for n in order]


def _value(v):
    return json.dumps(v) if isinstance(v, dict) else str(v)


def test_cost_scope_lookup_uses_the_partial_index_on_a_libc_locale(cluster):
    _upgrade_to_head(cluster)
    engine = create_engine(sync_url(cluster), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT datcollate FROM pg_database WHERE datname = current_database()")
                                ).scalar() != "C"
            conn.execute(text("INSERT INTO companies (id, name, slug, settings, created_at) VALUES (:id, 'Plan Co', 'plan-co', '{}', now())"),
                         {"id": COMPANY})
            # Many journal entries of the company, and cost corrections for many records, so the
            # entry-id prefix of one record is selective.
            conn.execute(text("""
                INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key, metadata)
                SELECT :c, 'je:' || g, 'journal_entry', 'acc.journal_entry.created', '{}', 'test', 'k' || g,
                       json_build_object('doc_id', 'doc:' || (g % 5000))
                FROM generate_series(1, 120000) g"""), {"c": COMPANY})
            conn.execute(text("""
                INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key, metadata)
                SELECT :c, 'je:auto:doc:' || (g % 2000) || ':cost:' || g, 'journal_entry',
                       'acc.journal_entry.created', '{}', 'test', 'c' || g, '{}'
                FROM generate_series(1, 20000) g"""), {"c": COMPANY})
            conn.execute(text("ANALYZE ledger"))

            prepare, values = _prepared(_lookup_statement("doc:7"))
            raw = conn.connection.dbapi_connection
            with raw.cursor() as cur:
                cur.execute(prepare)
                execute = f"EXECUTE lookup ({', '.join(['%s'] * len(values))})"
                for _ in range(8):
                    cur.execute(execute, [_value(v) for v in values])
                    cur.fetchall()
                cur.execute("EXPLAIN (COSTS OFF) " + execute, [_value(v) for v in values])
                plan = [row[0] for row in cur.fetchall()]
    finally:
        engine.dispose()
    text_plan = "\n".join(plan)
    at = next((i for i, line in enumerate(plan) if INDEX in line), None)
    assert at is not None, text_plan
    # The index is used through the entry-id prefix, not only touched.
    cond = next((line for line in plan[at + 1:] if "Index Cond" in line), "")
    assert "entity_id" in cond, text_plan
