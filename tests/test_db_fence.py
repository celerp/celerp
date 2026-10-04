# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Two Celerp versions never write to one database at the same time.

Every case runs real processes against its own real Postgres database: a holder
that opened the database as one version and keeps running, and a contender of
another version that tries to open it while the holder is up, after it stopped,
and after it was killed.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from test_db_compatibility import DATABASE_URL, NEWER, RUNNING, _meta, scratch, snapshot  # noqa: F401  (scratch is a fixture)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"), reason="needs a live Postgres database"
)

OLDER = RUNNING

# Runs as Celerp argv[1] and opens the database argv[3] through path argv[2]:
#   api      the real API startup, held open until stdin closes
#   ui       the real UI startup, held open until stdin closes
#   migrate  `celerp migrate`, the update's migrate step and the start path
#   reset    `celerp reset-password`
# argv[4] is how long it waits for another version before giving up.
_RUN = r"""
import asyncio, sys
from pathlib import Path
import celerp
celerp.__version__ = sys.argv[1]
path, url, wait, data_dir = sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5]
from celerp.migrations import compatibility
compatibility.FENCE_WAIT_SECONDS = wait
from celerp.config import settings
settings.data_dir = Path(data_dir)
settings.gateway_token = ""
settings.celerp_public_url = None

def hold():
    print("READY", flush=True)
    sys.stdin.read()

if path == "api":
    import celerp.main as app_main
    async def run():
        async with app_main.lifespan(app_main.app):
            await asyncio.to_thread(hold)
    asyncio.run(run())
elif path == "ui":
    from ui.app import app
    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(hold)
    asyncio.run(run())
elif path == "migrate":
    from click.testing import CliRunner
    from celerp.cli import main
    result = CliRunner().invoke(main, ["migrate", "--db-url", url])
    print(result.output, flush=True)
    sys.exit(result.exit_code)
elif path == "reset":
    from click.testing import CliRunner
    from celerp import cli
    cli._read_config = lambda: {"database": {"url": url}}
    result = CliRunner().invoke(cli.main, ["reset-password", "--email", "nobody@example.com",
                                           "--password", "a-long-enough-password"])
    print(result.output, flush=True)
    sys.exit(result.exit_code)
"""


def _env(url: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MODULE_DIR", "ENABLED_MODULES", "CELERP_UPDATE_VERIFY")}
    env.update({"DATABASE_URL": url, "ALLOW_INSECURE_JWT": "true", "PYTHONUNBUFFERED": "1"})
    return env


_SPAWNED: list[subprocess.Popen] = []


@pytest.fixture(autouse=True)
def _no_leftover_processes():
    """A failing test never leaves a holder running against the next one."""
    yield
    while _SPAWNED:
        proc = _SPAWNED.pop()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _spawn(version: str, path: str, url: str, data_dir, *, wait: float = 60.0) -> subprocess.Popen:
    """Output goes to a file beside *data_dir*, never a pipe nobody drains: a holder
    logs while it runs and would block on a full pipe instead of stopping."""
    log = f"{data_dir}.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", _RUN, version, path, url, str(wait), str(data_dir)],
        stdin=subprocess.PIPE, stdout=open(log, "w"), stderr=subprocess.STDOUT,
        text=True, env=_env(url))
    proc.log = log
    _SPAWNED.append(proc)
    return proc


def _output(proc: subprocess.Popen) -> str:
    with open(proc.log) as f:
        return f.read()


def _hold(version: str, path: str, url: str, data_dir) -> subprocess.Popen:
    """A process of *version* that opened the database through *path* and keeps it open."""
    proc = _spawn(version, path, url, data_dir)
    deadline = time.monotonic() + 120
    while "READY" not in _output(proc).splitlines():
        assert proc.poll() is None and time.monotonic() < deadline, _output(proc)
        time.sleep(0.2)
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.stdin.close()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise


def _finish(proc: subprocess.Popen) -> tuple[int, str]:
    try:
        proc.wait(timeout=180)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.returncode, _output(proc)


def _run(version: str, path: str, url: str, data_dir, *, wait: float) -> tuple[int, str]:
    return _finish(_spawn(version, path, url, data_dir, wait=wait))


def _still_waiting(proc: subprocess.Popen, url: str, before: dict, seconds: float = 8.0) -> None:
    """*proc* has neither finished nor changed the database for *seconds*."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert proc.poll() is None, _output(proc)
        time.sleep(0.5)
    assert snapshot(url) == before


@pytest.mark.parametrize("holder", ["api", "ui"])
def test_newer_waits_for_the_older_version_to_stop(scratch, tmp_path, holder):
    url = scratch()
    old = _hold(OLDER, holder, url, tmp_path / "old")
    try:
        before = snapshot(url)
        new = _spawn(NEWER, "migrate", url, tmp_path / "new")
        _still_waiting(new, url, before)
        _stop(old)
        code, out = _finish(new)
        assert code == 0, out
    finally:
        _stop(old)
    assert _meta(url)["newest_celerp_version"] == NEWER


def test_newer_is_refused_while_the_older_version_keeps_running(scratch, tmp_path):
    url = scratch()
    old = _hold(OLDER, "api", url, tmp_path / "old")
    try:
        before = snapshot(url)
        code, out = _run(NEWER, "migrate", url, tmp_path / "new", wait=2)
        assert code == 1, out
        assert "Another version of Celerp is still using this data" in out, out
        assert "Nothing was changed" in out, out
        assert snapshot(url) == before
        assert old.poll() is None
    finally:
        _stop(old)


def test_newer_starts_once_the_older_holder_is_killed(scratch, tmp_path):
    url = scratch()
    old = _hold(OLDER, "api", url, tmp_path / "old")
    try:
        before = snapshot(url)
        new = _spawn(NEWER, "migrate", url, tmp_path / "new")
        _still_waiting(new, url, before)
        old.send_signal(signal.SIGKILL)
        old.wait(timeout=30)
        code, out = _finish(new)
        assert code == 0, out
    finally:
        _stop(old)
    assert _meta(url)["newest_celerp_version"] == NEWER


@pytest.mark.parametrize("path", ["api", "ui", "migrate", "reset"])
def test_older_version_cannot_open_while_the_newer_one_runs(scratch, tmp_path, path):
    url = scratch()
    new = _hold(NEWER, "api", url, tmp_path / "new")
    try:
        before = snapshot(url)
        code, out = _run(OLDER, path, url, tmp_path / "old", wait=60)
        assert code == 1, out
        assert f"last opened with Celerp {NEWER}" in out, out
        assert snapshot(url) == before
    finally:
        _stop(new)


@pytest.mark.parametrize("path", ["api", "ui", "migrate", "reset"])
def test_older_version_cannot_open_after_the_newer_one_stopped(scratch, tmp_path, path):
    url = scratch()
    _stop(_hold(NEWER, "api", url, tmp_path / "new"))
    before = snapshot(url)
    code, out = _run(OLDER, path, url, tmp_path / "old", wait=60)
    assert code == 1, out
    assert f"last opened with Celerp {NEWER}" in out, out
    assert snapshot(url) == before


def test_the_same_version_shares_the_database(scratch, tmp_path):
    url = scratch()
    api = _hold(RUNNING, "api", url, tmp_path / "a")
    ui = _hold(RUNNING, "ui", url, tmp_path / "b")
    try:
        code, out = _run(RUNNING, "migrate", url, tmp_path / "c", wait=2)
        assert code == 0, out
        code, out = _run(RUNNING, "reset", url, tmp_path / "d", wait=2)
        assert "No user found" in out, out
    finally:
        _stop(ui)
        _stop(api)
