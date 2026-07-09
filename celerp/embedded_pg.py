# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Embedded PostgreSQL provider for pip installs.

The pip package ships client drivers only; a self-hosted user with no system
Postgres previously hit "connection refused" on `celerp init`. This module boots
a self-contained PostgreSQL 17 cluster from the binaries in the `celerp-postgres`
wheel (no sudo, no system service), giving the pip path the same zero-setup
experience the Electron desktop app already provides.

The cluster manager lives here in full — initdb, pg_ctl start/stop, database
creation — behind a small seam (`is_available/ensure_cluster/bin_dir/pgdata_dir/
wipe`) so the binary source can be swapped without touching the CLI.

The cluster is loopback/unix-socket only and single-user: on POSIX it listens on
a private socket directory (never TCP); on Windows on 127.0.0.1 with a port
persisted in the data dir. `celerp init` only falls back to it when no PostgreSQL
server is already reachable — an existing server always wins.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

# Name of the application database created inside the embedded cluster. The
# cluster's bootstrap superuser is `postgres`; the app connects as that user
# locally, so no role/password provisioning (and no sudo) is needed.
_DATABASE = "celerp"

# pg_ctl handles we started in this process, stopped again at exit so no
# postmaster outlives `celerp start` (its SIGTERM handler exits via sys.exit,
# which runs atexit hooks).
_STARTED: set[Path] = set()


def is_available() -> bool:
    """True if the embedded-Postgres backend can run on this interpreter.

    The `celerp-postgres` dependency is marker-limited to platforms with wheels
    (Linux x86_64/aarch64 glibc+musl, Windows x64); macOS 26+ users can install
    it manually. A False here means "embedded mode unavailable" and callers fall
    back to external guidance.
    """
    try:
        import celerp_postgres  # noqa: F401
    except Exception:
        return False
    return True


def pgdata_dir(config_dir: Path) -> Path:
    """Location of the embedded cluster's data directory.

    Kept next to config.toml (`~/.config/celerp/pgdata`) rather than under the
    cwd-relative `settings.data_dir`, so the cluster is found regardless of which
    directory `celerp` is invoked from.
    """
    return Path(config_dir) / "pgdata"


def bin_dir() -> str | None:
    """Directory holding the bundled `pg_dump`/`pg_restore`, or None if the
    backend is unavailable. Wired into `[backup] pg_bin_dir` so backups use the
    matching-version tools rather than a system pg_dump of unknown version."""
    try:
        import celerp_postgres
    except Exception:
        return None
    return celerp_postgres.bin_dir()


# ── internals ─────────────────────────────────────────────────────────────────


def _tool(name: str) -> str:
    import celerp_postgres

    return celerp_postgres.tool(name)


def _env() -> dict:
    """Subprocess env for the PG tools. On musl wheels ICU reads its data from
    a bundled file that must be pointed at via ICU_DATA (PG17's initdb always
    version-checks the built-in 'unicode' ICU collation)."""
    import celerp_postgres

    env = os.environ.copy()
    icu = celerp_postgres.icu_data_dir() if hasattr(celerp_postgres, "icu_data_dir") else None
    if icu:
        env["ICU_DATA"] = icu
    return env


def _socket_dir(pgdata: Path) -> Path:
    """Deterministic, SHORT socket directory for this cluster.

    Derived from the pgdata path so the stored URI stays valid across reboots,
    and placed under /tmp because a socket path inside a deep config dir can
    exceed the 107-char AF_UNIX limit.
    """
    h = hashlib.sha1(str(Path(pgdata).resolve()).encode()).hexdigest()[:10]
    return Path("/tmp") / f"celerp-pg-{h}"


def _win_port(pgdata: Path) -> int:
    """Windows: loopback TCP port, chosen once and persisted in the data dir so
    the stored URI stays valid across restarts."""
    pf = pgdata / "celerp.port"
    if pf.exists():
        return int(pf.read_text().strip())
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pf.write_text(str(port))
    return port


def _run(cmd: list[str], fail_hint: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, env=_env(), cwd=cwd)
    if r.returncode:
        raise RuntimeError(
            f"{fail_hint} (rc={r.returncode})\n"
            f"STDOUT: {r.stdout[-1200:]}\nSTDERR: {r.stderr[-1200:]}"
        )
    return r


def _is_running(pgdata: Path) -> bool:
    r = subprocess.run(
        [_tool("pg_ctl"), "status", "-D", str(pgdata)],
        capture_output=True, text=True, env=_env(),
    )
    return r.returncode == 0


def _initdb(pgdata: Path) -> None:
    # libc locale provider: keeps the cluster independent of ICU data at its
    # core; ICU stays available for opt-in per-collation use.
    #
    # Relative single-component -D with cwd at the parent: initdb then creates
    # exactly one directory and never walks the ancestor chain, which
    # misfires on some Windows environments (GitHub runners error "File
    # exists" on existing intermediates).
    _run(
        [_tool("initdb"), "-D", pgdata.name, "-U", "postgres", "-A", "trust",
         "-E", "UTF8", "--locale-provider=libc"],
        "embedded PostgreSQL initdb failed",
        cwd=str(pgdata.parent),
    )


def _start(pgdata: Path) -> tuple[str, int | None]:
    """Start the postmaster (idempotent); returns (host, port) — host is the
    socket dir on POSIX (port None), 127.0.0.1 on Windows."""
    if os.name == "nt":
        host: str = "127.0.0.1"
        port: int | None = _win_port(pgdata)
        opts = f"-c listen_addresses=127.0.0.1 -c port={port}"
    else:
        sock = _socket_dir(pgdata)
        sock.mkdir(mode=0o700, exist_ok=True)
        host, port = str(sock), None
        opts = f"-c listen_addresses='' -c unix_socket_directories='{sock}'"

    if not _is_running(pgdata):
        log = pgdata / "server.log"
        try:
            _run(
                [_tool("pg_ctl"), "-D", str(pgdata), "-w", "-t", "60",
                 "-o", opts, "-l", str(log), "start"],
                "embedded PostgreSQL failed to start",
            )
        except RuntimeError as e:
            tail = log.read_text()[-1200:] if log.exists() else "-"
            raise RuntimeError(f"{e}\nSERVERLOG: {tail}") from None
        if pgdata not in _STARTED:
            _STARTED.add(pgdata)
            if not _STARTED - {pgdata}:  # first cluster this process started
                import atexit

                atexit.register(_stop_all)
    return host, port


def _stop_all() -> None:
    """atexit: stop every postmaster this process started (data preserved)."""
    for pgdata in list(_STARTED):
        subprocess.run(
            [_tool("pg_ctl"), "-D", str(pgdata), "-w", "-t", "30", "-m", "fast", "stop"],
            capture_output=True, text=True, env=_env(),
        )
        _STARTED.discard(pgdata)


def _uri(host: str, port: int | None, database: str) -> str:
    if port is not None:
        return f"postgresql+asyncpg://postgres@{host}:{port}/{database}"
    return f"postgresql+asyncpg://postgres@/{database}?host={host}"


def _ensure_app_database(host: str, port: int | None) -> None:
    """Create the `celerp` database if missing, via the `postgres` maintenance
    DB (CREATE DATABASE needs autocommit). The connecting user is the cluster
    superuser, so none of the external path's sudo/ownership dance applies."""
    from sqlalchemy import create_engine, text

    engine = create_engine(
        _uri(host, port, "postgres").replace("+asyncpg", ""),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": _DATABASE}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{_DATABASE}"'))
    finally:
        engine.dispose()


# ── public lifecycle ──────────────────────────────────────────────────────────


def ensure_cluster(config_dir: Path) -> str:
    """Boot the embedded cluster (initdb on first run, start if stopped) and
    ensure the app database exists. Returns an asyncpg connection URI.

    Idempotent: safe to call from every DB-touching CLI command. Postgres itself
    recovers stale postmaster.pid files from crashed processes on start.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    # Resolve to the canonical long-form path: Windows 8.3 short names in the
    # location (e.g. C:\Users\RUNNER~1\... from a short-name %TMP%) make
    # initdb's directory walk fail with "File exists".
    config_dir = config_dir.resolve()
    pgdata = pgdata_dir(config_dir)

    if not (pgdata / "PG_VERSION").exists():
        # Let initdb create the pgdata leaf itself (its parent exists above).
        # Pre-creating it trips Windows initdb's ancestor walk ("could not
        # create directory ...: File exists").
        _initdb(pgdata)
    host, port = _start(pgdata)
    _ensure_app_database(host, port)
    return _uri(host, port, _DATABASE)


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
        if _is_running(pgdata):
            subprocess.run(
                [_tool("pg_ctl"), "-D", str(pgdata), "-w", "-t", "30", "-m", "fast", "stop"],
                capture_output=True, text=True, env=_env(),
            )
        _STARTED.discard(pgdata)
    except Exception:
        pass
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
