# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The tests use only a Postgres server someone pointed them at on purpose, and never retune it.

The root conftest decides which server the run uses. The application's DATABASE_URL (an .env
can carry a staging or production one) is never that decision: without CELERP_TEST_DATABASE_URL
the run starts a throwaway container, and CELERP_TEST_DATABASE_URL must name a database with
"test" in its name on this machine. Durability settings are only ever chosen for a server the
harness started itself (the container's start command); CI tunes its own service container in
a workflow step.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import pytest

from conftest_support import RefusedTestDatabase, TEST_DATABASE_VAR, opted_in_test_database
from test_helpers import REPO_ROOT

pytestmark = pytest.mark.process

_PROBE_WORKER = "optinprobe"
_RECORD_SQL = """
import psycopg2, psycopg2.extensions as ext
class Recording(ext.cursor):
    def execute(self, query, args=None):
        print("SQL", query, flush=True)
        return super().execute(query, args)
_connect = psycopg2.connect
psycopg2.connect = lambda *a, **k: _connect(*a, cursor_factory=Recording, **k)
"""


def _local_test_url() -> str:
    """This run's own database: on this machine, with "test" in its name."""
    return os.environ["DATABASE_URL"]


def _import_conftest(env: dict[str, str], prelude: str = "") -> subprocess.CompletedProcess:
    """Import the root conftest in a fresh process with exactly *env* for the database variables."""
    base = {k: v for k, v in os.environ.items()
            if k not in ("DATABASE_URL", TEST_DATABASE_VAR, "PYTEST_XDIST_WORKER")}
    code = prelude + "import conftest, os\nprint('URL', os.environ['DATABASE_URL'])\n"
    return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env={**base, **env},
                          capture_output=True, text=True, timeout=120)


def _used_url(proc: subprocess.CompletedProcess) -> str | None:
    found = re.search(r"^URL (\S+)$", proc.stdout, re.M)
    return found.group(1) if found else None


@pytest.mark.timeout(120)
def test_an_inherited_database_url_is_never_used():
    proc = _import_conftest({
        "DATABASE_URL": "postgresql+asyncpg://celerp:celerp@app-server.invalid:5432/celerp",
        TEST_DATABASE_VAR: _local_test_url(),
    })
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _used_url(proc) == _local_test_url()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp",
    "postgresql+asyncpg://celerp:celerp@db.example.com:5432/celerp_test",
    "postgresql+asyncpg://celerp:celerp@10.0.0.5:5432/celerp_test",
    "postgresql+asyncpg://celerp:celerp@/celerp_test?host=db.example.com",
    "sqlite+aiosqlite:///celerp_test.db",
], ids=["name-without-test", "remote-host", "remote-address", "remote-host-in-query", "not-postgres"])
def test_a_server_not_opted_in_on_purpose_is_refused(url):
    # A preset DATABASE_URL pointing at a usable server: the refusal is the opt-in's alone.
    proc = _import_conftest({TEST_DATABASE_VAR: url, "DATABASE_URL": _local_test_url()})
    assert proc.returncode != 0
    assert _used_url(proc) is None
    out = proc.stdout + proc.stderr
    assert TEST_DATABASE_VAR in out and '"test"' in out
    assert "celerp:celerp@" not in out, "the refusal must not print the password"


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp_test",
    "postgresql+asyncpg://celerp:celerp@127.0.0.1/my_test_db",
    "postgresql+asyncpg://celerp:celerp@[::1]:5432/test",
    "postgresql+asyncpg://celerp@/celerp_test?host=/var/run/postgresql",
    "postgresql://celerp@/celerp_test",
])
def test_a_local_test_database_is_accepted(url):
    assert opted_in_test_database({TEST_DATABASE_VAR: url}) == url


def test_no_opt_in_means_no_existing_server():
    assert opted_in_test_database({"DATABASE_URL": _local_test_url()}) is None
    with pytest.raises(RefusedTestDatabase):
        opted_in_test_database({TEST_DATABASE_VAR: "postgresql://u@db.example.com/celerp_test"})


@pytest.mark.timeout(120)
def test_an_opted_in_server_is_never_retuned():
    """A worker's start on an opted-in server creates its database and changes no server setting."""
    proc = _import_conftest({TEST_DATABASE_VAR: _local_test_url(), "DATABASE_URL": _local_test_url(),
                             "PYTEST_XDIST_WORKER": _PROBE_WORKER}, prelude=_RECORD_SQL)
    try:
        sql = [line[4:] for line in proc.stdout.splitlines() if line.startswith("SQL ")]
        assert any(s.startswith("CREATE DATABASE") for s in sql), proc.stdout[-2000:] + proc.stderr[-2000:]
        assert not [s for s in sql if re.search(r"alter\s+system|pg_reload_conf", s, re.I)]
    finally:
        parts = urlsplit(_local_test_url().replace("+asyncpg", ""))
        conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                                password=parts.password, dbname=parts.path.lstrip("/"))
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{parts.path.lstrip("/")}_{_PROBE_WORKER}"')
        conn.close()


def _harness_files() -> list[Path]:
    """Every file that sets up a test run: each conftest.py, its support module and test_helpers."""
    root = Path(REPO_ROOT)
    skip = {".venv", "node_modules", ".git"}
    found = [p for p in root.rglob("conftest.py") if not skip & set(p.relative_to(root).parts)]
    return sorted(found + [root / "conftest_support.py", root / "test_helpers.py"])


def test_no_harness_code_changes_server_settings():
    files = _harness_files()
    assert len(files) > 3
    offenders = [str(p.relative_to(REPO_ROOT)) for p in files
                 if re.search(r"alter\s+system|pg_reload_conf", p.read_text(), re.I)]
    assert not offenders


def test_only_the_opt_in_names_the_test_server():
    """Only conftest_support reads the opt-in, and CI hands its server to the tests only through it."""
    literal = re.compile(rf"[\"']{TEST_DATABASE_VAR}[\"']")
    readers = [str(p.relative_to(REPO_ROOT)) for p in _harness_files() if literal.search(p.read_text())]
    assert readers == ["conftest_support.py"]
    ci = (Path(REPO_ROOT) / ".github" / "workflows" / "ci.yml").read_text()
    assert not re.search(r"^\s*DATABASE_URL:", ci, re.M)
    assert re.search(rf"^\s*{TEST_DATABASE_VAR}:", ci, re.M)


def test_a_worker_uses_the_container_the_run_started(monkeypatch):
    """The run's own container is reached at whatever host Docker gives it (the gateway inside a
    devcontainer, a remote DOCKER_HOST), and every xdist worker uses it as is; only an opt-in
    someone set is held to this machine, in every process."""
    import conftest_support

    container = "postgresql+asyncpg://test:test@172.17.0.1:32768/test"
    controller: dict[str, str] = {}
    assert conftest_support.provision_test_database(controller, lambda: container) == container
    assert TEST_DATABASE_VAR not in controller

    def no_container():
        raise AssertionError("a worker never starts a container")
    monkeypatch.setattr(conftest_support, "provision_worker_db", lambda url, worker: f"{url}_{worker}")
    worker = {**controller, "PYTEST_XDIST_WORKER": "gw0"}
    assert conftest_support.provision_test_database(worker, no_container) == f"{container}_gw0"
    with pytest.raises(RefusedTestDatabase):
        conftest_support.provision_test_database(
            {**controller, TEST_DATABASE_VAR: "postgresql://u@db.example.com/celerp_test",
             "PYTEST_XDIST_WORKER": "gw0"}, no_container)


async def test_maintenance_never_runs_on_the_tables_the_run_builds(session):
    """Maintenance (VACUUM, ANALYZE) holds a table in a mode a company reset's no-wait locks
    refuse as busy, at a moment the server picks, so it is off on every table the run builds."""
    from sqlalchemy import text

    from celerp.models.base import Base

    rows = dict((await session.execute(text(
        "SELECT relname, coalesce(reloptions, '{}') FROM pg_class "
        "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"))).all())
    built = [name for name in Base.metadata.tables if name in rows]
    assert len(built) > 50
    assert [name for name in built if "autovacuum_enabled=false" not in rows[name]] == []
