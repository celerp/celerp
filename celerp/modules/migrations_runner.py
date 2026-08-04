# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Core-owned, forward-only runtime migration runner for enabled modules.

At API startup, before ``load_all``, each enabled module's manifest-declared
migration files are applied against the live database under the shared Postgres
advisory lock. Migrations are expected to be inspector-guarded so a re-run is a
no-op: the runner re-applies them every boot and relies on those guards for
idempotence rather than tracking a per-module version stamp.

Isolation of blast radius:
  - Every touched table name must start with the module's declared table_prefix
    (``GuardedOperations``), so a module migration cannot alter core or a sibling
    module's tables through the ``op.*`` proxy.
  - A third-party module whose migration fails is rolled back and dropped from
    the enabled set, its error held for the caller to surface; a first-party
    module's failure re-raises, matching the loader's trust policy.
  - Each per-module transaction bounds itself with SET LOCAL statement/lock
    timeouts, so a hung upgrade becomes a caught failure instead of a boot stall.
"""
from __future__ import annotations

import importlib.util
import uuid
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from celerp.db import _MIGRATION_LOCK_KEY, mask_db_credentials
from celerp.modules import loader

# A prefix is well-formed when it is a non-empty string of at least this length
# that ends in an underscore, so "acme_" scopes cleanly and can never be a bare
# word that swallows unrelated tables.
_MIN_PREFIX_LEN = 3

# Bounds every per-module migration transaction. A lock wait or runaway statement
# past this becomes a caught per-module failure, never an unbounded boot stall.
_MIGRATION_TIMEOUT = "30s"


def _prefix_ok(prefix: str) -> bool:
    return bool(prefix) and len(prefix) >= _MIN_PREFIX_LEN and prefix.endswith("_")


class GuardedOperations(Operations):
    """An ``Operations`` whose table-touching DDL is refused unless the table
    name starts with the module's ``table_prefix``.

    This is an accident seatbelt, not a sandbox: a migration that reaches for the
    raw connection via ``op.get_bind()`` and issues arbitrary SQL bypasses it
    entirely. That is accepted, because module code already runs in-process; the
    guard exists to turn an honest mistake (a typo'd table name, a copied
    migration) into a clear, isolated failure instead of a silent write to a core
    table.

    Alembic's ``Operations.context`` hardcodes ``Operations(migration_context)``
    and its proxy install reads ``self._setups[self.__class__]``. This subclass
    therefore overrides ``context`` to build ``cls(...)`` with the prefix and
    registers itself in the setup map, so the installed ``op.*`` proxy dispatches
    to these guarded overrides.
    """

    def __init__(self, migration_context, table_prefix, impl=None):
        super().__init__(migration_context, impl=impl)
        self._table_prefix = table_prefix

    @classmethod
    @contextmanager
    def context(cls, migration_context, table_prefix):
        if cls not in cls._setups:
            cls._setups[cls] = Operations._setups[Operations]
        op = cls(migration_context, table_prefix)
        op._install_proxy()
        try:
            yield op
        finally:
            op._remove_proxy()

    def _check(self, table_name) -> None:
        if not str(table_name or "").startswith(self._table_prefix):
            raise ValueError(
                f"Module migration attempted DDL on {table_name!r}, which is "
                f"outside the module's table prefix {self._table_prefix!r}. "
                "Module migrations may only touch their own prefixed tables."
            )

    def create_table(self, table_name, *columns, **kw):
        self._check(table_name)
        return super().create_table(table_name, *columns, **kw)

    def drop_table(self, table_name, **kw):
        self._check(table_name)
        return super().drop_table(table_name, **kw)

    def add_column(self, table_name, column, **kw):
        self._check(table_name)
        return super().add_column(table_name, column, **kw)

    def drop_column(self, table_name, column_name, **kw):
        self._check(table_name)
        return super().drop_column(table_name, column_name, **kw)

    def alter_column(self, table_name, column_name, **kw):
        self._check(table_name)
        return super().alter_column(table_name, column_name, **kw)

    def create_index(self, index_name, table_name, *args, **kw):
        self._check(table_name)
        return super().create_index(index_name, table_name, *args, **kw)

    def drop_index(self, index_name, table_name=None, **kw):
        # A bare drop_index(name) still must not escape the prefix; guard the
        # table when named, otherwise the index name itself.
        self._check(table_name if table_name is not None else index_name)
        return super().drop_index(index_name, table_name, **kw)

    def rename_table(self, old_table_name, new_table_name, **kw):
        self._check(old_table_name)
        self._check(new_table_name)
        return super().rename_table(old_table_name, new_table_name, **kw)


def run_module_migrations(sync_conn, module_name, module_path, migrations_pkg,
                          table_prefix) -> None:
    """Apply every migration file for one module against ``sync_conn``, in order.

    ``sync_conn`` is the plain Connection handed by ``AsyncConnection.run_sync``,
    already inside a transaction the caller commits or rolls back. The migration
    files are discovered under ``module_path/<migrations_pkg dotted path>`` and
    run sorted by filename; files starting with ``_`` are skipped. Each file must
    define ``upgrade()``, which is executed with the guarded ``op.*`` proxy bound.

    Fails closed on a malformed prefix. Exceptions propagate to the caller (the
    phase), which decides isolate-or-reraise. An absent or empty migrations
    directory is a no-op, not an error.
    """
    if not _prefix_ok(table_prefix):
        raise ValueError(
            f"Module {module_name!r} declares migrations but its table_prefix "
            f"{table_prefix!r} is malformed (need >= {_MIN_PREFIX_LEN} characters, "
            "ending in '_'); refusing to run its migrations."
        )

    sync_conn.execute(sa.text(f"SET LOCAL lock_timeout = '{_MIGRATION_TIMEOUT}'"))
    sync_conn.execute(sa.text(f"SET LOCAL statement_timeout = '{_MIGRATION_TIMEOUT}'"))

    mig_dir = Path(module_path).joinpath(*migrations_pkg.split("."))
    if not mig_dir.is_dir():
        return
    files = sorted(
        p for p in mig_dir.glob("*.py")
        if p.is_file() and not p.name.startswith("_")
    )
    for path in files:
        _run_migration_file(sync_conn, path, table_prefix)


def _run_migration_file(sync_conn, path: Path, table_prefix: str) -> None:
    # Load with a unique synthetic name and do NOT register it in sys.modules, so
    # re-running the same file every boot never leaves module state behind.
    spec = importlib.util.spec_from_file_location(
        f"_celerp_module_migration_{uuid.uuid4().hex}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration file {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    upgrade = getattr(mod, "upgrade", None)
    if not callable(upgrade):
        raise AttributeError(f"Migration {path.name} defines no upgrade() function.")
    with GuardedOperations.context(MigrationContext.configure(sync_conn), table_prefix):
        upgrade()


async def run_migration_phase(engine, enabled):
    """Run the enabled modules' migrations under the shared advisory lock.

    Returns ``(surviving, held_errors)``: ``surviving`` is the enabled set minus
    any third-party module whose migration failed, and ``held_errors`` maps each
    such module to its masked failure message for the caller to record with
    ``loader.record_load_error`` AFTER ``load_all`` (which clears the load-error
    map on entry). A first-party module's failure re-raises.

    Postgres only: on any other dialect the phase is skipped and
    ``(set(enabled), {})`` is returned unchanged, leaving the SQLite dev path on
    its ``create_all`` fallback.
    """
    surviving = set(enabled)
    held_errors: dict[str, str] = {}
    if engine.dialect.name != "postgresql":
        return surviving, held_errors

    lock_conn = await engine.connect()
    lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
    try:
        await lock_conn.execute(
            sa.text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
        )
        try:
            for name in sorted(enabled):
                module_path = loader.resolve_module_path(name)
                if module_path is None:
                    continue
                manifest = loader.read_manifest(module_path)
                migrations_pkg = manifest.get("migrations")
                if not migrations_pkg:
                    continue
                table_prefix = manifest.get("table_prefix") or ""
                try:
                    async with engine.begin() as conn:
                        await conn.run_sync(
                            run_module_migrations, name, module_path,
                            migrations_pkg, table_prefix,
                        )
                except Exception as exc:
                    if loader.is_first_party(module_path):
                        raise
                    held_errors[name] = mask_db_credentials(
                        f"Migration failed: {type(exc).__name__}: {exc}"
                    )
                    surviving.discard(name)
        finally:
            await lock_conn.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
    finally:
        await lock_conn.close()
    return surviving, held_errors
