# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Embedded PostgreSQL provider for pip installs.

The pip package ships client drivers only; a self-hosted user with no system
Postgres previously hit "connection refused" on `celerp init`. This module boots
a self-contained PostgreSQL cluster (bundled binaries, no sudo, no system
service) under the user's config directory, giving the pip path the same
zero-setup experience the Electron desktop app already provides.

Everything provider-specific (currently `pgserver`) is isolated here behind a
tiny interface — `is_available()`, `ensure_cluster()`, `bin_dir()`, `wipe()` —
so the backing implementation can be swapped (e.g. for our own PostgreSQL wheels
built from the same binaries the desktop app uses) without touching the CLI.

The cluster is loopback/unix-socket only and single-user: it is never bound to a
TCP port on POSIX and never exposed off-host. `celerp init` only falls back to it
when no PostgreSQL server is already reachable — an existing server always wins.
"""

from __future__ import annotations

from pathlib import Path

# Name of the application database created inside the embedded cluster. The
# cluster's bootstrap superuser is `postgres`; the app connects as that user
# over a unix socket, so no role/password provisioning (and no sudo) is needed.
_DATABASE = "celerp"


def is_available() -> bool:
    """True if the embedded-Postgres backend can run on this interpreter.

    False on platforms with no prebuilt wheel (CPython ≥3.13, linux-aarch64,
    …), where the marker-guarded dependency is simply absent. Callers treat a
    False here as "embedded mode unavailable" and fall back to external guidance.
    """
    try:
        import pgserver  # noqa: F401
    except Exception:
        return False
    return True


def pgdata_dir(config_dir: Path) -> Path:
    """Location of the embedded cluster's data directory.

    Kept next to config.toml (`~/.config/celerp/pgdata`) rather than under the
    cwd-relative `settings.data_dir`, so the cluster is found regardless of which
    directory `celerp` is invoked from. Mirrors the Electron app keeping its
    cluster under `userData/celerp-data/postgres/`.
    """
    return Path(config_dir) / "pgdata"


def ensure_cluster(config_dir: Path) -> str:
    """Boot the embedded cluster (initdb on first run, start otherwise) and
    ensure the `celerp` database exists. Returns an asyncpg connection URI.

    Idempotent: pgserver refcounts by pgdata path, so repeated calls in one
    process return the same running server. The server is stopped (data
    preserved) when the process exits, via pgserver's `cleanup_mode='stop'`
    handle — the SIGTERM/SIGINT handlers in `celerp start` exit through
    `sys.exit`, which runs that atexit hook, so no postmaster is orphaned.
    """
    import pgserver

    config_dir = Path(config_dir)
    # get_server requires pgdata's PARENT to exist; it creates pgdata itself.
    config_dir.mkdir(parents=True, exist_ok=True)
    pgdata = pgdata_dir(config_dir)

    server = pgserver.get_server(pgdata, cleanup_mode="stop")
    _ensure_app_database(server)

    uri = server.get_uri(database=_DATABASE)
    # pgserver yields a psycopg2-style URI; the app connects via asyncpg.
    return uri.replace("postgresql://", "postgresql+asyncpg://", 1)


def _ensure_app_database(server) -> None:
    """Create the `celerp` database if the cluster only has the default one.

    A fresh cluster ships with just `postgres`; `get_server` does not create our
    app DB. The connecting user is the cluster superuser, so the app owns every
    object it creates — none of the `sudo -u postgres` ownership/grant dance the
    external path needs applies here.
    """
    out = server.psql(
        f"SELECT 1 FROM pg_database WHERE datname = '{_DATABASE}'"
    )
    if "(1 row)" not in out:
        server.psql(f"CREATE DATABASE {_DATABASE}")


def bin_dir() -> str | None:
    """Directory holding the bundled `pg_dump`/`pg_restore`, or None if the
    backend is unavailable. Wired into `[backup] pg_bin_dir` so backups use the
    matching-version tools rather than a system pg_dump of unknown version.
    """
    try:
        import pgserver
    except Exception:
        return None
    cand = Path(pgserver.__file__).parent / "pginstall" / "bin"
    return str(cand) if cand.is_dir() else None


def wipe(config_dir: Path) -> None:
    """Stop the cluster and delete its data directory (for `init --force`).

    Best-effort: a missing/already-stopped cluster is fine. After this the next
    `ensure_cluster` initdb's a fresh cluster.
    """
    import shutil

    pgdata = pgdata_dir(config_dir)
    if not pgdata.exists():
        return
    try:
        import pgserver

        # cleanup_mode='delete' is what actually stops the postmaster AND removes
        # pgdata; 'stop' only stops it. cleanup() is gated on this process holding
        # the last handle, which a fresh `init --force` process does.
        server = pgserver.get_server(pgdata, cleanup_mode="delete")
        server.cleanup()
    except Exception:
        # Provider missing, or a stale cross-process handle made pgserver decline
        # to act — fall through to the unconditional rmtree below.
        pass
    # Belt-and-suspenders: guarantee the directory is gone even if pgserver's
    # refcount bookkeeping left it (a crashed prior process can do this).
    if pgdata.exists():
        _force_stop_postmaster(pgdata)
        shutil.rmtree(pgdata, ignore_errors=True)


def _force_stop_postmaster(pgdata: Path) -> None:
    """Kill a postmaster still holding `pgdata`, so the dir can be removed.

    Reads the PID from postmaster.pid (Postgres writes it there); no-op if the
    file is absent or the process is already gone.
    """
    pid_file = pgdata / "postmaster.pid"
    try:
        pid = int(pid_file.read_text().splitlines()[0].strip())
    except Exception:
        return
    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(3)
        except psutil.TimeoutExpired:
            proc.kill()
    except Exception:
        pass
