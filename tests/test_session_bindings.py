# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A module first imported while a test patched celerp.db keeps no patched session factory."""

from __future__ import annotations

import sys
import types

import celerp.db
from conftest_support import restore_session_bindings


def test_a_module_that_bound_a_patched_session_factory_is_pointed_back_at_celerp_db(monkeypatch):
    def scratch():
        raise AssertionError("a dropped scratch database")

    probe = types.ModuleType("celerp._session_binding_probe")
    with monkeypatch.context() as patched:
        patched.setattr(celerp.db, "get_session_ctx", scratch)
        probe.get_session_ctx = celerp.db.get_session_ctx  # what `from celerp.db import ...` does
        probe.SessionLocal = celerp.db.SessionLocal
    monkeypatch.setitem(sys.modules, probe.__name__, probe)

    assert restore_session_bindings() == ["celerp._session_binding_probe.get_session_ctx"]
    assert probe.get_session_ctx is celerp.db.get_session_ctx
    assert probe.SessionLocal is celerp.db.SessionLocal
