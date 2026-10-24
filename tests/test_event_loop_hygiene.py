# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A sync test that runs its own loop leaves no unclosed loop behind.

Before each async test pytest-asyncio saves the loop ``asyncio.get_event_loop()``
returns and puts it back afterwards. On the main thread that call creates a loop when
none is set, and nobody closes it. A later sync test that runs its own loop with
``asyncio.run()`` replaces that loop, it is collected unclosed, and the
ResourceWarning fails whichever test happens to be running at that moment.

The order that drops the loop is set up in a pytest session of its own, run once
without the root conftest's ``pytest_configure`` (it must fail, or the session does
not reproduce the leak) and once with it (it must pass).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.process

_ROOT_CONFTEST = Path(__file__).resolve().parent.parent / "conftest.py"

_TESTS = '''
import asyncio
import gc


async def test_an_async_test_runs_first():
    assert asyncio.get_running_loop() is not None


def test_a_sync_test_then_runs_its_own_loop():
    asyncio.run(asyncio.sleep(0))
    gc.collect()
'''


def _root_hook() -> str:
    """The source of the root conftest's ``pytest_configure``, or "" when it has none."""
    source = _ROOT_CONFTEST.read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_configure":
            return ast.get_source_segment(source, node)
    return ""


def _session(tmp_path: Path, conftest: str) -> subprocess.CompletedProcess:
    tmp_path.mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\nfilterwarnings = error\n")
    (tmp_path / "conftest.py").write_text(conftest)
    (tmp_path / "test_order.py").write_text(_TESTS)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path), str(tmp_path / "test_order.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=50)


def test_a_sync_test_running_its_own_loop_after_an_async_test_drops_no_loop(tmp_path):
    without = _session(tmp_path / "without", "")
    assert without.returncode != 0 and "ResourceWarning" in without.stdout, without.stdout
    hook = _root_hook()
    with_hook = _session(tmp_path / "with", hook)
    assert hook and with_hook.returncode == 0, with_hook.stdout
