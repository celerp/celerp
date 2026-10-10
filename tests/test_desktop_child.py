# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A server started by the desktop app stops when the app is killed (SIGKILL), with real processes."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows ends the app's children with its job object")

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_CHILD = ROOT / "celerp" / "desktop_child.py"

# Stands in for the app: starts one server with its stdin on a pipe, reports the
# server's pid and first line of output, then waits to be killed.
APP = """
import subprocess, sys
server = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd={root!r})
print(server.pid, server.stdout.readline().decode().strip(), flush=True)
sys.stdin.read()
"""

# Stands in for postgres: writes the signal that stopped it.
PROGRAM = """
import signal, sys, time
def stop(number, frame):
    open(sys.argv[1], "w").write(signal.Signals(number).name)
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
print("ready", flush=True)
time.sleep(600)
"""

SERVER = """
import signal, sys, time
def stop(number, frame):
    open(sys.argv[1], "w").write(signal.Signals(number).name)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print("ready", flush=True)
time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")


def _gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _start(server: list[str]):
    app = subprocess.Popen([sys.executable, "-c", APP.format(root=str(ROOT)), *server],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    pid, ready = app.stdout.readline().decode().split(" ", 1)
    return app, int(pid), ready.strip()


@pytest.fixture
def module(tmp_path, monkeypatch):
    package = tmp_path / "zz_server"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(SERVER)
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path}{os.pathsep}{ROOT}")
    return "zz_server"


def test_a_python_server_stops_when_the_app_is_killed(tmp_path, module):
    stopped = tmp_path / "stopped"
    app, pid, ready = _start([sys.executable, "-m", "celerp.desktop_child", module, str(stopped)])
    try:
        assert (ready, _alive(pid)) == ("ready", True)
        app.kill()
        app.wait()
        assert _gone(pid)
        assert stopped.read_text() == "SIGTERM"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        app.kill()
        app.wait()


def test_a_program_stops_when_the_app_is_killed_and_on_a_normal_stop(tmp_path):
    for case in ("app killed", "stopped"):
        stopped = tmp_path / case
        program = tmp_path / "program.py"
        program.write_text(PROGRAM)
        app, pid, ready = _start([sys.executable, "-I", str(DESKTOP_CHILD), sys.executable,
                                            str(program), str(stopped)])
        try:
            assert (ready, _alive(pid)) == ("ready", True)
            if case == "app killed":
                app.kill()
                app.wait()
            else:
                os.kill(pid, signal.SIGINT)
            assert _gone(pid)
            assert stopped.read_text() == "SIGINT"
        finally:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
            app.kill()
            app.wait()
