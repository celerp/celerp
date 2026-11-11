# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The trimmed numbering lookup revision keeps a valid index a start of this version already
built from the models, rebuilds an INVALID index of its name, builds a missing one, and drops the
indexes on the untrimmed expressions no query reads any more. A downgrade puts them back. On
every server encoding Celerp creates (UTF8, and SQL_ASCII from earlier installs) it stores each
padded number stripped and numbering reads it through the indexes, whose definitions are ASCII
and restore into a database of the other encoding."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
from types import SimpleNamespace

import pytest
from alembic_support import alembic_run
import sqlalchemy as sa
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from celerp.json_text import SYNC_TEXT

from .conftest import throwaway_db, upgrade_to
from .test_q0r1s2t3u4v5_projections_number_lookup import INDEXES as UNTRIMMED, _query, _valid

REVISION = "r1s2t3u4v5w6"
DOWN_REVISION = "o0c1d2e3f4a5"
INDEXES = ("ix_projections_canonical_number_shown", "ix_projections_canonical_number_kept",
           "ix_projections_canonical_number_key")


def test_upgrade_rebuilds_an_invalid_index_keeps_a_valid_one_drops_the_untrimmed_and_stamps():
    with throwaway_db("trimidx") as (_, sync_url):
        upgrade_to(sync_url, DOWN_REVISION)
        assert [_valid(sync_url, name) for name in UNTRIMMED] == [True, True, True]
        # A start before the upgrade built the first index from the models; a cancelled
        # concurrent build left the second one invalid; the third is missing.
        _query(sync_url, "CREATE INDEX ix_projections_canonical_number_shown ON projections "
                         "(company_id, (trim(lower(state ->> 'ref_id'))))")
        kept = _query(sync_url, "SELECT to_regclass('ix_projections_canonical_number_shown')::oid")
        _query(sync_url, "CREATE INDEX ix_projections_canonical_number_kept ON projections "
                         "(company_id, (trim(lower(state ->> 'doc_number'))))")
        _query(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)",
               "ix_projections_canonical_number_kept")

        upgrade_to(sync_url, REVISION)

        assert [_valid(sync_url, name) for name in INDEXES] == [True, True, True]
        assert _query(sync_url, "SELECT to_regclass('ix_projections_canonical_number_shown')::oid") == kept
        assert [_valid(sync_url, name) for name in UNTRIMMED] == [None, None, None]
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == REVISION

        alembic_run(sync_url, "downgrade", DOWN_REVISION)

        assert [_valid(sync_url, name) for name in UNTRIMMED] == [True, True, True]
        assert [_valid(sync_url, name) for name in INDEXES] == [None, None, None]


# Padding an earlier version saved around a number: name -> the space.
_PADS = {"space": " ", "tab": "\t", "NBSP": "\u00a0", "U+3000": "\u3000"}
# The server encodings Celerp creates a database in: UTF8, and SQL_ASCII before 2.5.1.
ENCODINGS = ("UTF8", "SQL_ASCII")


def _query_all(sync_url: str, sql: str) -> list[tuple]:
    engine = create_engine(sync_url, **SYNC_TEXT)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql))]
    finally:
        engine.dispose()


def _seed_padded(sync_url: str, encoding: str) -> str:
    """A company with two Documents per padding, saved as an earlier version saved them: one
    with the number as sent, in both number fields, with a letter past ASCII in it; one stored
    under its number as sent (its key) and since renumbered. Enough other Documents that the
    planner reads a lookup through the indexes."""
    eng = create_engine(sync_url, **SYNC_TEXT)
    try:
        with eng.begin() as conn:
            assert conn.execute(text("SHOW server_encoding")).scalar() == encoding
            cid = conn.execute(text("INSERT INTO companies (id, name, slug, settings, created_at) VALUES "
                                    "(gen_random_uuid(), 'Demo', 'demo', '{}', now()) RETURNING id")).scalar()
            for name in sorted(_PADS):
                number = f"{_PADS[name]}LEG-{name}-\u00c9{_PADS[name]}"
                conn.execute(text(
                    "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                    "VALUES (:c, :e, 'doc', CAST(:s AS json), 1, now())"),
                    {"c": cid, "e": f"doc:{name}", "s": json.dumps({"ref_id": number, "doc_number": number,
                                                                   "doc_type": "quotation"}, ensure_ascii=False)})
                conn.execute(text(
                    "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                    "VALUES (:c, :e, 'doc', CAST(:s AS json), 2, now())"),
                    {"c": cid, "e": f"doc:{_PADS[name]}KEY-{name}-\u00c9{_PADS[name]}",
                     "s": json.dumps({"ref_id": f"MOVED-{name}", "doc_type": "quotation"})})
            conn.execute(text(
                "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                "SELECT :c, 'doc:f' || n, 'doc', json_build_object('ref_id', 'F-' || n), 1, now() "
                "FROM generate_series(1, 5000) n"), {"c": cid})
    finally:
        eng.dispose()
    return str(cid)


async def _lookups(async_url: str, cid: str, numbers: list[str]) -> tuple[list, list[str], list[str]]:
    """(holder of each number, every statement numbering sent, the plan of the last lookup)."""
    from celerp_docs.sequences import held_numbers, number_holder

    eng = create_async_engine(async_url)
    sent: list[tuple[str, object]] = []
    event.listen(eng.sync_engine, "before_cursor_execute",
                 lambda conn, cur, stmt, params, ctx, many: sent.append((stmt, params)))
    try:
        async with AsyncSession(eng) as session:
            holders = [await number_holder(session, cid, n) for n in numbers]
            await held_numbers(session, cid, {}, ["quotation"])
        lookup, params = next(s for s in reversed(sent) if "LIMIT" in s[0])
        async with eng.connect() as conn:
            await conn.exec_driver_sql("ANALYZE projections")
            plan = (await conn.exec_driver_sql("EXPLAIN " + lookup, params)).scalars().all()
    finally:
        await eng.dispose()
    return holders, [s for s, _ in sent], plan


def _assert_ascii(statements) -> None:
    """No statement names a character set: every one is plain ASCII without chr()."""
    bad = [s for s in statements if "chr(" in s or not s.isascii()]
    assert not bad, bad[:1]


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_every_server_encoding_upgrades_stores_padded_numbers_stripped_and_holds_them_by_index(encoding):
    with throwaway_db("numenc", encoding) as (async_url, sync_url):
        upgrade_to(sync_url, DOWN_REVISION)
        cid = _seed_padded(sync_url, encoding)

        upgrade_to(sync_url, "head")

        stored = _query_all(sync_url, "SELECT state ->> 'ref_id', state ->> 'doc_number' FROM projections")
        assert sorted(r for r in stored if r[1]) == sorted((f"LEG-{n}-\u00c9",) * 2 for n in _PADS)
        numbers = [f"{p}-{n.lower()}-\u00c9" for p in ("leg", "key") for n in sorted(_PADS)]
        holders, statements, plan = asyncio.run(_lookups(async_url, cid, numbers))
        assert [h[:2] for h in holders] == [("doc", f"LEG-{n}-\u00c9") for n in sorted(_PADS)] + [
            ("doc", f"MOVED-{n}") for n in sorted(_PADS)], holders
        _assert_ascii(statements)
        assert {f"ix_projections_canonical_number_{i}" for i in ("shown", "kept", "key")} <= {
            w for line in plan for w in line.split()}, plan


@pytest.mark.parametrize("source, target", [("UTF8", "SQL_ASCII"), ("SQL_ASCII", "UTF8")])
def test_a_head_database_restored_into_another_encoding_keeps_its_number_indexes(source, target, tmp_path):
    from celerp.services import backup

    with throwaway_db("numsrc", source) as (_, src_url), throwaway_db("numdst", target) as (dst_async, dst_url):
        upgrade_to(src_url, "head")
        cid = _seed_padded(src_url, source)
        dump = tmp_path / "head.dump"
        backup.dump_database(src_url.replace("+psycopg2", ""), dump)
        backup.restore_database_file(dump, dst_url.replace("+psycopg2", ""))

        assert [_valid(dst_url, name) for name in INDEXES] == [True, True, True]
        holders, statements, plan = asyncio.run(_lookups(dst_async, cid, ["leg-space-\u00c9"]))
        assert [h[:2] for h in holders] == [("doc", "LEG-space-\u00c9")]
        _assert_ascii(statements)
        assert {f"ix_projections_canonical_number_{i}" for i in ("shown", "kept", "key")} <= {
            w for line in plan for w in line.split()}, plan


class _Unread:
    """A result with no rows, for statements compiled without a database."""

    def first(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter(())


class _Compiler:
    """A session and a migration op that compile what they are given for PostgreSQL instead of
    sending it: every numbering statement, and every index the revision creates."""

    def __init__(self):
        self.sql: list[str] = []
        self.built: set[str] = set()

    def _compile(self, clause) -> str:
        from sqlalchemy.dialects import postgresql

        return str(clause.compile(dialect=postgresql.dialect()))

    async def execute(self, statement, *_):
        self.sql.append(self._compile(statement))
        return _Unread()

    # The op the revision's upgrade() calls.
    def get_bind(self):
        return self

    def get_context(self):
        return SimpleNamespace(autocommit_block=contextlib.nullcontext)

    def run(self, statement, params):
        return SimpleNamespace(scalar=lambda: params["name"] in self.built or None)

    def create_index(self, name, table, columns, **_):
        from sqlalchemy.schema import CreateIndex

        self.built.add(name)
        detached = sa.Table(table, sa.MetaData(), sa.Column("company_id", sa.Uuid))
        self.sql.append(self._compile(CreateIndex(sa.Index(
            name, *(detached.c[c] if isinstance(c, str) else c for c in columns)))))

    def drop_index(self, *_, **__):
        pass


def test_every_numbering_statement_and_index_definition_is_ascii_without_chr(monkeypatch):
    """Every statement numbering sends for a candidate padded with spaces past ASCII, and the
    number lookup indexes the revision creates, compiled for PostgreSQL, are plain ASCII: no
    character set an encoding might not hold, no chr(). The revision builds the indexes the
    models declare, text for text."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    from celerp.models.projections import Projection
    from celerp_docs.routes import _ids_numbered
    from celerp_docs.sequences import held_numbers, number_holder

    revision = importlib.import_module(f"celerp.migrations.versions.{REVISION}_projections_canonical_number_lookup")
    candidate, session, op = "\u1680\u3000INV-\u00c9\u00a0", _Compiler(), _Compiler()
    op.execute = op.run
    asyncio.run(number_holder(session, "c", candidate, exclude_entity_id="doc:x", doc_type="invoice"))
    asyncio.run(held_numbers(session, "c", {}, ["quotation"]))
    for kind in ("doc", "list"):
        asyncio.run(_ids_numbered(session, "c", kind, candidate, doc_type="invoice"))
    monkeypatch.setattr(revision, "op", op)
    monkeypatch.setattr(revision, "update_projection_state", lambda *_, **__: None)
    revision.upgrade()

    models = sorted(str(CreateIndex(i).compile(dialect=postgresql.dialect()))
                    for i in Projection.__table__.indexes if i.name in INDEXES)
    assert len(session.sql) == 4
    assert sorted(op.sql) == models and len(models) == len(INDEXES)
    _assert_ascii(session.sql + op.sql)
