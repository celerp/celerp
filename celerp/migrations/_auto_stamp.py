# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Schema-aware alembic stamp repair.

The dev startup path runs `Base.metadata.create_all()` on every boot
(`celerp/main.py:148-151`), which builds the schema from the current
SQLAlchemy models. After a wipe (`celerp init --force`) the schema is at
"head" but the alembic stamp is missing or stale. A naive `alembic upgrade
head` then re-applies N migrations and crashes on DuplicateTable, DuplicateColumn
or worse.

This module walks revisions newest→oldest and asks, for each revision with
verifiable DDL, "is its signature present in the live schema?". It returns
the newest revision the schema is provably consistent with. Revisions
without verifiable DDL (data backfills) carry no evidence and are skipped —
they never justify a stamp on their own, so a backfill at head cannot mask
an unapplied migration below it. The caller (cli.py) stamps the DB to the
returned revision, leaving the rest for alembic upgrade to apply cleanly.

**Safety contract: false negatives are safe, false positives are catastrophic.**
A false negative (saying "not applied" when it actually is) costs us a
DuplicateTable/Column error which is recoverable. A false positive
(saying "applied" when it isn't) would mean we stamp past an unapplied
migration, skipping it forever. The walker errs strongly on the side of
false negatives — it only marks a revision as "fully applied" when every
DDL signature is present in the live schema.

DDL signatures handled:
  - create_table:  table exists in the schema
  - add_column:    column exists in the table
  - create_index:  index name exists in the table's indexes
  - create_unique_constraint: treated as create_index (Postgres/SQLite
                              both implement unique constraints as indexes)

DDL signatures NOT introspected (trusted via stamp):
  - alter_column: type/default changes are hard to verify; trust the stamp
  - drop_column / drop_table: we don't run downgrades; ignore
  - op.execute: data backfills cannot be verified safely; trust the stamp
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Signatures that can be verified by introspecting the live schema.
VERIFIABLE_KINDS = frozenset({
    "create_table", "add_column", "create_index", "create_unique_constraint",
})


@dataclass(frozen=True)
class RevisionSignature:
    """One DDL operation in a revision that can be checked against the live schema."""
    rev: str
    kind: str           # "create_table" | "add_column" | "create_index" | "create_unique_constraint"
    table: str          # the table the op targets
    column: str | None = None  # for add_column, the new column name
    extra: str | None = None   # index / constraint name
    columns: tuple[str, ...] = ()  # index / unique-constraint columns when literal


def _str_arg(node: ast.AST) -> str | None:
    """If node is a string literal, return its value. Otherwise None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _first_string_arg(call: ast.Call) -> str | None:
    """Return the value of the first string-literal positional argument."""
    for arg in call.args:
        s = _str_arg(arg)
        if s is not None:
            return s
    return None


def _kwarg_string(call: ast.Call, *names: str) -> str | None:
    """Return the value of the first matching keyword argument (string only)."""
    for kw in call.keywords:
        if kw.arg in names:
            return _str_arg(kw.value)
    return None


def _string_sequence(node: ast.AST | None) -> tuple[str, ...]:
    """Return literal string items from a list/tuple, otherwise empty."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return ()
    out: list[str] = []
    for item in node.elts:
        value = _str_arg(item)
        if value is None:
            return ()
        out.append(value)
    return tuple(out)


def _kwarg_node(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def extract_signatures(migration_file: Path) -> list[RevisionSignature]:
    """AST-walk a migration file and return its verifiable DDL signatures.

    Returns an empty list for files that:
      - cannot be parsed (syntax errors)
      - have no `def upgrade():` (e.g. helper modules)
      - contain only unverifiable ops (alter_column, op.execute, drops)

    Never raises. The walker degrades gracefully.
    """
    try:
        source = migration_file.read_text()
        tree = ast.parse(source)
    except Exception:
        return []

    rev_id: str | None = None
    upgrade_funcs: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        # Alembic templates emit either `revision = "abc"` (Assign) or, on newer
        # templates, `revision: str = "abc"` (AnnAssign). Handle both — missing
        # the annotated form silently drops every signature in that migration,
        # which would make find_safe_stamp advance past it unverified.
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "revision":
                    rev_id = _str_arg(node.value)
                    break
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name) and node.target.id == "revision"
                    and node.value is not None):
                rev_id = _str_arg(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            upgrade_funcs.append(node)

    if rev_id is None:
        return []

    sigs: list[RevisionSignature] = []
    for func in upgrade_funcs:
        for stmt in ast.walk(func):
            if not isinstance(stmt, ast.Call):
                continue
            # stmt.func should be something like op.add_column or op.create_table
            func_attr = stmt.func
            if not isinstance(func_attr, ast.Attribute):
                continue
            op_name = func_attr.attr

            if op_name == "create_table":
                table = _first_string_arg(stmt)
                if table:
                    sigs.append(RevisionSignature(
                        rev=rev_id, kind="create_table",
                        table=table, column=None, extra=None,
                    ))

            elif op_name == "add_column":
                table = _first_string_arg(stmt)
                column = None
                # op.add_column('users', sa.Column('email', ...))
                if len(stmt.args) >= 2:
                    col_arg = stmt.args[1]
                    # Could be sa.Column(...) — extract first string arg
                    if isinstance(col_arg, ast.Call):
                        column = _first_string_arg(col_arg)
                    else:
                        column = _str_arg(col_arg)
                if table and column:
                    sigs.append(RevisionSignature(
                        rev=rev_id, kind="add_column",
                        table=table, column=column, extra=None,
                    ))

            elif op_name == "create_index":
                # op.create_index('ix_users_email', 'users', ['email'])
                index_name = _first_string_arg(stmt)
                table = _kwarg_string(stmt, "table_name")
                if table is None and len(stmt.args) >= 2:
                    table = _str_arg(stmt.args[1])
                columns_node = _kwarg_node(stmt, "columns")
                if columns_node is None and len(stmt.args) >= 3:
                    columns_node = stmt.args[2]
                columns = _string_sequence(columns_node)
                if index_name and table:
                    sigs.append(RevisionSignature(
                        rev=rev_id, kind="create_index",
                        table=table, column=None, extra=index_name, columns=columns,
                    ))

            elif op_name == "create_unique_constraint":
                constraint_name = _first_string_arg(stmt)
                table = _kwarg_string(stmt, "table_name")
                if table is None and len(stmt.args) >= 2:
                    table = _str_arg(stmt.args[1])
                columns_node = _kwarg_node(stmt, "columns")
                if columns_node is None and len(stmt.args) >= 3:
                    columns_node = stmt.args[2]
                columns = _string_sequence(columns_node)
                if constraint_name and table:
                    sigs.append(RevisionSignature(
                        rev=rev_id, kind="create_unique_constraint",
                        table=table, column=None, extra=constraint_name, columns=columns,
                    ))

            # alter_column, drop_*, op.execute: not verifiable — skipped
    return sigs


def _table_exists(inspector, table: str) -> bool:
    try:
        return table in set(inspector.get_table_names())
    except Exception:
        return False


def _column_exists(inspector, table: str, column: str) -> bool:
    try:
        cols = inspector.get_columns(table)
    except Exception:
        return False
    return any(c.get("name") == column for c in cols)


def _index_exists(
    inspector, table: str, index_name: str | None, columns: tuple[str, ...] = (),
    *, unique_only: bool = False,
) -> bool:
    try:
        indexes = inspector.get_indexes(table)
    except Exception:
        indexes = []
    try:
        uqs = inspector.get_unique_constraints(table)
    except Exception:
        uqs = []

    wanted = tuple(columns)
    for item in indexes:
        if unique_only and not item.get("unique"):
            continue
        if index_name and item.get("name") == index_name:
            return True
        if wanted and tuple(item.get("column_names") or ()) == wanted:
            return True
    if unique_only:
        for item in uqs:
            if index_name and item.get("name") == index_name:
                return True
            if wanted and tuple(item.get("column_names") or ()) == wanted:
                return True
    return False


def _metadata_has_index(metadata, sig: RevisionSignature, *, unique_only: bool = False) -> bool:
    table = metadata.tables.get(sig.table)
    if table is None:
        return False
    wanted = tuple(sig.columns)
    for index in table.indexes:
        if unique_only and not index.unique:
            continue
        if sig.extra and index.name == sig.extra:
            return True
        if wanted and tuple(col.name for col in index.columns) == wanted:
            return True
    if unique_only:
        from sqlalchemy import UniqueConstraint
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            if sig.extra and constraint.name == sig.extra:
                return True
            if wanted and tuple(col.name for col in constraint.columns) == wanted:
                return True
    return False


def _signature_expected(metadata, sig: RevisionSignature) -> bool:
    """Whether this historical create/add operation is still kernel schema."""
    if metadata is None:
        return True
    table = metadata.tables.get(sig.table)
    if sig.kind == "create_table":
        return table is not None
    if sig.kind == "add_column":
        return table is not None and sig.column in table.c
    if sig.kind == "create_index":
        return _metadata_has_index(metadata, sig)
    if sig.kind == "create_unique_constraint":
        return _metadata_has_index(metadata, sig, unique_only=True)
    return True


def _signature_applied(inspector, sig: RevisionSignature) -> bool:
    """Return True iff the live schema contains this signature's DDL."""
    if sig.kind == "create_table":
        return _table_exists(inspector, sig.table)
    if sig.kind == "add_column":
        return _table_exists(inspector, sig.table) and _column_exists(inspector, sig.table, sig.column)
    if sig.kind == "create_index":
        return _table_exists(inspector, sig.table) and _index_exists(
            inspector, sig.table, sig.extra, sig.columns)
    if sig.kind == "create_unique_constraint":
        return _table_exists(inspector, sig.table) and _index_exists(
            inspector, sig.table, sig.extra, sig.columns, unique_only=True)
    return False


def load_kernel_metadata():
    """Register and return the current kernel model schema used by create_all."""
    from celerp.models.base import Base
    import celerp.models  # noqa: F401
    import celerp.models.company  # noqa: F401
    import celerp.models.ledger  # noqa: F401
    import celerp.models.projections  # noqa: F401
    return Base.metadata


def find_safe_stamp(
    revisions: Iterable,
    sigs_by_rev: dict[str, list[RevisionSignature]],
    inspector,
    *,
    expected_metadata=None,
) -> str:
    """Return the newest revision the current kernel schema safely proves.

    Revisions are newest→oldest. Data-only revisions carry no schema evidence.
    When ``expected_metadata`` is supplied, historical objects no longer owned
    by the current kernel are ignored; this prevents a create_all database from
    being judged against obsolete intermediate schema while still detecting a
    missing current column/index in any older revision.
    """
    revs = [r for r in revisions if getattr(r, "revision", None) is not None]
    seen_gap = False
    saw_evidence = False

    for rev in revs:
        sigs = [
            sig for sig in sigs_by_rev.get(rev.revision, [])
            if _signature_expected(expected_metadata, sig)
        ]
        if not sigs:
            continue
        saw_evidence = True
        if all(_signature_applied(inspector, sig) for sig in sigs):
            if seen_gap:
                return rev.revision
        else:
            seen_gap = True

    if not saw_evidence or seen_gap:
        return "base"
    return revs[0].revision if revs else "base"
