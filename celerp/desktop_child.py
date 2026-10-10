# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Stops a server of the desktop app when the app exits, also when it is killed.

The app holds the other end of the server's stdin, and the operating system closes it
when the app's process ends, however it ends. The app runs its servers this way on
Linux and macOS; on Windows its job object already ends them.

  python -m celerp.desktop_child MODULE ARGS...   runs a Python module here, as -m would
  python -I desktop_child.py PROGRAM ARGS...      runs a program, by absolute path, as a child
"""
import os
import runpy
import signal
import subprocess
import sys
import threading


def _when_stdin_closes(action) -> None:
    threading.Thread(target=lambda: (sys.stdin.buffer.read(), action()), daemon=True).start()


def main(argv: list[str]) -> int:
    if os.path.isabs(argv[0]):
        program = subprocess.Popen(argv, stdin=subprocess.DEVNULL)

        def stop(*_) -> None:
            program.send_signal(signal.SIGINT)

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        _when_stdin_closes(stop)
        return program.wait()
    _when_stdin_closes(lambda: os.kill(os.getpid(), signal.SIGTERM))
    sys.argv = argv
    runpy.run_module(argv[0], run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
