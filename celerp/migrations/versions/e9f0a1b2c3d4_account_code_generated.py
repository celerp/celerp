# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record on each account whether an importer made its code up.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-10-05

An account whose source books carried no code is given one by the migration, made
from the run id and the source account's id. Users see such an account by its name.
The flag replaces reading that from the code's spelling, which also hid a code
someone chose that happened to look the same.

Existing accounts are marked only where the migration's own record proves it: the
run's map row for the source account, in the account's company, whose code is the
one made from that run and source id (with the "-n" suffix a clash adds). Every other
account keeps showing its code.

The accounts table belongs to the accounting module and is created from its models
after core migrations run, so on a new install there is nothing to alter here.
"""

from __future__ import annotations

import re
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def _columns(table: str) -> list[str]:
    inspector = inspect(op.get_bind())
    return [c["name"] for c in inspector.get_columns(table)] if inspector.has_table(table) else []


def _made_up(run_id, source_external_id: str, code: str) -> bool:
    """Whether ``code`` is the one the migration sink made for this source account
    (migration_core_sink.deterministic_id, accounting_roles.generated_account_code)."""
    made = "M" + uuid.uuid5(uuid.UUID(str(run_id)), f"account:{source_external_id}").hex[:8]
    return re.fullmatch(re.escape(made) + r"(-\d+)?", code) is not None


def upgrade() -> None:
    columns = _columns("accounts")
    if not columns or "code_generated" in columns:
        return
    op.add_column("accounts", sa.Column("code_generated", sa.Boolean, nullable=False, server_default=sa.false()))
    if not (_columns("migration_entity_maps") and _columns("migration_runs")):
        return
    bind = op.get_bind()
    mapped = bind.execute(sa.text(
        "SELECT r.company_id, m.migration_run_id, m.source_external_id, m.target_entity_id"
        " FROM migration_entity_maps m JOIN migration_runs r ON r.id = m.migration_run_id"
        " WHERE m.target_entity_type = 'account'"
    )).all()
    for company_id, run_id, source_external_id, code in mapped:
        if _made_up(run_id, source_external_id, code):
            bind.execute(sa.text(
                "UPDATE accounts SET code_generated = TRUE"
                " WHERE CAST(company_id AS text) = CAST(:c AS text) AND code = :code"
            ), {"c": str(company_id), "code": code})


def downgrade() -> None:
    if "code_generated" in _columns("accounts"):
        op.drop_column("accounts", "code_generated")
