# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Store every List's counterparty as contact_id and contact_name.

Revision ID: l9a0b1c2d3e4
Revises: k8f9a0b1c2d3
Create Date: 2026-09-29

Lists stored their counterparty as customer_id and customer_name (transfers:
receiver). Lists now read contact_id and contact_name only, so this moves the
older fields onto them. A contact already on the List is kept.
"""

from __future__ import annotations

from alembic import op

from celerp.migrations._json_compat import update_projection_state

revision = "l9a0b1c2d3e4"
down_revision = "k8f9a0b1c2d3"
branch_labels = None
depends_on = None

# Hardcoded (migrations must be self-contained), in the order the List projection folds them.
_FOLD = (("customer_id", "contact_id"), ("customer_name", "contact_name"), ("receiver", "contact_name"))


def upgrade() -> None:
    def _fold(state, row):
        changed = False
        for older, field in _FOLD:
            if older not in state:
                continue
            value = state.pop(older)
            if value and not state.get(field):
                state[field] = value
            changed = True
        return changed

    update_projection_state(op.get_bind(), _fold, where="entity_type = 'list'")


def downgrade() -> None:
    pass  # Data migration - no safe downgrade
