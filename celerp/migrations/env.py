# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
#
# Alembic env.py — wired to Celerp SQLAlchemy models.
# Target database: PostgreSQL (production).
# SQLite is dev-only and uses create_all(); do NOT run Alembic against SQLite.

import contextlib
import os
from logging.config import fileConfig

from alembic import context
from alembic.runtime.migration import RevisionStep
from sqlalchemy import engine_from_config, pool

from celerp.db_url import sync_url as sync_db_url
from celerp.models.base import Base

# Register all models with Base.metadata by importing them.
import celerp.models.company       # noqa: F401
import celerp.models.ledger        # noqa: F401
import celerp.models.projections   # noqa: F401
import celerp.models.accounting    # noqa: F401  (UserCompany)
import celerp.models.marketplace   # noqa: F401  (re-exports MarketplaceConfig if connector installed)
try:
    import celerp_accounting.models  # noqa: F401  (Account — loaded when celerp-accounting module is present)
except ImportError:
    pass
try:
    import celerp_inventory.models_import_batch  # noqa: F401  (ImportBatch)
except ImportError:
    pass
try:
    import celerp_docs.models_share  # noqa: F401  (DocShareToken)
except ImportError:
    pass

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: alembic runs in-process during
    # `celerp migrate`/`celerp start` and the restore reconcile, so the default
    # (True) would silence every application logger already created, including
    # the reconcile's own failure logger. Configure alembic's loggers without
    # muting the app's.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/celerp")

_sync_url = sync_db_url(DATABASE_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def _refusals_first(planned_steps):
    """The run's steps, once every downgrade step's revision has been asked whether it refuses.

    Each revision commits on its own, so a refusal met part-way down would leave the
    revisions above it already undone. A revision whose downgrade can refuse says so in its
    ``downgrade_refusal(bind)``, and the whole run stops on the first one before any
    revision changes the schema. The revision asks again under its own locks when it runs."""
    def steps(heads, migration):
        planned = list(planned_steps(heads, migration))
        for step in planned:
            if isinstance(step, RevisionStep) and not step.is_upgrade:
                refusal = getattr(step.revision.module, "downgrade_refusal", None)
                reason = refusal(migration.connection) if refusal else None
                if reason:
                    raise RuntimeError(reason)
        # The checks hold nothing into the first revision's transaction.
        migration.connection.commit()
        return planned
    return steps


def run_migrations_online() -> None:
    # A run changes the schema only while it holds the migration key, which keeps every
    # other run out.
    from celerp.db import holds_migration_key
    if not holds_migration_key(_sync_url):
        raise RuntimeError(
            "Alembic changes a database only under the migration key "
            "(celerp.db.migration_key), which keeps every other migration run out.")
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _sync_url

    # Every production migration runs inside the mutating scope
    # (celerp.migrations.compatibility.mutating_scope), so it migrates through a
    # guarded engine of the fence it holds.
    from celerp.migrations.compatibility import held_fence
    held = held_fence(_sync_url)
    if held is not None:
        engines = held.engine(poolclass=pool.NullPool)
    else:
        engines = contextlib.nullcontext(
            engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool))
    with engines as connectable, connectable.connect() as connection:
        options = dict(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=False,
            # Each revision commits with its stamp before the next begins, as the CLI's
            # one-revision-per-upgrade run does: no revision holds an earlier one's locks, and
            # a failure leaves the stamp naming the last revision that completed.
            transaction_per_migration=True,
        )
        context.configure(**options)
        # The command's steps, configured again so that every refusal is asked first.
        context.configure(**options, fn=_refusals_first(context.get_context().opts["fn"]))
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
