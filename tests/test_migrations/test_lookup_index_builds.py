# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A lookup index build that fails costs only that index. The revision still finishes and is
stamped, the other indexes of the revision are built and kept, and only the failed one (left
invalid, as a failed concurrent build leaves it) is dropped. The next start builds the missing
one and keeps the rest. Every concurrent index build in a migration goes through the one helper
that does this."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from alembic_support import apply_migrations
from .conftest import head_rev, throwaway_db, upgrade_to

_VERSIONS = Path(__file__).resolve().parents[2] / "celerp" / "migrations" / "versions"
_NUMBER = ("ix_projections_number_shown", "ix_projections_number_kept", "ix_projections_number_key")
_CANONICAL = ("ix_projections_canonical_number_shown", "ix_projections_canonical_number_kept",
              "ix_projections_canonical_number_key")


def _query(sync_url: str, sql: str, name: str | None = None):
    engine = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql), {"name": name})
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def _valid(sync_url: str, name: str):
    return _query(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)", name)


def _oid(sync_url: str, name: str):
    return _query(sync_url, "SELECT to_regclass(:name)::oid", name)


@pytest.fixture
def failing_build(monkeypatch):
    """``failing_build(name)``: the build of index *name* fails the way a concurrent build does,
    leaving an INVALID index of that name behind."""
    from alembic.operations import Operations

    real = Operations.create_index

    def arm(name: str) -> None:
        def create_index(self, index_name, *args, **kwargs):
            real(self, index_name, *args, **kwargs)
            if index_name == name:
                self.execute(text(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass('{name}')"))
                raise RuntimeError(f"build of {name} failed")

        monkeypatch.setattr(Operations, "create_index", create_index)

    return arm


def test_a_failed_first_build_still_builds_the_others_in_the_same_run(failing_build):
    with throwaway_db("lookupfirst") as (_, sync_url):
        upgrade_to(sync_url, "p9q0r1s2t3u4")
        failing_build("ix_projections_number_shown")

        upgrade_to(sync_url, "q0r1s2t3u4v5")

        assert [_valid(sync_url, name) for name in _NUMBER] == [None, True, True]
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == "q0r1s2t3u4v5"


def test_a_failed_build_drops_only_itself_and_the_next_start_builds_it(failing_build, monkeypatch):
    with throwaway_db("lookupretry") as (async_url, sync_url):
        upgrade_to(sync_url, "o0c1d2e3f4a5")
        # A start before the upgrade built the first index from the models.
        _query(sync_url, "CREATE INDEX ix_projections_canonical_number_shown ON projections "
                         "(company_id, (trim(lower(state ->> 'ref_id'))))")
        kept = _oid(sync_url, _CANONICAL[0])
        failing_build("ix_projections_canonical_number_kept")

        upgrade_to(sync_url, "r1s2t3u4v5w6")

        assert [_valid(sync_url, name) for name in _CANONICAL] == [True, None, True]
        assert _oid(sync_url, _CANONICAL[0]) == kept
        built = _oid(sync_url, _CANONICAL[2])
        # Numbering keeps the indexes it reads until all three replacements are valid.
        assert [_valid(sync_url, name) for name in _NUMBER] == [True, True, True]
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == "r1s2t3u4v5w6"

        monkeypatch.undo()
        apply_migrations(async_url)

        assert [_valid(sync_url, name) for name in _CANONICAL] == [True, True, True]
        assert [_oid(sync_url, _CANONICAL[0]), _oid(sync_url, _CANONICAL[2])] == [kept, built]
        assert [_valid(sync_url, name) for name in _NUMBER] == [None, None, None]
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == head_rev()


def _concurrent_builds_outside_the_helper(tree: ast.AST) -> list[int]:
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "build_lookup_index":
            inside |= {id(n) for n in ast.walk(node)}
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "create_index"
            and any(k.arg == "postgresql_concurrently" for k in node.keywords) and id(node) not in inside
            and any(isinstance(f, ast.FunctionDef) and f.name == "upgrade" and node in ast.walk(f)
                    for f in ast.walk(tree))]


def test_every_concurrent_index_build_goes_through_the_lookup_index_helper():
    found = {path.name: _concurrent_builds_outside_the_helper(ast.parse(path.read_text()))
             for path in sorted(_VERSIONS.glob("*.py"))}
    assert {name: lines for name, lines in found.items() if lines} == {}
