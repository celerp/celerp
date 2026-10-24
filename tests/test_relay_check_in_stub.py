# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The suite boots the API without checking in with the relay through one helper
(skip_relay_check_in), used by the fixture and by every child process that boots the app;
no test carries its own copy of the stub."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest_support import skip_relay_check_in

_CHECK_INS = ("_try_auto_activate", "_try_sync_existing_entitlement")
_OWN_COPY = re.compile(r'setattr\(main_mod, "(%s)", AsyncMock\(\)\)|app_main\.(%s) =' % (("|".join(_CHECK_INS),) * 2))


@pytest.mark.asyncio
async def test_both_boot_check_ins_do_nothing(monkeypatch):
    import celerp.main as main_mod

    skip_relay_check_in(monkeypatch.setattr)
    for name in _CHECK_INS:
        assert await getattr(main_mod, name)() is None


def test_no_test_stubs_the_check_in_itself():
    root = Path(__file__).parent
    copies = [f"{path.relative_to(root)}:{n}" for path in sorted(root.rglob("*.py")) if path.name != Path(__file__).name
              for n, line in enumerate(path.read_text().splitlines(), 1) if _OWN_COPY.search(line)]
    assert copies == []
