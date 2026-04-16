# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
#
# Alembic env.py — wired to Celerp SQLAlchemy models.
# Target database: PostgreSQL (production).
# SQLite is dev-only and uses create_all(); do NOT run Alembic against SQLite.

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

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
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/celerp")

# Strip async driver prefix — Alembic's synchronous engine can't use asyncpg.
_sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


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


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _sync_url

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
