# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The record checks have one home, the admin module's POST /admin/doctor. No second,
unmounted copy of them is kept in core."""
from __future__ import annotations

import importlib.util


def test_core_carries_no_copy_of_the_doctor():
    assert importlib.util.find_spec("celerp.routers.doctor") is None


def test_the_record_checks_come_from_the_admin_module():
    from celerp_admin.routes import _check_stale_projections

    assert _check_stale_projections.__module__ == "celerp_admin.routes"
