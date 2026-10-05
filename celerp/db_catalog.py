# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The database's own tables, columns and foreign keys, read from the Postgres catalog.

What the database holds can be more than the code loaded today knows about: a module
switched off keeps its tables. Anything that must account for every table a company
has rows in (a backup, a factory reset) reads it here rather than from the ORM."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Column:
    udt: str
    notnull: bool
    generated: bool


class ForeignKey(NamedTuple):
    cols: tuple[str, ...]
    target: str
    tcols: tuple[str, ...]
    cascades: bool  # ON DELETE CASCADE


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    pk: tuple[str, ...] = ()
    fks: list[ForeignKey] = field(default_factory=list)

    @property
    def insertable(self) -> list[str]:
        return [c for c, col in self.columns.items() if not col.generated]


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def read(session: AsyncSession) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for rel, att, udt, notnull, generated in (await session.execute(text(
            "SELECT c.relname::text, a.attname::text, t.typname::text, a.attnotnull, "
            "(a.attidentity <> '' OR a.attgenerated <> '' "
            " OR COALESCE(pg_get_expr(d.adbin, d.adrelid), '') LIKE 'nextval(%') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
            "ORDER BY c.relname, a.attnum"))).all():
        tables.setdefault(rel, Table(rel)).columns[att] = Column(udt, notnull, generated)
    for rel, kind, cols, target, tcols, on_delete in (await session.execute(text(
            "SELECT c.relname::text, k.contype::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY u(n, i) "
            "      JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n ORDER BY u.i), "
            "f.relname::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY u(n, i) "
            "      JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n ORDER BY u.i), "
            "k.confdeltype::text "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_class f ON f.oid = k.confrelid "
            "WHERE n.nspname = current_schema() AND k.contype IN ('p', 'f') "
            "ORDER BY c.relname, k.conname"))).all():
        table = tables.get(rel)
        if table is None:
            continue
        if kind == "p":
            table.pk = tuple(cols)
        else:
            table.fks.append(ForeignKey(tuple(cols), target, tuple(tcols), on_delete == "c"))
    return tables


def fk_order(tables: list[str], schema: dict[str, Table]) -> tuple[list[str], set[str]]:
    """Tables ordered so each follows every table it references, and the tables no such
    order exists for: those referencing themselves or in a reference cycle."""
    listed = set(tables)
    refs = {t: {fk.target for fk in schema[t].fks if fk.target in listed} for t in tables}
    unordered = {t for t in tables if t in refs[t]}
    parents = {t: refs[t] - {t} for t in tables}
    order: list[str] = []
    while ready := sorted(t for t, p in parents.items() if not p):
        order.extend(ready)
        for t in ready:
            del parents[t]
        for p in parents.values():
            p.difference_update(ready)
    # What is left is in a cycle or references one; only the cycles are unordered.
    while behind := [t for t in parents if not any(t in p for p in parents.values())]:
        for t in behind:
            del parents[t]
    return order, unordered | set(parents)


def company_tables(schema: dict[str, Table]) -> set[str]:
    """Every table holding a company's rows: ``companies``, each table with a company
    column, and each table with a foreign key to one of those (a conversation's
    messages, a run's entity maps)."""
    owned = {"companies"}
    while grown := {name for name, table in schema.items() if name not in owned
                    and ("company_id" in table.columns or any(fk.target in owned for fk in table.fks))}:
        owned |= grown
    return owned


def delete_users_left_without_a_company(schema: dict[str, Table]) -> str:
    """Of the users bound as ``:members``, delete those no remaining row still names.

    A row of a company, or one hanging off it, keeps its user whatever its foreign key
    does on delete: a cascade or a set-null would change that company's data. Elsewhere
    (a session, a badge) a cascading reference goes with the user."""
    company = company_tables(schema)
    refs = [f"NOT EXISTS (SELECT 1 FROM {ident(name)} WHERE {ident(col)} = users.id)"
            for name, table in schema.items()
            for fk in table.fks if fk.target == "users" and (name in company or not fk.cascades)
            for col in fk.cols]
    return " AND ".join(["DELETE FROM users WHERE id = ANY(:members)", *refs])
