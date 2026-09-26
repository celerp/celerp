# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Where the running Celerp code lives, and the switch between releases.

A pip install runs the release pip put in the environment (the base). A
self-update never changes the base: it installs the new release, dependencies
included, into its own directory under `<config dir>/runtime/`, and only once
that release has migrated and started cleanly does it point `runtime/current`
at it. `celerp` then runs the release `current` names, with that directory
first on PYTHONPATH. Stopping at any moment leaves either the old release or the
new one complete, never a mix.

A pointer to a release no newer than the base is ignored, so upgrading the base
with pip (or reinstalling it) always wins over an older self-update.
"""

from __future__ import annotations

import os
import shutil
from importlib import metadata
from pathlib import Path

from packaging.version import InvalidVersion, Version

PKG_ROOT_ENV = "CELERP_PKG_ROOT"  # set in every process running a release directory
SUPERVISOR_PIPE_ENV = "CELERP_SUPERVISOR_PIPE"
UPDATE_VERIFY_ENV = "CELERP_UPDATE_VERIFY"
_PARTIAL = ".partial"


def package_root() -> Path:
    """The directory the running `celerp` package was imported from."""
    return Path(__file__).resolve().parent.parent


def _runtime_dir() -> Path:
    from celerp.config import config_path
    return config_path().parent / "runtime"


def release_dir(version: str) -> Path:
    return _runtime_dir() / version


def staging_dir(version: str) -> Path:
    return _runtime_dir() / f"{version}{_PARTIAL}"


def _pointer() -> Path:
    return _runtime_dir() / "current"


def pointed() -> str | None:
    try:
        return _pointer().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def active() -> Path | None:
    """The release directory `celerp` should run, or None for the base."""
    try:
        version = pointed()
        if not version or not release_dir(version).is_dir():
            return None
        if Version(version) <= Version(metadata.version("celerp")):
            return None
        return release_dir(version)
    except (InvalidVersion, metadata.PackageNotFoundError, OSError):
        return None


def switch(version: str) -> None:
    """Make `version` the release `celerp` runs from the next start. Atomic."""
    from celerp.config_store import atomic_write_text
    _runtime_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(_pointer()), version)


def discard(version: str) -> None:
    """Remove a staged `version` unless it is the one `current` points at."""
    shutil.rmtree(staging_dir(version), ignore_errors=True)
    if pointed() != version:
        shutil.rmtree(release_dir(version), ignore_errors=True)


def prune(keep: set[str]) -> None:
    """Remove release directories other than `keep` and the one `current` names.
    A directory still in use (Windows) is left for the next update."""
    root = _runtime_dir()
    keep = keep | {pointed() or ""}
    for path in root.iterdir() if root.is_dir() else ():
        if path.is_dir() and path.name not in keep:
            shutil.rmtree(path, ignore_errors=True)


def watch_supervisor_pipe() -> None:
    """Exit a supervised server when the process that launched it disappears.

    celerp start gives each API/UI child an anonymous stdin pipe whose write
    end exists only in the supervisor. EOF therefore means the supervisor died,
    including a hard kill, without PID files or platform-specific process APIs.
    """
    if os.environ.get(SUPERVISOR_PIPE_ENV) != "1":
        return

    import threading

    def _watch() -> None:
        try:
            while os.read(0, 1):
                pass
        except OSError:
            pass
        os._exit(1)

    threading.Thread(target=_watch, name="supervisor-watch", daemon=True).start()


def base_env(env: dict | None = None) -> dict:
    """`env` (default os.environ) without the release this process runs, so a
    child started with it picks its release afresh."""
    env = dict(os.environ if env is None else env)
    root = env.pop(PKG_ROOT_ENV, None)
    if root and env.get("PYTHONPATH"):
        kept = [p for p in env["PYTHONPATH"].split(os.pathsep)
                if p and os.path.normpath(p) != os.path.normpath(root)]
        if kept:
            env["PYTHONPATH"] = os.pathsep.join(kept)
        else:
            env.pop("PYTHONPATH")
    return env


def release_env(root: Path, env: dict | None = None, extra_paths: list[str] = ()) -> dict:
    """`env` set to run the release at `root`, with `extra_paths` after it on
    PYTHONPATH."""
    env = base_env(env)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), *extra_paths, env.get("PYTHONPATH", "")]))
    env[PKG_ROOT_ENV] = str(root)
    return env
