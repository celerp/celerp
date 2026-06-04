# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Schema-aware alembic stamp repair.

The dev startup path runs `Base.metadata.create_all()` on every boot
(`celerp/main.py:148-151`), which builds the schema from the current
SQLAlchemy models. After a wipe (`celerp init --force`) the schema is at
"head" but the alembic stamp is missing or stale. A naive `alembic upgrade
head` then re-applies N migrations and crashes on DuplicateTable, DuplicateColumn
or worse.

This module walks revisions newest→oldest and for each one asks
"is the DDL signature of this revision present in the live schema?". It
returns the highest revision that is fully applied. The caller (cli.py)
then stamps the DB to that revision, leaving the rest for alembic upgrade
to apply cleanly.

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
    extra: str | None = None   # for create_index, the index name


def _str_arg(node: ast.AST) -> str | None:
    """If node is a string literal, return its value. Otherwise None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _arg_by_index(call: ast.Call, idx: int) -> ast.AST | None:
    """Return the positional argument at idx, or the first kwarg by position."""
    if idx < len(call.args):
        return call.args[idx]
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
    in_upgrade = False
    upgrade_funcs: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "revision":
                    rev_id = _str_arg(node.value)
                    break
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
                if index_name and table:
                    sigs.append(RevisionSignature(
                        rev=rev_id, kind="create_index",
                        table=table, column=None, extra=index_name,
                    ))

            elif op_name == "create_unique_constraint":
                # Treated as a uniqueness index for verification purposes
                constraint_name = _first_string_arg(stmt)
                table = _kwarg_string(stmt, "table_name")
                if table is None and len(stmt.args) >= 2:
                    table = _str_arg(stmt.args[1])
                if constraint_name and table:
                    sigs.append(RevisionSignature(
                        rev=rev_id, kind="create_unique_constraint",
                        table=table, column=None, extra=constraint_name,
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


def _index_exists(inspector, table: str, index_name: str) -> bool:
    try:
        indexes = inspector.get_indexes(table)
    except Exception:
        return False
    if any(i.get("name") == index_name for i in indexes):
        return True
    # Unique constraints also show up here in some dialects
    try:
        uqs = inspector.get_unique_constraints(table)
    except Exception:
        uqs = []
    return any(u.get("name") == index_name for u in uqs)


def _signature_applied(inspector, sig: RevisionSignature) -> bool:
    """Return True iff the live schema contains this signature's DDL."""
    if sig.kind == "create_table":
        return _table_exists(inspector, sig.table)
    if sig.kind == "add_column":
        return _table_exists(inspector, sig.table) and _column_exists(inspector, sig.table, sig.column)
    if sig.kind == "create_index":
        return _table_exists(inspector, sig.table) and _index_exists(inspector, sig.table, sig.extra)
    if sig.kind == "create_unique_constraint":
        return _table_exists(inspector, sig.table) and _index_exists(inspector, sig.table, sig.extra)
    return False


def find_safe_stamp(
    revisions: Iterable,
    sigs_by_rev: dict[str, list[RevisionSignature]],
    inspector,
) -> str:
    """Walk revisions in the order given and return the safe-stamp revision.

    Contract:
      - For each revision, check every verifiable signature against the live
        schema. If ALL signatures are present (or there are none), the
        revision is considered fully applied.
      - The function returns the revision id of the highest fully-applied
        revision, or "base" if none are applied.
      - On the first revision that is NOT fully applied, stop walking.
        Any revisions earlier in the chain (which the caller should also
        check) are not our concern here — pass them in the right order.
      - A revision with zero verifiable signatures (pure data backfill) is
        conservatively treated as "applied" — we cannot prove it isn't, and
        refusing to advance would leave the dev stuck on a stamp that
        already represents applied work.

    Args:
        revisions: Iterable of alembic Script objects (need .revision attr)
        sigs_by_rev: Mapping of revision_id → list of RevisionSignature
        inspector: SQLAlchemy Inspector bound to the live DB

    Returns:
        Revision id of the highest fully-applied revision, or "base".
    """
    safe_stamp = "base"
    for rev in revisions:
        rev_id = getattr(rev, "revision", None)
        if rev_id is None:
            continue
        sigs = sigs_by_rev.get(rev_id, [])
        if not sigs:
            # No verifiable DDL — trust the stamp, advance past
            safe_stamp = rev_id
            continue
        # All signatures must be present for the revision to be "applied"
        if all(_signature_applied(inspector, s) for s in sigs):
            safe_stamp = rev_id
        else:
            # First not-fully-applied revision — stop walking
            break
    return safe_stamp
