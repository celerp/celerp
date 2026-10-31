# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A row other tables refer to is locked FOR UPDATE only where the transaction must hold its
key: it removes the row, changes a unique key of it, or must stop new rows referring to it.
FOR UPDATE makes every insert of a row referring to it wait, which an UPDATE of the row
never does, and the lock checker ranks it as the key (key:<t>, after every row lock), so
locking a row FOR UPDATE only to change it orders the key before locks it never needed.
Every other lock of such a row is FOR NO KEY UPDATE, the lock an UPDATE of it takes."""
from __future__ import annotations

import lock_rank

# "path function table" -> why the function holds the row's key.
KEY_LOCKS = {
    "celerp/notifications/service.py _write_pending notifications":
        "a commit's notice writes update or remove the notices it locks",
    "celerp/routers/companies.py delete_location locations":
        "removes the location, after an import placing items there commits",
    "celerp/services/migrations.py discard migration_runs":
        "removes the staged company's runs, this one included",
    "celerp/services/company_lock.py lock_company_for_deletion companies":
        "removes the company",
}


def test_a_row_others_refer_to_is_locked_for_update_only_where_its_key_must_be_held():
    assert lock_rank.key_locks() == sorted(KEY_LOCKS)
