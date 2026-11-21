# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The upgrade indexes a conversation's messages by (conversation_id, created_at, id) and drops
the index on conversation_id alone, which the composite serves; a build that fails keeps the
old index; the downgrade restores it. A database without the AI tables (the startup creates
them from the models) upgrades without them."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine, text

from .conftest import throwaway_db, upgrade_to

_REV, _PREV = "y8d9e0f1a2b3", "x7c8d9e0f1a2"
_NEW, _OLD = "ix_ai_messages_conversation_created", "ix_ai_messages_conversation_id"
_COMPOSITE, _ALONE = ["conversation_id", "created_at", "id"], ["conversation_id"]


def _existing_ai_tables(sync_url: str) -> None:
    """The AI tables as a start of the previous version created them: indexed by conversation."""
    from celerp.models.base import Base
    eng = create_engine(sync_url)
    try:
        Base.metadata.create_all(eng, tables=[Base.metadata.tables[t] for t in ("ai_conversations", "ai_messages")])
        with eng.begin() as conn:
            conn.execute(text(f"DROP INDEX {_NEW}"))
            conn.execute(text(f"CREATE INDEX {_OLD} ON ai_messages (conversation_id)"))
    finally:
        eng.dispose()


def _indexes(sync_url: str) -> dict:
    """The two indexes that exist, each with its columns in key order (None for one left invalid)."""
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {name: columns if valid else None for name, valid, columns in conn.execute(text(
                "SELECT c.relname, i.indisvalid, ARRAY(SELECT a.attname::text FROM unnest(i.indkey) "
                "WITH ORDINALITY AS k(attnum, pos) JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = k.attnum ORDER BY k.pos) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = 'ai_messages'::regclass AND c.relname IN (:new, :old)"),
                {"new": _NEW, "old": _OLD})}
    finally:
        eng.dispose()


@pytest.mark.parametrize("encoding", ["SQL_ASCII", "UTF8"])
def test_the_composite_replaces_the_conversation_index_and_the_downgrade_restores_it(encoding):
    from alembic_support import alembic_run
    with throwaway_db("aimsgidx", encoding=encoding) as (_, sync_url):
        upgrade_to(sync_url, _PREV)
        _existing_ai_tables(sync_url)
        before = _indexes(sync_url)
        upgrade_to(sync_url, _REV)
        up = _indexes(sync_url)
        alembic_run(sync_url, "downgrade", _PREV)
        down = _indexes(sync_url)
    assert before == {_OLD: _ALONE}
    assert up == {_NEW: _COMPOSITE}
    assert down == {_OLD: _ALONE}


def test_a_failed_build_keeps_the_conversation_index(monkeypatch):
    from alembic.operations import Operations
    real = Operations.create_index

    def create_index(self, index_name, *args, **kwargs):
        real(self, index_name, *args, **kwargs)
        if index_name == _NEW:
            self.execute(text(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass('{_NEW}')"))
            raise RuntimeError(f"build of {_NEW} failed")

    with throwaway_db("aimsgidxfail") as (_, sync_url):
        upgrade_to(sync_url, _PREV)
        _existing_ai_tables(sync_url)
        monkeypatch.setattr(Operations, "create_index", create_index)
        upgrade_to(sync_url, _REV)
        assert _indexes(sync_url) == {_OLD: _ALONE}


def test_a_database_without_the_ai_tables_upgrades_and_downgrades():
    """Nothing to index is not a failed build: no warning that the index is built at the next start.
    The handler sits on the index logger itself: alembic's logging config replaces the root's."""
    from alembic_support import alembic_run
    warned = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = lambda record: warned.append(record.getMessage())
    index_log = logging.getLogger("celerp.migrations._lookup_index")
    index_log.addHandler(handler)
    try:
        with throwaway_db("aimsgidxnone") as (_, sync_url):
            upgrade_to(sync_url, _PREV)
            upgrade_to(sync_url, _REV)
            alembic_run(sync_url, "downgrade", _PREV)
            eng = create_engine(sync_url)
            try:
                with eng.connect() as conn:
                    assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _PREV
                    assert conn.execute(text("SELECT to_regclass('ai_messages')")).scalar() is None
            finally:
                eng.dispose()
    finally:
        index_log.removeHandler(handler)
    assert warned == []


def test_a_fresh_create_all_database_already_carries_the_composite_index():
    """The dev startup path never runs this migration on a database it creates: it builds
    ai_messages straight from the model, so the model's own index declaration is what a
    fresh install gets."""
    from celerp.models.base import Base
    with throwaway_db("aimsgidxcreateall") as (_, sync_url):
        upgrade_to(sync_url, _PREV)
        eng = create_engine(sync_url)
        try:
            Base.metadata.create_all(eng, tables=[Base.metadata.tables[t] for t in ("ai_conversations", "ai_messages")])
        finally:
            eng.dispose()
        assert _indexes(sync_url) == {_NEW: _COMPOSITE}
