# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The database's own tables, columns and foreign keys, read from the Postgres catalog.

What the database holds can be more than the code loaded today knows about: a module
switched off keeps its tables. Anything that must account for every table a company
has rows in (a backup, a factory reset) reads it here rather than from the ORM."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import sqlstate


@dataclass(frozen=True)
class Column:
    udt: str
    notnull: bool
    generated: bool


class ForeignKey(NamedTuple):
    cols: tuple[str, ...]
    target: str  # ``schema.table`` when the table is in another schema
    tcols: tuple[str, ...]
    on_delete: str  # pg_constraint.confdeltype: a no action, r restrict, c cascade, n set null, d set default
    own: bool  # declared on the table itself, naming a table of this schema
    partial: bool  # declared on one partition of the table, or naming one partition of the target

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
    # Every key a table's rows are bound by: its own, those one of its partitions holds,
    # and those naming another schema. Postgres also keeps a copy of a key for each
    # partition it reaches; the key itself already says all a copy does. A key naming a
    # partition names rows of the partitioned table, so it names that table.
    for rel, kind, cols, target, tcols, on_delete, own, partial in (await session.execute(text(
            "SELECT r.relname::text, k.contype::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY u(n, i) "
            "      JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n ORDER BY u.i), "
            "CASE WHEN f.relnamespace = r.relnamespace THEN f.relname ELSE format('%s.%s', fn.nspname, f.relname) END, "
            "ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY u(n, i) "
            "      JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n ORDER BY u.i), "
            "k.confdeltype::text, c.oid = r.oid AND f.relnamespace = r.relnamespace, "
            "c.oid <> r.oid OR f.oid <> k.confrelid "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "JOIN pg_class r ON r.oid = COALESCE(pg_partition_root(c.oid), c.oid) "
            "JOIN pg_namespace n ON n.oid = r.relnamespace "
            "LEFT JOIN pg_class f ON f.oid = COALESCE(pg_partition_root(k.confrelid), k.confrelid) "
            "LEFT JOIN pg_namespace fn ON fn.oid = f.relnamespace "
            "WHERE n.nspname = current_schema() AND k.conparentid = 0 "
            "AND (k.contype = 'f' OR k.contype = 'p' AND c.oid = r.oid) "
            "ORDER BY r.relname, k.conname"))).all():
        table = tables.get(rel)
        if table is None:
            continue
        if kind == "p":
            table.pk = tuple(cols)
        else:
            table.fks.append(ForeignKey(tuple(cols), target, tuple(tcols), on_delete, own, partial))
    return tables


def own_keys(schema: dict[str, Table]) -> dict[str, Table]:
    """The catalog with each table keeping only the keys declared on it into this schema:
    the ones a reset or discard follows to find whose rows are whose. A key into another
    schema leaves this schema's rows as they are. A key one partition holds, or one
    naming a partition, or a table inheriting rows, makes them refuse
    (``changed_outside``)."""
    return {name: Table(name, table.columns, table.pk, [fk for fk in table.fks if fk.own])
            for name, table in schema.items()}


async def outside_referrer(session: AsyncSession) -> str | None:
    """A table of another schema with a foreign key into this one, as ``schema.table``, or
    None. Its rows are not in this catalog, so nothing reading it can tell whose they are."""
    return await session.scalar(text(
        "SELECT format('%I.%I', rn.nspname, r.relname) FROM pg_constraint k "
        "JOIN pg_class r ON r.oid = k.conrelid JOIN pg_namespace rn ON rn.oid = r.relnamespace "
        "JOIN pg_class f ON f.oid = k.confrelid JOIN pg_namespace fn ON fn.oid = f.relnamespace "
        "WHERE k.contype = 'f' AND fn.nspname = current_schema() AND rn.oid <> fn.oid "
        "ORDER BY 1 LIMIT 1"))


# Each table and every table it inherits rows from, at any depth, as (d, a). A partition
# inherits from its partitioned table; any other table inheriting from one does too.
_INHERITS = (
    "WITH RECURSIVE up(d, a) AS (SELECT inhrelid, inhparent FROM pg_inherits "
    "  UNION SELECT up.d, i.inhparent FROM up JOIN pg_inherits i ON i.inhrelid = up.a), "
    "here AS (SELECT to_regnamespace(current_schema()) AS ns) ")
# A table this connection's reads reach: any but another connection's temporary table.
_REACHED = "({t}.relpersistence <> 't' OR {t}.relnamespace = pg_my_temp_schema())"
# A table's name, with its schema's when another; each quoted where it must be to read one way.
_LABEL = "CASE WHEN {t}.relnamespace = here.ns THEN quote_ident({t}.relname) ELSE format('%I.%I', {n}.nspname, {t}.relname) END"


async def inheriting(session: AsyncSession) -> dict[str, str]:
    """Each table of this schema another table inherits rows from other than as a
    partition, with the first such table (``schema.table`` when in another schema).
    Reading or deleting the table's rows reaches that table's too, which none of the
    table's keys bind, so nothing reading the catalog can tell whose they are. Another
    connection's temporary tables are never reached, so they are left out; this
    connection's own count."""
    return dict((await session.execute(text(
        _INHERITS + "SELECT DISTINCT ON (a.relname) a.relname::text, " + _LABEL.format(t="d", n="dn") + " FROM up "
        "JOIN pg_class d ON d.oid = up.d JOIN pg_namespace dn ON dn.oid = d.relnamespace "
        "JOIN pg_class a ON a.oid = up.a, here "
        "WHERE NOT d.relispartition AND " + _REACHED.format(t="d") + " AND a.relnamespace = here.ns "
        "ORDER BY a.relname, 2"))).all())


# ``:t``, a table of this schema, and each table under it, as this transaction's snapshot
# of the catalog holds them.
_UNDER = (
    "WITH RECURSIVE down(oid) AS (SELECT c.oid FROM pg_class c "
    "  WHERE c.relname = :t AND c.relnamespace = to_regnamespace(current_schema()) "
    "  UNION SELECT i.inhrelid FROM down JOIN pg_inherits i ON i.inhparent = down.oid) ")
# Lock timeout and undefined table: a table being changed or gone.
_OUT_OF_REACH = {"55P03", "42P01"}


class TableElsewhere(Exception):
    """A table this connection reaches by name sits outside the schema Celerp's tables
    are in, so reading or deleting by name could miss it or reach it instead (``pin``)."""

    def __init__(self, table: str):
        super().__init__(table)
        self.table = table


async def pin(session: AsyncSession) -> None:
    """Hold this transaction to Celerp's own tables and every row of them. A table's name
    then reaches the table in the schema Celerp's tables are in, the one the catalog
    reads, even where a schema named after the connecting role comes first; and a read or
    delete row security would cut short fails instead.

    Raises ``TableElsewhere``, naming it, while a table sits in another schema this
    connection reaches tables by name in: a company's rows there would be left out of
    what is read and deleted, and a table there named like one of Celerp's would stand
    in for it."""
    elsewhere = await session.scalar(text(
        "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition "
        "AND n.nspname = ANY(current_schemas(false)) "
        "AND c.relnamespace <> (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('companies')) "
        "ORDER BY array_position(current_schemas(false), n.nspname::text), c.relname LIMIT 1"))
    if elsewhere:
        raise TableElsewhere(elsewhere)
    await session.execute(text(
        "SELECT set_config('search_path', quote_ident(n.nspname), true) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = to_regclass('companies')"))
    await session.execute(text("SET LOCAL row_security = off"))


async def hidden(session: AsyncSession) -> list[str]:
    """The tables of this schema row security keeps rows of from this connection, by
    name. Reading one returns only the rows a rule lets through, and deleting from one
    skips the others, so none of them can be backed up, reset or discarded whole."""
    return list((await session.scalars(text(
        "SELECT c.relname::text FROM pg_class c WHERE c.relnamespace = to_regnamespace(current_schema()) "
        "AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND row_security_active(c.oid) "
        "ORDER BY 1"))).all())


async def label(session: AsyncSession, name: str) -> str:
    """A table of this schema named as the catalog names it, quoted where it must be."""
    return await session.scalar(text("SELECT quote_ident(:t)"), {"t": name})


async def _stored(session: AsyncSession, name: str) -> dict[tuple[str, str], bool]:
    """``name`` and each table under it as this transaction's snapshot of the catalog
    holds them, as (schema, table), each with whether it stores rows of its own (a
    partitioned table stores none)."""
    rows = await session.execute(text(
        _UNDER + "SELECT n.nspname::text, c.relname::text, c.relkind <> 'p' FROM down JOIN pg_class c ON c.oid = down.oid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE " + _REACHED.format(t="c")), {"t": name})
    return {(ns, rel): stores for ns, rel, stores in rows}


def _scans(plan, found: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The tables ``plan`` reads rows of."""
    if isinstance(plan, dict):
        if "Relation Name" in plan:
            found.add((plan["Schema"], plan["Relation Name"]))
        for value in plan.values():
            _scans(value, found)
    elif isinstance(plan, list):
        for value in plan:
            _scans(value, found)
    return found


async def reshaped(session: AsyncSession, names: list[str]) -> str | None:
    """The first of ``names`` a read now reaches other tables of than this transaction's
    snapshot of the catalog holds under it, named as the catalog names it, or None. A
    repeatable-read transaction reads rows as they were when it began, but a read reaches
    the tables joined to the one read (by inheritance or as a partition) as they are now;
    once a table is joined, detached or swapped meanwhile, the rows read are no longer
    the rows the table held."""
    for name in names:
        plan = await session.scalar(text(f"EXPLAIN (VERBOSE, FORMAT JSON) SELECT 1 FROM {ident(name)}"))
        held = {table for table, stores in (await _stored(session, name)).items() if stores}
        if _scans(json.loads(plan) if isinstance(plan, str) else plan, set()) != held:
            return await label(session, name)
    return None


async def hold(session: AsyncSession, names: list[str]) -> str | None:
    """Lock ``names`` and every table this transaction's snapshot of the catalog holds
    under them until the transaction ends, so none of their partitions can be detached
    meanwhile, then return the first of them gone, replaced under the same name (dropped
    and made again, emptied, rewritten or swapped for another) or ``reshaped``, named as
    the catalog names it, or None. A table kept locked by another connection past the
    lock timeout counts as reshaped, as does one row security was turned on for
    (``hidden``). Rewriting a table's rows in place (VACUUM FULL, CLUSTER) stores them
    anew, so it counts as replaced too, and trying again succeeds. Writes to their rows
    go on; a table can still be joined to them, which only ``reshaped`` tells."""
    for name in names:
        tables = ", ".join(f"ONLY {ident(ns)}.{ident(rel)}" for ns, rel in await _stored(session, name))
        try:
            async with session.begin_nested():
                await session.execute(text(f"LOCK TABLE {tables} IN ACCESS SHARE MODE"))
        except DBAPIError as exc:
            if sqlstate(exc) in _OUT_OF_REACH:
                return await label(session, name)
            raise
        # The snapshot's row of each table against the table its name reaches now: another
        # table, or the same one with its rows stored anew, holds none of the rows read.
        # Row security on the table itself is checked under the lock, which keeps it as it is.
        if await session.scalar(text(
                _UNDER + "SELECT 1 FROM down JOIN pg_class c ON c.oid = down.oid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE " + _REACHED.format(t="c") + " AND ("
                "to_regclass(format('%I.%I', n.nspname, c.relname)) IS DISTINCT FROM c.oid "
                "OR pg_relation_filenode(c.oid) IS DISTINCT FROM NULLIF(c.relfilenode, 0) "
                "OR (c.relname = :t AND n.nspname = current_schema() AND row_security_active(c.oid))) LIMIT 1"),
                {"t": name}):
            return await label(session, name)
    return await reshaped(session, names)


async def partition_key(session: AsyncSession) -> str | None:
    """A partition of a table of this schema, or one in this schema, with a foreign key of
    its own or named by one, or None. It is named as ``schema.table`` when in another
    schema. The catalog holds only the partitioned table, so nothing reading it can tell
    whose rows such a key reaches."""
    return await session.scalar(text(
        _INHERITS + ", hidden AS (SELECT DISTINCT d.oid, d.relnamespace, d.relname FROM up "
        "  JOIN pg_class d ON d.oid = up.d JOIN pg_class a ON a.oid = up.a, here "
        "  WHERE d.relispartition AND (d.relnamespace = here.ns OR a.relnamespace = here.ns)) "
        "SELECT " + _LABEL.format(t="h", n="hn") + " FROM pg_constraint k "
        "JOIN hidden h ON h.oid IN (k.conrelid, k.confrelid) "
        "JOIN pg_namespace hn ON hn.oid = h.relnamespace, here "
        "WHERE k.contype = 'f' AND k.conparentid = 0 ORDER BY 1 LIMIT 1"))


async def changed_outside(session: AsyncSession) -> tuple[str, str] | None:
    """Why a reset or discard cannot tell whose rows a key or a delete reaches, as
    ``(kind, table)`` with kind ``outside_reference`` (``outside_referrer``) or
    ``partition_key`` (``partition_key``, or a table ``inheriting``), or None when every
    row they reach is one the catalog reads. A table row security keeps rows of
    (``hidden``) counts as ``partition_key`` too: a delete skips the rows it hides."""
    if table := await outside_referrer(session):
        return "outside_reference", table
    if table := await partition_key(session):
        return "partition_key", table
    if tables := await hidden(session):
        return "partition_key", await label(session, tables[0])
    if tables := await inheriting(session):
        return "partition_key", min(tables.values())
    return None


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
