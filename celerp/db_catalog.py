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
    on_delete: str  # pg_constraint.confdeltype: a no action, r restrict, c cascade, n set null, d set default

    @property
    def cascades(self) -> bool:
        return self.on_delete == "c"

    @property
    def clears(self) -> bool:
        """SET NULL or SET DEFAULT: the row outlives the one it names."""
        return self.on_delete in ("n", "d")


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
            # A key into another schema names a table this catalog does not hold, and so does
            # the copy Postgres keeps of a key for each partition of the table it names.
            "WHERE n.nspname = current_schema() AND (k.contype = 'p' OR k.contype = 'f' "
            "AND f.relnamespace = n.oid AND k.conparentid = 0) "
            "ORDER BY c.relname, k.conname"))).all():
        table = tables.get(rel)
        if table is None:
            continue
        if kind == "p":
            table.pk = tuple(cols)
        else:
            table.fks.append(ForeignKey(tuple(cols), target, tuple(tcols), on_delete))
    return tables


async def outside_referrer(session: AsyncSession) -> str | None:
    """A table of another schema with a foreign key into this one, as ``schema.table``, or
    None. Its rows are not in this catalog, so nothing reading it can tell whose they are."""
    return await session.scalar(text(
        "SELECT format('%s.%s', rn.nspname, r.relname) FROM pg_constraint k "
        "JOIN pg_class r ON r.oid = k.conrelid JOIN pg_namespace rn ON rn.oid = r.relnamespace "
        "JOIN pg_class f ON f.oid = k.confrelid JOIN pg_namespace fn ON fn.oid = f.relnamespace "
        "WHERE k.contype = 'f' AND fn.nspname = current_schema() AND rn.oid <> fn.oid "
        "ORDER BY 1 LIMIT 1"))


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


def company_tables(schema: dict[str, Table], *, held: bool = False) -> set[str]:
    """Every table whose rows name a company: ``companies``, each table with a company
    column, and each table with a foreign key to one of those (a conversation's
    messages, a run's entity maps). ``held`` follows only keys that do not clear on
    delete: a row reached only through a clearing key outlives the company, so it is
    not the company's to delete."""
    owned = {"companies"}
    while grown := {name for name, table in schema.items() if name not in owned
                    and ("company_id" in table.columns
                         or any(fk.target in owned and not (held and fk.clears) for fk in table.fks))}:
        owned |= grown
    return owned


def delete_users_left_without_a_company(schema: dict[str, Table]) -> str:
    """Of the users bound as ``:members``, delete those whose deletion goes no further
    than rows holding no company's data.

    Deleting a user cascades into the rows outside any company that name it by a
    cascading key (a session, a badge), and on into theirs. A user is kept while any
    other row names one of those: a row of a company, another user, or a row naming it
    by a key that does not cascade. Deleting the user would cascade into, change or trip
    over that row."""
    company = company_tables(schema)
    # The users being deleted are named by an alias no table of the schema has, so no
    # table a condition reads can hide it.
    user = "u"
    while user in schema:
        user += "_"

    def picks(fk: ForeignKey, condition: str) -> str:
        return (f"({', '.join(map(ident, fk.cols))}) IN (SELECT {', '.join(map(ident, fk.tcols))} "
                f"FROM {ident(fk.target)} WHERE {condition})")

    # The tables outside any company that deleting a user cascades into.
    reached = {"users"}

    def carries(fk: ForeignKey) -> bool:
        return fk.cascades and fk.target in reached and fk.target not in company

    while grown := {name for name in set(schema) - company - reached if any(map(carries, schema[name].fks))}:
        reached |= grown

    def going(name: str, seen: frozenset[str]) -> str:
        """The condition picking the rows of ``name`` deleted with the user. Rows reached
        again through a loop of cascading keys are not bounded here, so all of that
        table's rows count."""
        if name == "users":
            return f"id = {user}.id"
        conds = []
        for fk in filter(carries, schema[name].fks):
            if fk.target in seen:
                return "TRUE"
            conds.append(picks(fk, going(fk.target, seen | {fk.target})))
        return " OR ".join(conds)

    deleted = {name: going(name, frozenset({name})) for name in ["users", *sorted(reached - {"users"})]}
    refs = [f"NOT EXISTS (SELECT 1 FROM {ident(name)} WHERE {picks(fk, deleted[fk.target])})"
            for name in sorted(schema) for fk in schema[name].fks
            if fk.target in deleted and not (fk.cascades and name in deleted and name != "users")]
    return " AND ".join([f"DELETE FROM users AS {user} WHERE {user}.id = ANY(:members)", *refs])
