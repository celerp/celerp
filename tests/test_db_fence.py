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
import sqlalchemy as sa

import celerp
from sqlalchemy.pool import NullPool

from celerp.db_url import sync_url

from test_db_compatibility import DATABASE_URL, NEWER, RUNNING, _meta, scratch, snapshot  # noqa: F401  (scratch is a fixture)

pytestmark = [
    pytest.mark.process,
    pytest.mark.skipif(
        not DATABASE_URL.startswith("postgresql"), reason="needs a live Postgres database"
    ),
]

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

def hold(loop=None):
    # Each stdin line "write" or "write lifecycle" writes the sentinel through the
    # request (or lifecycle) engine every request and background job uses.
    print("READY", flush=True)
    for line in sys.stdin:
        if loop is not None and line.startswith("write"):
            fut = asyncio.run_coroutine_threadsafe(write(line.split()[1:]), loop)
            try:
                fut.result()
                print("WROTE", flush=True)
            except Exception as exc:
                print(f"REFUSED {type(exc).__name__}: {exc}", flush=True)

async def write(args):
    import sqlalchemy as sa
    from celerp import db
    note = sa.text("UPDATE zz_sentinel SET note = :n WHERE id = 1")
    if args == ["lifecycle"]:
        async with db.lifecycle_engine.begin() as conn:
            await conn.execute(note, {"n": f"written-by-{sys.argv[1]}"})
    else:
        async with db.get_session_ctx() as session:
            await session.execute(note, {"n": f"written-by-{sys.argv[1]}"})
            await session.commit()

if path == "api":
    import celerp.main as app_main
    async def run():
        async with app_main.lifespan(app_main.app):
            await asyncio.to_thread(hold, asyncio.get_running_loop())
    asyncio.run(run())
elif path == "ui":
    from ui.app import app
    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(hold, asyncio.get_running_loop())
    asyncio.run(run())
elif path.startswith("shutdown-"):
    # The real API startup with a job that ignores being stopped: a background job
    # boot started (shutdown-boot) or a migration run (shutdown-run). Once told to
    # stop it waits for the file <data_dir>.go and then writes the sentinel.
    import os, uuid
    import celerp.main as app_main
    from celerp.services import migrations, reorder
    app_main._SHUTDOWN_GRACE_S = 1
    async def stubborn(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        print("STOPPING", flush=True)
        while not os.path.exists(f"{data_dir}.go"):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass
        await write([])
        print("LATE WRITE", flush=True)
    if path == "shutdown-boot":
        reorder.reorder_alert_loop = stubborn
    else:
        migrations.run_migration = stubborn
    async def run():
        async with app_main.lifespan(app_main.app):
            if path == "shutdown-run":
                migrations.schedule_run(uuid.uuid4())
            await asyncio.to_thread(hold)
    asyncio.run(run())
elif path == "migrate":
    from click.testing import CliRunner
    from celerp.cli import main
    result = CliRunner().invoke(main, ["migrate", "--db-url", url])
    print(result.output, flush=True)
    sys.exit(result.exit_code)
elif path in ("backfill", "ownership", "restamp"):
    # `celerp migrate` (backfill), init's ownership fix (ownership), or `celerp
    # migrate` restamping a database whose stamp is wrong and then upgrading it one
    # revision at a time (restamp), paused at chosen points: it prints PAUSED and
    # waits for a "go" line. The first pause is inside the first backfill or
    # outside write, or just after the restamp; the second is just before the next
    # transaction's fence check after it.
    import sqlalchemy as sa
    from celerp import cli
    from celerp.db_url import sync_url
    from celerp.migrations import _data_reconcile
    armed = []
    def pause():
        print("PAUSED", flush=True)
        assert sys.stdin.readline().strip() == "go"
    gate = compatibility.Fence._gate
    def paused_gate(self, conn):
        if armed == [True]:
            armed.append(False)
            pause()
        gate(self, conn)
    compatibility.Fence._gate = paused_gate
    def write(conn, note):
        conn.execute(sa.text("UPDATE zz_sentinel SET note = :n WHERE id = 1"), {"n": note})
    if path == "backfill":
        from alembic import op
        class Backfill:
            def __init__(self, n):
                self.revision, self.module = f"backfill{n}", self
            def upgrade(self):
                write(op.get_bind(), f"{self.revision}-by-{sys.argv[1]}")
                if self.revision == "backfill1":
                    pause()
                    armed.append(True)
        _data_reconcile.data_backfill_scripts = lambda: [Backfill(1), Backfill(2)]
        cli.main(["migrate", "--db-url", url])
    if path == "restamp":
        # The walker finds the head revision's change missing, so the stamp moves
        # back one; the upgrade then meets that change already present and stamps
        # past it.
        from alembic import command
        from alembic.script import ScriptDirectory
        from celerp.alembic_config import build_alembic_config
        from celerp.migrations import _auto_stamp
        head = ScriptDirectory.from_config(build_alembic_config()).get_revision("head")
        _auto_stamp.find_safe_stamp = lambda *args, **kwargs: head.down_revision
        stamp = command.stamp
        def paused_stamp(*args, **kwargs):
            stamp(*args, **kwargs)
            print(f"STAMPED {args[1]}", flush=True)
            if not armed:
                pause()
                armed.append(True)
        command.stamp = paused_stamp
        cli.main(["migrate", "--db-url", url])
    import subprocess
    observed = []
    def psql(sql, db="postgres", *flags):
        # Stands in for psql, a writer outside this process's engines: it writes
        # through a connection of its own and records the version it found.
        engine = sa.create_engine(sync_url(url), poolclass=sa.pool.NullPool)
        with engine.begin() as conn:
            observed.append(conn.execute(sa.text(
                "SELECT value FROM instance_meta WHERE key = 'newest_celerp_version'")).scalar())
            write(conn, f"psql{len(observed)}-by-{sys.argv[1]}")
        engine.dispose()
        print(f"OBSERVED {observed[-1]}", flush=True)
        if len(observed) == 1:
            pause()
            armed.append(True)
        return subprocess.CompletedProcess(["psql"], 0, "", "")
    cli._psql = psql
    cli._needs_ownership_fix = lambda db_url: True
    cli._init_database(url)
elif path == "reset":
    from click.testing import CliRunner
    from celerp import cli
    cli._read_config = lambda: {"database": {"url": url}}
    result = CliRunner().invoke(cli.main, ["reset-password", "--email", "nobody@example.com",
                                           "--password", "a-long-enough-password"])
    print(result.output, flush=True)
    sys.exit(result.exit_code)
"""


def _env(url: str, data_dir) -> dict:
    """Each process keeps its own config file, so the modules one process turns on are
    not read by another."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("MODULE_DIR", "ENABLED_MODULES", "CELERP_UPDATE_VERIFY")}
    env.update({"DATABASE_URL": url, "ALLOW_INSECURE_JWT": "true", "PYTHONUNBUFFERED": "1",
                "CELERP_CONFIG": f"{data_dir}.toml"})
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
        text=True, env=_env(url, data_dir))
    proc.log = log
    _SPAWNED.append(proc)
    return proc


def _output(proc: subprocess.Popen) -> str:
    with open(proc.log) as f:
        return f.read()


def _hold(version: str, path: str, url: str, data_dir) -> subprocess.Popen:
    """A process of *version* that opened the database through *path* and keeps it open."""
    return _ready(_spawn(version, path, url, data_dir))


def _ready(proc: subprocess.Popen) -> subprocess.Popen:
    """*proc*, once it has opened the database and holds it open."""
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


# --- A holder whose fence connection dies while the process lives ------------------


def _send(proc: subprocess.Popen, line: str) -> None:
    """A holder that already ended (a background job of its own found the fence
    gone) gets nothing; _reply reports how it ended."""
    try:
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
    except BrokenPipeError:
        pass


def _reply(proc: subprocess.Popen, seen: int, timeout: float = 60) -> str:
    """The holder's next WROTE/REFUSED line after the first *seen* of them, or
    "EXITED <code>" when the process ended without one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        replies = [ln for ln in _output(proc).splitlines() if ln.startswith(("WROTE", "REFUSED"))]
        if len(replies) > seen:
            return replies[seen]
        if proc.poll() is not None:
            return f"EXITED {proc.returncode}"
        time.sleep(0.2)
    raise AssertionError(_output(proc))


def _fence_backends(url: str, version: str) -> list[int]:
    """Backends holding *version*'s session fence lock in *url*'s database: idle,
    so not a transaction holding the lock for its own lifetime."""
    from celerp.migrations.compatibility import _FENCE_NAMESPACE, _cohort_key
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text(
                "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 2 AND a.state = 'idle' "
                "AND l.classid::bigint = :ns AND l.objid::bigint = :key AND l.mode = 'ShareLock' "
                "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"),
                {"ns": _FENCE_NAMESPACE, "key": _cohort_key(version)}).scalars())
    finally:
        engine.dispose()


def _kill_fence_backend(url: str, version: str) -> None:
    """End only the holder's fence session; the process and its pool stay up."""
    pids = _fence_backends(url, version)
    assert len(pids) == 1, pids
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT pg_terminate_backend(:p)"), {"p": pids[0]}).scalar()
    finally:
        engine.dispose()
    deadline = time.monotonic() + 10
    while _fence_backends(url, version):
        assert time.monotonic() < deadline
        time.sleep(0.1)


def _transactions_holding(url: str, version: str) -> int:
    """Transactions holding *version*'s fence lock for their own lifetime."""
    from celerp.migrations.compatibility import _FENCE_NAMESPACE, _cohort_key
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.objsubid = 2 AND a.state <> 'idle' "
                "AND l.classid::bigint = :ns AND l.objid::bigint = :key"),
                {"ns": _FENCE_NAMESPACE, "key": _cohort_key(version)}).scalar()
    finally:
        engine.dispose()


def _freeze(proc: subprocess.Popen, url: str, version: str) -> None:
    """Stop *proc* between transactions. A busy API takes a lost fence again within
    moments (its background jobs write), so the window a newer version needs is made
    by stopping the older process outright."""
    deadline = time.monotonic() + 30
    while True:
        proc.send_signal(signal.SIGSTOP)
        if not _transactions_holding(url, version):
            return
        proc.send_signal(signal.SIGCONT)
        assert time.monotonic() < deadline
        time.sleep(0.05)


@pytest.mark.parametrize("holder", ["api", "ui"])
@pytest.mark.parametrize("engine", ["request", "lifecycle"])
def test_an_older_process_that_lost_its_fence_cannot_write_after_a_newer_one_opened(
        scratch, tmp_path, holder, engine):
    url = scratch()
    old = _hold(OLDER, holder, url, tmp_path / "old")
    new = None
    try:
        _send(old, "write" if engine == "request" else "write lifecycle")
        assert _reply(old, 0) == "WROTE", _output(old)
        _freeze(old, url, OLDER)
        _kill_fence_backend(url, OLDER)
        new = _hold(NEWER, "api", url, tmp_path / "new")
        before = snapshot(url)
        assert _meta(url)["newest_celerp_version"] == NEWER
        old.send_signal(signal.SIGCONT)
        _send(old, "write" if engine == "request" else "write lifecycle")
        reply = _reply(old, 1)
        assert reply != "WROTE", _output(old)
        assert snapshot(url) == before
        # The older process ends rather than wait to write over the newer version's data.
        old.wait(timeout=30)
        assert old.returncode != 0, _output(old)
        assert f"last opened with Celerp {NEWER}" in _output(old), _output(old)
        assert snapshot(url) == before
    finally:
        old.send_signal(signal.SIGCONT)
        _stop(old)
        if new is not None:
            _stop(new)


@pytest.mark.parametrize("holder", ["api", "ui"])
def test_a_lost_fence_is_taken_again_before_the_next_write(scratch, tmp_path, holder):
    url = scratch()
    old = _hold(OLDER, holder, url, tmp_path / "old")
    try:
        _kill_fence_backend(url, OLDER)
        _send(old, "write")
        assert _reply(old, 0) == "WROTE", _output(old)
        assert snapshot(url)["sentinel"] == [(1, f"written-by-{OLDER}")]
        # The fence is held again, so a newer version is kept out as before.
        assert len(_fence_backends(url, OLDER)) == 1
        before = snapshot(url)
        code, out = _run(NEWER, "migrate", url, tmp_path / "new", wait=2)
        assert code == 1, out
        assert "Another version of Celerp is still using this data" in out, out
        assert snapshot(url) == before
        _send(old, "write")
        assert _reply(old, 1) == "WROTE", _output(old)
    finally:
        _stop(old)


def test_a_newer_version_waits_for_an_older_write_in_flight(scratch, tmp_path, monkeypatch):
    """The write gate holds the fence for the life of each transaction, so a newer
    version that finds the older fence connection gone still cannot slip in between
    the check and the write."""
    from celerp.migrations import compatibility
    monkeypatch.setattr(celerp, "__version__", OLDER)
    url = scratch()
    held = compatibility.Fence.join(sync_url(url))
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    held.guard(engine)
    try:
        with engine.connect() as conn:
            conn.begin()
            conn.execute(sa.text("UPDATE zz_sentinel SET note = 'in-flight' WHERE id = 1"))
            # The fence session dies while the write transaction is open.
            pid = held._conn.execute(sa.text("SELECT pg_backend_pid()")).scalar()
            held._conn.rollback()
            killer = sa.create_engine(sync_url(url), poolclass=NullPool)
            with killer.connect() as k:
                k.execute(sa.text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            killer.dispose()
            code, out = _run(NEWER, "migrate", url, tmp_path / "new", wait=2)
            assert code == 1, out
            assert "Another version of Celerp is still using this data" in out, out
            conn.commit()
    finally:
        held.release()
        engine.dispose()


def test_a_restore_keeps_the_fence_while_pg_restore_runs(scratch, tmp_path, monkeypatch):
    """pg_restore writes from a process of its own; a newer version cannot be admitted
    while it runs, even if the fence session ends meanwhile."""
    import subprocess as sp
    from celerp.migrations import compatibility
    from celerp.services.backup import restore_database_file
    monkeypatch.setattr(celerp, "__version__", OLDER)
    url = scratch()
    seen = {}

    def runner(command, **kwargs):
        _kill_fence_backend(url, OLDER)
        seen["newer"] = _run(NEWER, "migrate", url, tmp_path / "new", wait=2)
        return sp.CompletedProcess(command, 0, b"", b"")

    held = compatibility.Fence.join(sync_url(url))
    try:
        restore_database_file(tmp_path / "dump", url, runner=runner)
    finally:
        held.release()
    code, out = seen["newer"]
    assert code == 1, out
    assert "Another version of Celerp is still using this data" in out, out


# --- Every change a command makes is inside its fence ----------------------------


def _paused(proc: subprocess.Popen, count: int, timeout: float = 120) -> None:
    """Wait until *proc* has paused *count* times."""
    deadline = time.monotonic() + timeout
    while _output(proc).splitlines().count("PAUSED") < count:
        assert proc.poll() is None and time.monotonic() < deadline, _output(proc)
        time.sleep(0.2)


@pytest.mark.parametrize("path", ["backfill", "ownership"])
def test_a_command_that_lost_its_fence_mid_write_makes_no_write_after_a_newer_version_opened(
        scratch, tmp_path, path):
    """`celerp migrate` replaying data backfills, and init changing table ownership
    through psql, lose the fence session part way. A newer version stays out until
    that write ends, and once it is in, the older command writes nothing more. A newer
    server finishes starting only once an older migration still running has ended, as
    no two schema changes run at once."""
    url = scratch()
    old = _spawn(OLDER, path, url, tmp_path / "old")
    new = None
    try:
        _paused(old, 1)
        _kill_fence_backend(url, OLDER)
        before = snapshot(url)
        code, out = _run(NEWER, "migrate", url, tmp_path / "early", wait=3)
        assert code == 1, out
        assert "Another version of Celerp is still using this data" in out, out
        assert snapshot(url) == before
        _send(old, "go")
        _paused(old, 2)
        new = _spawn(NEWER, "api", url, tmp_path / "new")
        deadline = time.monotonic() + 120
        while _meta(url).get("newest_celerp_version") != NEWER:
            assert new.poll() is None and time.monotonic() < deadline, _output(new)
            time.sleep(0.2)
        if path == "backfill":  # its start waits for the migration to end
            time.sleep(2)
            assert "READY" not in _output(new).splitlines(), _output(new)
            seen = lambda: snapshot(url)["sentinel"]  # noqa: E731  (the start goes on after it)
        else:
            _ready(new)
            seen = lambda: snapshot(url)  # noqa: E731
        before = seen()
        _send(old, "go")
        old.wait(timeout=60)
        assert old.returncode != 0, _output(old)
        assert f"last opened with Celerp {NEWER}" in _output(old), _output(old)
        assert seen() == before
        _ready(new)
        observed = [ln.split()[1] for ln in _output(old).splitlines() if ln.startswith("OBSERVED")]
        assert set(observed) <= {OLDER}, observed
    finally:
        _stop(old)
        if new is not None:
            _stop(new)


def test_a_migrate_that_lost_its_fence_after_restamping_makes_no_further_change(scratch, tmp_path):
    """`celerp migrate` restamps a database whose stamp disagrees with its schema and
    then upgrades it one revision at a time. When it loses the fence between the
    restamp and the upgrade and a newer version opens the database, the upgrade
    changes nothing: neither the revision's DDL nor the stamp past it."""
    from alembic.script import ScriptDirectory
    from celerp.alembic_config import build_alembic_config

    head = ScriptDirectory.from_config(build_alembic_config()).get_revision("head")
    url = scratch()
    old = _spawn(OLDER, "restamp", url, tmp_path / "old")
    new = None
    try:
        _paused(old, 1)
        assert snapshot(url)["alembic_version"] == [(head.down_revision,)], _output(old)
        _kill_fence_backend(url, OLDER)
        _send(old, "go")
        _paused(old, 2)
        new = _hold(NEWER, "api", url, tmp_path / "new")
        assert _meta(url)["newest_celerp_version"] == NEWER
        before = snapshot(url)
        _send(old, "go")
        old.wait(timeout=60)
        assert old.returncode != 0, _output(old)
        assert f"last opened with Celerp {NEWER}" in _output(old), _output(old)
        assert snapshot(url) == before
        assert snapshot(url)["alembic_version"] == [(head.down_revision,)]
        assert [ln for ln in _output(old).splitlines() if ln.startswith("STAMPED")] == [
            f"STAMPED {head.down_revision}"], _output(old)
    finally:
        _stop(old)
        if new is not None:
            _stop(new)


def test_init_records_its_version_before_changing_ownership(scratch, monkeypatch):
    """The ownership fix runs as the superuser through psql; it never starts on a
    database this copy has not been admitted to."""
    from celerp import cli
    monkeypatch.setattr(celerp, "__version__", OLDER)
    url = scratch()
    seen = []

    def psql(sql, db="postgres", *flags):
        seen.append(_meta(url).get("newest_celerp_version"))
        return subprocess.CompletedProcess(["psql"], 0, "", "")

    monkeypatch.setattr(cli, "_psql", psql)
    monkeypatch.setattr(cli, "_needs_ownership_fix", lambda db_url: True)
    monkeypatch.setattr(cli, "_migrate_to_head", lambda db_url: True)
    cli._init_database(url)
    assert seen and set(seen) == {OLDER}, seen


# --- Shutdown never leaves a writer behind the fence ------------------------------


@pytest.mark.parametrize("job", ["boot", "run"])
def test_a_job_that_will_not_stop_ends_the_process_before_a_newer_version_can_open(
        scratch, tmp_path, job):
    """The API stops a job that ignores being told to stop by ending the process while
    it still holds the fence: the job never writes, and no newer version is admitted
    while it is alive."""
    url = scratch()
    old = _hold(OLDER, f"shutdown-{job}", url, tmp_path / "old")
    try:
        before = snapshot(url)["sentinel"]
        old.stdin.close()
        try:
            old.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        alive_after_shutdown = old.poll() is None
        code, out = _run(NEWER, "migrate", url, tmp_path / "new", wait=5)
        newer_admitted = code == 0
        (tmp_path / "old.go").touch()
        deadline = time.monotonic() + 15
        while old.poll() is None and "LATE WRITE" not in _output(old) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert "STOPPING" in _output(old), _output(old)
        assert not (alive_after_shutdown and newer_admitted), (
            "a newer version opened the database while the older process still ran a job", out)
        assert snapshot(url)["sentinel"] == before, _output(old)
        assert old.returncode not in (None, 0), _output(old)
        assert "did not stop" in _output(old), _output(old)
    finally:
        old.kill()
        old.wait()
